# Scheduling: what runs where, what it costs, what has already gone wrong

*The single reference for the worker's scheduling. Read this before
changing anything that decides WHAT RUNS WHEN — the round loop, a stage's
selection or batching, the archive passes, the pools, the maintainers.*

## 0. Why this file exists

Scheduling was reworked six times in one day (amendments 45, 47, 48, 49,
50, 51), and three of those reworks fixed a defect the previous one had
introduced or walked past. The pattern was always the same:

- a fix was made **by inspection** instead of by measurement, so it
  targeted the wrong cost;
- the fix addressed **one layer** while the identical defect sat one
  layer down (a query got an index hint while its sibling kept the scan;
  a walk was windowed while the window itself still scanned);
- the new code went onto **the critical path without a bound**, so when
  it misbehaved a collection stopped dead rather than degrading.

Everything below is what those six passes cost. The checklist in §6 is
the part that prevents the seventh.

## 1. The model — who runs what

| runner | owns | must never |
|---|---|---|
| **main loop** (`run_forever`) | node control state, the round over collections that do NOT own their embed, bundles, telemetry publish | run a long CPU or IO job; it is GPU-serial across collections by doctrine |
| **pipeline thread**, one per collection (`_pipeline_worker`) | that collection's rdf/fts, its archive passes, and — when the collection `owns_own_embed` (its encoder is its own container) — its embed step | touch another collection's manifest, index or Lance table |
| **index maintainer** (one thread, `_index_maintainer`) | the vector index and the `book_id` index of every collection, one at a time | run on a pipeline thread (a refresh took 41 minutes and idled wikivec), hold a manifest write transaction while it works |
| **stats publisher** | telemetry, the seeding table | do any pipeline work |
| **discovery scan** (one thread per collection, on demand) | enumerating the corpus into the manifest in batches | hold the write lock across a whole scan |
| **acquisition** (background, budgeted) | fetching corpus and archive bytes | bypass the transfer manager's budget |
| **parse pool** (`app/parallel.py`, spawned children at `nice 10`) | fetch/parse/chunk per document, metadata, pack records, `prepare` prefetches | write ANYTHING — every writer stays in the worker |
| **encoders** (separate containers) | embeddings | — |

Two ownership rules follow from the table and are load-bearing:

1. **One writer per thing.** One tantivy writer per collection, one Lance
   writer per collection (`vector_write_lock`), one manifest writer at a
   time (`BEGIN IMMEDIATE`). The pool computes; it never writes.
2. **A collection's work happens on its own thread**, so its stages
   cannot overlap each other — rdf, fts and the archive alternate, they
   never run concurrently for the same collection.

## 2. The shared resources, and where they actually contend

| resource | shared by | contention seen |
|---|---|---|
| **the GIL** | every pipeline thread (one interpreter) | the whole worker was ONE core busy at 87 % with eleven idle — parse/chunk is pure Python (amendment 47) |
| **the manifest write lock** (per collection) | rdf/fts marks, archive flush marks, discovery batches, control writes | an archive importing at 1,400 docs/s holds it in a near-continuous stream of short transactions; other writers wait |
| **the Lance table** (per collection) | the embed step, archive flushes, the index maintainer | a build/refresh marks the collection busy and its vector writers stand down (rdf/fts continue) |
| **the encoders** | GPU: all collections that do not own theirs; CPU: one per collection | GPU work is serial by doctrine; a CPU encoder is idle whenever its collection's archive covers what would be embedded. **The Arc fits ONE model server at a time**, so today the GPU moves between collections BY HAND — see [`gpu-time-slots.md`](gpu-time-slots.md) for the deferred design that makes it serial by construction, in roughly one-hour slots (operator, 2026-09-10; waits for the multi-embedder llama server) |
| **disk** | corpora on the HDD pool, `/data` (manifests, Lance, packs) on NVMe | a 437 MB shard read is ~6 s from the HDD; row-group reads keep it to a few MB |

## 3. The rules — each one paid for

1. **A per-round cost scales with the work available, never with the
   corpus.** If a round's cost grows as a collection grows, it is a bug
   however fast it looks at fixture scale. (CLAUDE.md's first engineering
   rule; violated by amendments 45, 48, 49 and fixed there.)
2. **A pipeline thread never waits without a bound on an optional
   subsystem.** The parse pool hung with its children gone and pubmed
   stopped dead; a worker that cannot finish also cannot honour SIGTERM
   (amendment 51).
3. **Every writer stays in the worker; the pool only computes.** The
   results come back in order, one `(ok, value)` per document, so one bad
   document costs itself and never its batch.
4. **Long work never holds the manifest write lock** — not an encoder
   call, not a fetch, not a chunking pass, not a Lance compaction. The
   transaction opens just before the marks.
