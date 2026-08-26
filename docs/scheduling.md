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
| **the encoders** | GPU: all collections that do not own theirs; CPU: one per collection | GPU work is serial by doctrine; a CPU encoder is idle whenever its collection's archive covers what would be embedded |
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

## 4. What things cost — measured on the reference node, 2026-08-28

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

*The operator's question, 2026-08-28: "always ask yourself what happens
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

### Where today's design stands

| requirement | today | gap |
|---|---|---|
| **A1** fetch never CPU-starved | acquisition runs on its own threads; the parse pool's children are `nice 10`, so fetch outranks them; parse/chunk left the parent (amendment 47), which removed most GIL pressure | satisfied **by accident, not by design**: nothing measures whether fetch is achieving its budgeted rate, and nothing reacts if it is not |
| **A2** no phase starves for input | rdf and fts interleave on a watermark: fts takes the thread only when a full batch is ready, else rdf runs ahead (`_next_book`) | only for rdf→fts. fts→embed has no watermark: the encoder simply takes what exists, and the archive can hold the thread for 300 s while the encoder idles |
| **A3** batch dimensioning | fixed constants: `RDF_BATCH_DOCS=1000`, `FTS_BATCH_DOCS=1000` capped at 20,000 chunks, `APPLY_BATCH_DOCS=500`, genesis pass 300 s | constants, not measurements. A gutenberg fts commit takes 14–37 s (20k chunks), pubmed's 0.4 s (1k chunks) — the same "1000 documents" means two different things |
| **A4** regular embedding pace | none | embedding is bursty by construction: 1,900 docs/s during an archive pass, 0 during the parse phases |
| **A5** query priority | the lazy queue is consulted FIRST in selection, so a query-driven embed jumps the line | that is the only mechanism. A query's embedding call queues behind the worker's at the same endpoint; a query's reader load queues behind the pool's FIFO |
| **B** fair share, need-aware, work-conserving | none. Threads compete; the GPU is round-robin per round | no accounting of what a collection consumed, no slots, no shrinking, no redistribution |

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

### What this changes about measuring

Any of the above must be evaluated under a **balanced** load — every
collection with work in every phase — not the accidental mix that
produced §4. The instrument grows a `--scenario` mode that reports, per
collection AND per phase, what each resource delivered, so a change can
be judged on the general picture rather than on whichever collection
happened to be busy.