5. **Index maintenance is not pipeline work, and it does not stop the
   writers.** Its own thread, one collection at a time; the `busy` flag
   exists only so two maintenance passes never run on one table. Writers
   keep going — Lance is MVCC — and a commit conflict is a deferral
   (`vector_store_contended`), never an error on a document.
6. **A stage that can succeed at doing nothing must not win the round**
   from the stages behind it.
7. **Selection queries are plan-checked AND timed** (§5).
8. **Deferral is not failure.** A busy manifest, an unreachable endpoint,
   a pool that did not answer: the document is untouched and retried; no
   error is written to it.

## 4. What things cost — measured on the reference node, 2026-08-26

> **Read §9 before using these numbers.** They were taken while the
> collections were in DIFFERENT phases (two finished, one parse-bound,
> one archive-bound), so the cross-collection comparisons describe one
> unbalanced snapshot. Per-phase costs (a batch, a query, a pass) are
> sound; anything that looks like a share or a ratio between collections
> is not, and must be re-measured under a balanced load before it
> becomes a constant.

12 cores, 62 GB; pubmed 40M documents, wikivec 22M (35M vectors),
wikipedia 19.4M, gutenberg 74k. Numbers are the point of this section:
when something looks slow, compare against these before theorising.

**Per-document work (parse pool, 11 children)**

| work | serial | pooled |
|---|---|---|
| metadata for a 1,000-document rdf batch | 0.23 s | 0.44 s (pool overhead dominates at this size) |
| fetch+parse+chunk, per document | 2.87 ms | ~0.3 ms effective across 11 children |
| worker CPU, whole node | 232 % (1 Python core) | 830–1115 % |
| pubmed rdf+fts | 35 docs/s | 510–1245 docs/s |

**After the scheduler (amendments 53–54), measured 2026-08-27**

| reading | value |
|---|---|
| worker container CPU | 692–721 % of 1200 %, dipping to ~101 % between phases |
| node load average (12 cores) | 18–20 — the run queue is deep, i.e. saturated |
| GPU encoder container | **0.00 %** |

The operator's target ("CPU close to 100 % when there is not much else to
do") is met, and it is worth being precise about what that means: the node
is saturated with PARSE and INDEX work. The GPU encoder is idle at 0.00 %
because the only collection in the encoder rotation is gutenberg, which is
in `lazy` mode with ~64,000 documents unembedded — so the rotation has one
flow and nothing to give it. A busy CPU and an idle GPU at the same time
is not a scheduling fault; it is a spending decision the scheduler cannot
make. Do not "fix" it by taking CPU work away.

**The gutenberg encoder — where the vectors actually cost their time
(measured 2026-08-27, and the reason "feed the GPU" is not a scheduling
question)**

| reading | value |
|---|---|
| endpoint | `llama.cpp` server, `Qwen3-Embedding-8B-Q4_K_M.gguf`, `-ngl 999 -np 4`, Intel Arc |
| chunk size, this corpus | ~4,000 chars = **~3,528 tokens** per chunk |
| server throughput | ~1,640 tokens/s across 2 concurrent requests (~2.15 s/chunk) |
| `EMBED_CONCURRENCY` | 2 — while the server was started with **4 slots** |
| remaining work | 63,764 documents = **7,182,952 chunks** (avg 113, max 13,003) |
| projection at that rate | **~178 days** |

Three conclusions, each of which cost a measurement:

1. **Batching is not available here, however tempting the numbers look.**
   Batch 16 measured 2.5 s/chunk against batch 1's 7.8 s — and it returns
   WRONG VECTORS: two of four 4,000-char texts came back at cosine 0.73
   against their single-text embeddings, while short texts were identical.
   That is `config.EMBED_BATCH`'s 2026-08-17 warning reproduced on the
   very server it was written about. Never raise it without re-running
   that comparison.
2. **Concurrency measurements taken while the worker is running are
   polluted.** Mine were: the worker holds 2 slots continuously, so a
   "concurrency 4" probe actually asked the server for 6. The flat result
   (conc 2 → 0.277 chunks/s, conc 4 → 0.275) says the server saturates
   near its 4 slots, NOT that concurrency does not help. The worker using
   2 of 4 slots is the readable fact, and it is the cheap thing to fix.
3. **The bottleneck is the model, not the pipeline.** 7.18M chunks ×
   ~3,528 tokens is ~25 billion tokens through an 8B embedder. No
   scheduling change touches that number; only a smaller model, smaller
   chunks, or more time will. Both of the first two change `space_id`
   (invariant 1/3) and mean re-embedding — which is cheapest NOW, at 16 %
   done, and never gets cheaper.

**An archive pass (wikivec, 18,020 documents / 100,000 vectors, ~150 s)**

| phase | cost |
|---|---|
| read the shard's vectors (437 MB parquet) | 6.3 s (15,800 vectors/s) |
| manifest lookups | 0.4 s |
| fetch+parse+chunk (before amendment 50: serial) | 51.7 s |
| the writes: 36 Lance delete+add pairs + 100k ledger rows | ~91 s |

**Vector-table writes**

| operation | without the `book_id` index | with it |
|---|---|---|
| the `book_id IN (500 ids)` predicate a flush deletes with | 0.90 s (wikivec, 35M rows) / 0.59 s (pubmed, 16M) | index probe |
| building the index itself | — | 0.3 s (1.4M rows) … 2.3 s (35M rows) |
| archive import rate, end to end | pubmed ~470/s avg, wikivec ~100/s | pubmed ~1,400/s, wikivec ~250/s |

**Selection queries** (the ones that look indexed and are not)

| query | before | after |
|---|---|---|
| version-driven re-index selection, EVERY round of a caught-up collection | 16.7 s (wikivec), 18.3 s (wikipedia), **127.9 s** (pubmed) | 0.09–0.30 s per window, and the walk ENDS |
| its batch peers (same `OR`) | 19.3 s / 15.9 s | 0.2–7.1 ms (fresh peers are an index prefix) |
| archive "eligible in a shard's id range" | 19.1 s | 1.9 s (browse index; the residue is one row fetch per candidate) |
| archive "pending in a shard's id range" | 9.5 s | 1.1 s |
| prefetch lookahead probe | 10.5 s (scanned from rowid 1) | 0–27 ms |

**Index maintenance**

| | |
|---|---|
| IVF-PQ build, 1.3M rows × 4096-d | 2,568 s |
| IVF-PQ build, 3.1M rows × 768-d | 1,705 s |
| IVF-PQ build, 16.8M rows × 1024-d | 3,788 s |
| refresh (optimize), 942k new rows on 16M | 398 s |
| refresh, 115k new rows on 16.8M (HDD-resident) | 2,448 s |

## 5. The pitfalls catalogue

Each entry: how it looked → what it actually was → the rule. **Read this
list before diagnosing anything; five of the six cost a rework.**

**1. A plan that reads as indexed but is a full walk.**
`SEARCH documents USING INDEX idx_documents_scope (in_scope=?)` means
SQLite took ONE equality off a four-column index and filtered the rest of
the collection. → Time every hot query against a real manifest; a plan
check proves the shape, only a timing proves the cost.

**2. `rowid>?` is not a range unless the index says so.**
`idx_documents_scope` is `(in_scope, rdf_done, fts_done, embed_done)` with
an implicit rowid tail. A rowid bound is usable ONLY when every preceding
column is constrained by equality; otherwise SQLite ignores it and walks
the range (5.4 s per "window"). → Constrain the full prefix, or force the
integer primary key with `+col=1` prefixes, and plan-check it.

**3. `OFFSET k` restarts at the beginning.**
A lookahead probe with `ORDER BY rowid LIMIT 1 OFFSET 40000` re-filters
every document already done to find the frontier. → Anchor on the current
rowid and walk forward.

**4. An `OR` across an indexed and an unindexed column disables the
index.** `(fts_done=0 OR fts_version<?)` cannot use `fts_done`. → Split
the query: the common case as an index prefix, the rare case separately.

**5. A cost that only appears when a collection is FINISHED.**
The re-index selection ran on every round of a caught-up collection to
conclude there was nothing to do. Idle collections are the ones nobody
looks at. → Measure the caught-up path explicitly; if no index can answer
a question, walk it in windows from a cursor and STOP when the walk ends.

**6. A per-pass cost that scales with the shard, not the new work.**
Every archive revisit re-walked its million-row shard, importing less each
time. → Name the eligible documents with one indexed range query and read
only their rows.

**7. Work done inline that could be prepared ahead.**
The first batch to touch a pubmed container built its 17 s pack on the
critical path — 45 % of that pipeline's time — while eleven children
idled. → `prepare`/`prepare_key` and prefetch, fire-and-forget.

**8. A subsystem that hangs instead of failing.**
`ProcessPoolExecutor` with no live children left the pipeline thread
waiting forever: no error, no progress, no SIGTERM. → §3 rule 2.

**9. A lock held across work that does not need it.**
The first index build took the collection's manifest write lock for its
whole duration and blocked every event and commit. → Locks cover the
writes, never the computation.

**10. An optimisation that costs where it cannot pay.**
The prefetch probes ran for every collection, including the four plugins
with no derived cache and on serial nodes. → Check the precondition
before paying for the lookahead.

**11. A safety flag that costs more than the risk it removes.**
Index maintenance marked a collection "busy" and its embed step and
archive pass stood down for the whole window — 41 minutes for wikivec's
70 GB table, reached every few minutes of work, so the collection would
have spent most of its life idle. The risk it was protecting against did
not exist: Lance is MVCC, and a measured 210 concurrent delete+add writes
during `optimize()` produced zero errors. → Measure the risk before
paying for the guard; where optimistic concurrency exists, handle the
conflict as a DEFERRAL instead of preventing the work.

**12. A process-wide cursor keyed on nothing.**
The re-index walk's cursor keyed on `""` when no collection was passed —
and the pipeline loop passed none, so one collection's finished walk
suppressed every other collection's re-index. Caught by a test that had
nothing to do with it. → Cursors, memos and caches key on PHYSICAL
identity (the manifest path), never on a name and never on a default;
and tests reset process-level state between cases (`conftest.py`'s
`fresh_worker_state`).

## 6. Before you change the scheduler — the checklist

1. **Read the worker log as a time series first.** It is the first
   benchmark and it is free; per-collection, per-stage lines with
   durations already exist.
2. **Measure the phases** of whatever you are about to change
   (`tools/sched_report.py --phases <collection>`), and write the numbers
   into the change's commit message. If you cannot say what the current
   cost IS, you are guessing.
3. **Plan-check AND time every query** you add or touch, against a real
   manifest (`tools/sched_report.py --queries`). Reject `SCAN`,
   `TEMP B-TREE`, `LIST SUBQUERY`, and anything over ~100 ms whose cost
   grows with the corpus.
4. **Ask where the same defect sits one layer down.** Six of these
   pitfalls came in pairs (a query and its peers query; a walk and its
   window; a fix and its instrument).
5. **Bound every wait** on anything that can die — pools, endpoints,
   locks — and define the fallback.
6. **Ask what happens when the collection is FINISHED**, empty, or
   errored — the idle path is where the 128 s queries lived.
7. **Write the regression test as a COST test**: count the lookups, the
   row groups, the windows; assert the plan. Behaviour alone never
   catches any of this.
8. **Deploy, then verify on the live node with the code's own code
   paths** — not with SQL you copied into a probe (that mistake reported
   a fix as ineffective when it was not even testing it).

## 7. How to measure

- **`python -m tools.sched_report --queries [collection...]`** — plan +
  timing for every hot query, per collection, flagging bad plans and
  anything slow.
- **`python -m tools.sched_report --phases <collection>`** — where an
  archive pass's time goes (read / lookups / chunking / writes),
  read-only: it replays an already-applied shard, so nothing is written.
- **`python -m tools.sched_report --rates [--seconds 120]`** — per
  collection, documents per second through rdf, fts and embed, sampled.
- **`python -m tools.sched_report --pool`** — parse pool health
  (processes, failures, whether children are disabled).
- **A thread that has gone quiet rather than erroring**: dump it. From
  the NAS, a sidecar sharing the worker's PID namespace:
  ```
  docker run --rm --pid=container:ix-louise-rag-worker-1 --cap-add=SYS_PTRACE \
    louise-rag:local sh -c "pip install -q py-spy && py-spy dump --pid 1"
  ```
  This settled amendment 51 in one shot after two rounds of theorising.
  `docker top <container>` beside it shows whether the pool's children
  exist at all.

## 8. Where the code is

| concern | file |
|---|---|
| the round, stage selection, batching, pipeline threads, prefetch | `app/worker.py` |
| the parse pool | `app/parallel.py` |
| archive passes, the same-function proof, coverage | `app/bundles/genesis.py`, `genesis_fetch.py` |
| vector + `book_id` indexes, maintenance | `app/vector_index.py` |
| the knobs | `app/config.py` (`WORKER_*`, `FTS_BATCH_*`, `RDF_BATCH_DOCS`, `GENESIS_*`, `PARSE_*`, `VECTOR_INDEX_*`) |
| the history and the incidents | `docs/architecture-v2.md` §17 amendments 37, 38, 42, 45, 47–51; `docs/STATUS.md` |

## 9. What happens when everything fires at once

*The operator's question, 2026-08-26: "always ask yourself what happens
if a great number of processes of the same kind fire all together?" —
and the observation that killed the credibility of §4's numbers: the
collections were in DIFFERENT PHASES when I measured (wikipedia and
gutenberg finished, pubmed parse-bound, wikivec archive-bound), so every
cross-collection comparison in this file describes one snapshot of an
unbalanced system, not the machine. Numbers taken that way must never be
turned into constants without re-measuring under a balanced load.*

Today's design has **no global resource model**. Each collection runs its
own thread with local rules, and the resources are shared by accident —
by the OS scheduler, by FIFO queues, by whoever asks first. What that
means when N collections are all busy at once:

| resource | how it is shared today | what happens at N busy collections |
|---|---|---|
| CPU (parse pool) | ONE FIFO pool of `cores−1` children | the collection submitting the biggest batches gets the most children; a 1,000-document batch queues ahead of a 10-document one and delays it by its whole duration. No fairness, no reservation, no preemption |
| CPU (parent, GIL) | every pipeline thread + acquisition + maintainer share one interpreter | whoever holds the GIL wins; a collection doing cheap work in a tight loop starves one doing rare expensive work |
| GPU encoder | serialised on the main loop, one collection per round | fair-ish by round-robin, but a round is a whole document — worst-case latency for a collection is (N−1) × a document's embed time |
| CPU encoders | one endpoint per collection | no contention — but also no way to lend an idle endpoint to anyone |
| fetch bandwidth | central token bucket with priority classes (interactive > delta > backfill > archive > seeding) | the ONE resource that IS budgeted globally today |
| disk | nothing | N archive passes read N × 437 MB shards concurrently; on the HDD pool that is thrash |
| manifest write lock | per collection | no cross-collection effect — correct by construction |
| vector store | per collection, MVCC | no cross-collection effect since amendment 52 |
| index maintenance | one thread, one collection at a time | a 41-minute compaction makes the other collections wait their turn — one slow table delays everyone's index freshness |

So the honest summary: **fetch is scheduled, everything else is
first-come-first-served.** It has held up so far because the collections
have mostly been in different phases — which is exactly the condition
that made my measurements unrepresentative.

**What that looks like on the reference node** (`sched_report --rates`,
120 s, 2026-08-26 — the instrument's first run, and the clearest
statement of the gap):

| collection | rdf/s | fts/s | embed/s | documents still to embed | ETA |
|---|---|---|---|---|---|
| pubmed | 1125 | 1125 | 70.6 | 17,873,169 | 70.3 h |
| wikivec | 0 | 0 | 174.7 | 17,566,801 | 27.9 h |
| gutenberg | 0 | 0 | **0** | 63,060 | never |
| wikipedia | 0 | 0 | **0** | 19,402,204 | never |
| genome-dm6 / hg38 | 0 | 0 | **0** | 2,294 | never |

Six collections have unfinished embedding. Two of them consume
everything; four get nothing — gutenberg's share of the idle GPU is zero,
wikipedia's share of an idle BGE-M3 is zero. Some of that is the
operator's `embed_mode` (a spending decision, correctly theirs), but
NOTHING in the design would give those collections a share even if the
knob said yes: there is no notion of "a collection with unfinished
embedding is entitled to a share of what it needs". That is requirement
B, and its absence is not a tuning problem — it is a missing component.

## 10. The target model, and how today's design measures against it

The operator's model, stated as requirements:

**A. Within one collection**
1. Fetching has the least predictable throughput: never starve it of
   CPU. (Throttling it for the global bandwidth budget is fine.)
2. Spend the budget so that no phase starves for want of input from the
   previous one.
3. Minimise phase-switching overhead: dimension the batches sensibly.
4. Embedding should proceed at a REGULAR pace, not in bursts.
5. Query-related work outranks regular processing.

**B. Across collections**
Every collection whose embedding is unfinished gets an equal share of
each resource **it actually needs** (a collection that uses no GPU is
assigned none). A collection that does not consume its slot has it
progressively reduced, down to a floor, and the freed time is
distributed among the collections that overran theirs — balanced.

The mechanics of that share, stated by the operator:

1. **A plugin manages its own budget internally.** The scheduler hands a
   plugin a share of a named resource; the plugin decides which of its
   own phases spends it. Two levels, not one.
2. **A plugin on hold can claim its full slot the moment it needs it.**
   Being idle costs a plugin its current occupancy, never its
   entitlement.
3. **A plugin can release a resource before its slot expires**, and the
   remainder is redistributed immediately, not at the next round.

### Where today's design stands

| requirement | today | gap |
|---|---|---|
| **A1** fetch never CPU-starved | acquisition runs on its own threads; the parse pool's children are `nice 10`, so fetch outranks them; parse/chunk left the parent (amendment 47), which removed most GIL pressure | satisfied **by accident, not by design**: nothing measures whether fetch is achieving its budgeted rate, and nothing reacts if it is not |
| **A2** no phase starves for input | rdf and fts interleave on a watermark: fts takes the thread only when a full batch is ready, else rdf runs ahead (`_next_book`) | only for rdf→fts. fts→embed has no watermark: the encoder simply takes what exists, and the archive can hold the thread for 300 s while the encoder idles |
| **A3** batch dimensioning | fixed constants: `RDF_BATCH_DOCS=1000`, `FTS_BATCH_DOCS=1000` capped at 20,000 chunks, `APPLY_BATCH_DOCS=500`, genesis pass 300 s | constants, not measurements. A gutenberg fts commit takes 14–37 s (20k chunks), pubmed's 0.4 s (1k chunks) — the same "1000 documents" means two different things |
| **A4** regular embedding pace | none | embedding is bursty by construction: 1,900 docs/s during an archive pass, 0 during the parse phases |
| **A5** query priority | the lazy queue is consulted FIRST in selection, so a query-driven embed jumps the line | that is the only mechanism. A query's embedding call queues behind the worker's at the same endpoint; a query's reader load queues behind the pool's FIFO |
| **B** fair share, need-aware, work-conserving | none. Threads compete; the GPU is round-robin per round | no accounting of what a collection consumed, no slots, no shrinking, no redistribution |
| **B1** plugin manages its own budget | no budget exists to manage: each phase decides for itself, from constants | the phase controller (proposal 3) is the plugin-side half of this, and does not exist yet |
| **B2** an on-hold plugin reclaims its full slot | nothing to reclaim: a collection with nothing to do is skipped and re-tried next round | while the GPU is inside another collection's 300 s archive pass, a newly-ready collection waits for it to end — up to 300 s |
| **B3** release before the slot expires | genesis yields at its 300 s budget or when the range empties, whichever comes first; the pool holds its children for a whole batch | releases are coarse. A phase that finds nothing to do still holds its thread until its own loop decides to return |

### The proposal

A scheduler that owns the *resources*, with the pipeline threads asking
it what to do rather than deciding for themselves. Four parts, each
useful alone and in this order:

**1. Name and account the resources.** A `Resource` per contended thing:
`parse` (pool slots), `index` (tantivy writer time), `encoder:<endpoint>`
(one per endpoint, GPU or CPU), `fetch` (already the token bucket),
`store` (vector writes), `disk:<pool>`. Every unit of work declares which
resource it consumes and for how long it ran — the recorder in
`app/stats.py` already measures most of this; it becomes the scheduler's
input rather than a display.

**2. Deficit round-robin per resource (requirement B).** For each
resource, the collections that NEED it (pending work for that resource
and embedding unfinished) each hold a quantum. A collection runs until
its quantum is spent, then yields. Unused quantum shrinks the next
assignment geometrically to a floor (`min_share`); the freed capacity is
split among collections that exhausted theirs. This is the operator's
rule exactly, and it is a solved algorithm (DRR) — cheap, no clock
skew, work-conserving by construction.
*Concretely:* the parse pool gets per-collection queues instead of FIFO;
the GPU gets a run queue with quanta instead of round-robin-per-round;
the index maintainer serves the collection with the largest
`unindexed × staleness` product rather than list order.

**3. A per-collection phase controller (A2, A4).** Between phases there
are queues whose depth is already in the manifest:
`q(rdf→fts) = rdf_done ∧ ¬fts_done`, `q(fts→embed) = fts_done ∧ ¬embed_done ∧ ¬archive-covered`.
Keep each between a low and a high watermark: run the phase whose
DOWNSTREAM queue is below its low watermark; if all are healthy, run the
phase with the deepest input. Embedding then gets a continuously
non-empty queue, which is what makes its pace regular (A4) — and the
archive's 300 s budget stops being a fixed constant: it yields as soon
as the embed queue is full enough, and keeps the thread when it is not.

**4. Adaptive batch sizing (A3).** Replace the fixed counts with a target
batch DURATION (2–5 s), and size each batch from the collection's own
measured throughput (the same EMA the ETAs use), bounded by the existing
memory caps. Gutenberg then commits ~200 documents and pubmed ~5,000,
both taking about the same time — which is what "minimise switching
overhead" actually means when documents differ by two orders of
magnitude.

**5. Query priority as a real reservation (A5).** Two additions: the
embed client takes a priority argument, and query embeddings hold a
reserved slot at each endpoint (the worker's batch yields between
documents, never mid-call); the parse pool keeps one child free for
query-time work (the reader panel), so a 500-document batch never delays
a reader by 10 s.

### The contract between the scheduler and a plugin (B1–B3)

These three are what makes B's shrinking slot safe instead of punitive,
and they constrain the design more than they look.

**Entitlement is not occupancy.** The ENTITLEMENT is the quantum: a
property of the flow, which changes only when the set of needy
collections changes. What a plugin loses by being idle is its
OCCUPANCY — its place in the rotation and any credit it had accrued —
never its entitlement. The tempting implementation, decaying the share
of whoever is idle and letting them earn it back over rounds, is the bug
this rules out: it punishes exactly the plugins whose input arrives in
bursts (the fetch-driven ones), which is the opposite of A1.

*(Superseded, and worth keeping as a record of a wrong turn: an earlier
draft of this section proposed tracking an explicit LOAN — capacity
lent to others while a plugin is idle, recalled when it returns. Once
re-entry is "at your next turn" rather than "immediately", the loan is
unnecessary: a flow that is not in the rotation simply is not served,
and the others come round faster on their own. There is nothing to
lend, nothing to recall, and no bookkeeping to get wrong.)*

**Re-entry is at the TAIL, and costs one rotation.** A plugin that
released — whether it ran out of work or handed the slot back early —
rejoins behind everyone already waiting. So the worst-case wait for a
collection that has just become ready is the sum of the OTHER flows'
quanta. That number is DERIVED, not chosen: each flow's quantum is at
least its own minimum efficient unit, so the latency follows from
everyone's unit costs. If it is too long, the levers are fewer
concurrent flows or smaller units of work — never a smaller floor, which
would only leave the plugins unable to do anything useful in their slot.

**Demand is announced, not discovered.** "Upon need" means the plugin
signals when it becomes runnable — its fetch returned, its queue crossed
a watermark. A scheduler that learns only by polling makes a plugin that
became ready just after being skipped wait a whole round to say so. The
signal already exists (the inter-phase queue depths of proposal 3); it
has to be pushed on transition rather than read on rotation.

**Releasing early must be free.** A plugin that hands back unused
quantum keeps its entitlement intact — otherwise the rational move is to
burn the slot on make-work, and a scheduler that charges for honesty
gets exactly that. The remainder is redistributed within the SAME round.

**A release needs a yield point.** You can only release at a unit
boundary, so B3's granularity is decided by proposal 4: a 300 s archive
pass, or a 37 s gutenberg fts commit, cannot release. Bounding every
unit to a target DURATION is therefore not merely about switching
overhead — it is the precondition for both B2 and B3, and the strongest
argument for doing proposal 4 early rather than last.

**Churn needs no guard, once re-entry is at the tail.** An earlier draft
worried that release-early plus reclaim-immediately would oscillate, and
proposed a minimum hold to damp it. Tail re-entry removes the problem at
the root: releasing costs you your place, so there is nothing to gain by
releasing and immediately asking again, and a plugin that manages its
own budget (B1) already knows whether it will want the resource shortly.
The guard would have been machinery protecting against an incentive that
does not exist.

### What this changes about measuring

Any of the above must be evaluated under a **balanced** load — every
collection with work in every phase — not the accidental mix that
produced §4. The instrument grows a `--scenario` mode that reports, per
collection AND per phase, what each resource delivered, so a change can
be judged on the general picture rather than on whichever collection
happened to be busy.


## 11. The scheduler as built

`app/sched.py`, wired into the encoder rotation in `app/worker.py`. This
section is what EXISTS; §10's proposal list is what it was measured
against, and the parts still outstanding are named at the end.

### The core (`app/sched.py`)

Deficit round-robin per named resource. A resource is a contended thing —
`encoder:<url>` (per ENDPOINT: two collections on one GPU contend, two on
different containers do not), and in future `parse`, `index`. A flow is
one collection's claim on one resource.

| the model says | how it is implemented |
|---|---|
| equal share of what a collection needs | equal quanta among the flows in the rotation; a collection with no work is not in it |
| unused capacity is redistributed | nothing to redistribute: an idle flow leaves and the others come round faster. Work-conserving by construction |
| a plugin can release early (B3) | `Turn.release(idle=True)` — used for every deferral and every empty queue |
| released, then wanted again → next turn (B2) | re-entry appends at the TAIL; the quantum is untouched, the deficit is reset |
| idle must not cost the entitlement | the quantum is a property of the flow. Only the deficit is reset |
| minimum slot = minimum efficient unit | the floor is MEASURED — p90 of that flow's actual unit costs, after ≥8 samples, capped at `SCHED_FLOOR_CAP_S`. Never guessed, and never below `SCHED_QUANTUM_S` |
| query work outranks regular work (A5) | `reserve()` — served before the rotation, and it neither spends the collection's quantum nor moves its place |

Three things are deliberate and each rules out a plausible wrong version:

1. **A turn that overruns is charged as debt**, and the debt is clamped
   at one quantum. Nothing here can be preempted — a Lance write or an
   encoder call finishes — so the only lever is the NEXT turn. The clamp
   means a single 300 s unit against a 10 s quantum costs its collection
   one turn, not thirty; the oversized unit itself is reported (an
   overrun, plus `suggest_floor_s`) rather than silently absorbed.
2. **Demand is announced, not polled.** The pipeline thread calls
   `demand()` when an fts step completes — that is the moment a document
   becomes embeddable — so a collection that becomes ready right after
   being skipped does not wait a rotation to say so.
3. **A stale flow is readmitted for one probing turn** every
   `IDLE_REPROBE_S`. Announced demand can miss an edge, and a collection
   stranded by a missed signal must cost one delayed probe, never a
   collection that stops for good. The first cut offered that probe only
   when the rotation was EMPTY, which is no safety net at all — a
   collection with work holds the rotation open indefinitely. That is a
   regression test now (`test_a_stranded_flow_is_readmitted_while_the_others_stay_busy`).

### The wiring (`app/worker.py`)

`_serve_rotation` replaces "a fixed budget per collection, in list order,
whether or not it had work". Per pass each flow gets at most one turn and
the loop returns — the caller owns telemetry, the control state and
discovery, and a rotation that never came back up would make all three
wait for it. `_serve_turn` spends the slot over as many units as fit, and
hands it back the moment a step defers or the queue empties.

`SCHED_FAIR_SHARE=0` restores the previous behaviour (`_serve_in_order`)
without a redeploy.

### Reading it back

The worker logs each resource's accounting every `SHARES_LOG_INTERVAL_S`
(stage `sched`), from the stats thread — telemetry is never published by
the thread doing the work. `python -m tools.sched_report --shares` prints
the recent lines. Without this the fair share is an assertion; with it, a
change can be judged on the general picture.

### The rest of the model, as built

The four items §10 listed after the rotation, and the one thing that is
deliberately not ours to build.

**Units sized by duration (`app/sizing.py`, A3).** Fixed counts were the
wrong unit: `FTS_BATCH_DOCS=200` is 0.4 s of work for pubmed and 14–37 s
for gutenberg, so one setting bought two different things and neither was
chosen. A batch is now sized from that collection's own measured cost per
document to last `BATCH_TARGET_S`, clamped, with the configured count
standing until there is a measurement — it never replaces a guess with
another guess. This is also the precondition for B2 and B3 being
fine-grained: a slot can only be handed back at a unit boundary.

The re-index probe needed a different treatment. It is a rowid RANGE and
its cost depends on how many rows the range happens to hold, so there is
no rate to size it from; the width is adjusted from the last probe
instead — halve when it is over target, grow a quarter when it is
comfortably under. AIMD, for the reason TCP uses it: it converges without
a model of what makes the window expensive. Measured live at 498 ms
(pubmed) with a fixed 200k window, on a query that runs every round of a
caught-up collection.

**The phase controller (`worker._archive_yields`, A2 + A4).** An archive
pass holds its collection's thread for up to 300 s, and while it does,
nothing reaches `fts_done` — so nothing new becomes embeddable and the
encoder runs dry. That is the source of A4's burstiness. The rule is one
line and deliberately one-directional: *if the encoder's queue is below
its low watermark and fts has work ready, fts goes first.*

What makes it safe without modelling who needs what: genesis imports
vectors ONLY for documents with `fts_done=1`, so fts is the archive's
upstream as well as the encoder's. Running fts is never wasted for either
of them, so the rule cannot rob the archive in the long run. Both
watermark counts are bounded by a LIMIT inside the subquery and
plan-checked against the scope index.

**The index maintainer's order.** One collection at a time is deliberate
— two hour-long builds must never run together — which is exactly why the
order matters: whoever is served last waits for everyone ahead. It now
uses the SAME rotation as the encoder (`sched.RES_INDEX`) rather than list
order, which gives longest-waiting-first and starvation-freedom without a
second ordering heuristic to get wrong. A collection that is not due
releases idle and drops out instead of holding a place.

**Query priority (A5).** This one could not be solved inside either
process: the query is answered by the WEB process and the backfill runs in
the WORKER, so neither one's in-process scheduler can see the other's
demand. The reservation crosses the boundary through the control table —
`db.claim_query_priority` stamped by the query path before it embeds,
read by the worker between documents, which then stands its own embedding
down and keeps its place in the rotation (yielding for a query is not the
same as having nothing to do). Short TTL, memoised on the read side: at
175 embeds/s an unmemoised check would be a SELECT per document. Losing a
claim costs a query one document's wait; holding one too long costs
everybody, which is the right way round.

**Parse-pool admission — and the part that is NOT ours.** The executor is
FIFO and a submission is ~2 tasks per child, so whichever collection
submits first takes every child, and one whose documents cost 30 s each
holds them for the whole batch. A submission may now occupy at most its
share of the children while other collections are active; alone, it gets
all of them and nothing changes.

That is admission control, not CPU weighting. How the kernel divides CPU
*between* the running children is `cgroups v2 cpu.weight`'s job, and the
right move there is to use it rather than write a second scheduler in
Python — it is kernel-enforced and already proven. Not done here: it needs
cgroup delegation into the container, and the pool spawns children per
task rather than per collection, so it would need partitioning either way.

**Still open, and honest about it:** query priority reserves the ENCODER
but not a pool child, so a reader-panel load can still queue behind a
batch; the archive pass yields only between shards, so B3's granularity
there is still coarse; and if the encoder ever moves from Ollama to vLLM,
A5 becomes a per-request `priority` field on the server and this
reservation should be retired rather than extended.
