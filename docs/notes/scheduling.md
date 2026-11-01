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
| **main loop** (`run_forever`) | node control state, discovery requests, telemetry publish, and keeping the embed producers matched to the collections (§12; with `WORKER_EMBED_PRODUCERS=0`, the old encoder rotation and bundles) | run a long CPU or IO job |
| **embed producer**, one per collection (`_embed_producer`, nice 0) | that collection's embedding: selection, chunks, held rows, the commit; its requests pass the encoder's gate (§12) | drain bundles or archives, or do anything that does not feed its encoder |
| **pipeline thread**, one per collection (`_pipeline_worker`) | that collection's rdf/fts (rdf also commits each batch's catalog-index entries, app/catalog.py, before the marks), its archive passes, and — when the collection `owns_own_embed` (its encoder is its own container) — its embed step | touch another collection's manifest, index or Lance table |
| **index maintainer** (one thread, `_index_maintainer`, at `nice 10` like the parse pool — and the pylance runtime it creates with it) | the vector index and the `book_id` index of every collection, one at a time; and each collection's catalog-index BACKFILL (app/catalog.py) until it is complete, in rowid windows, `CATALOG_BACKFILL_BUDGET_S` per turn | run on a pipeline thread (a refresh took 41 minutes and idled wikivec), hold a manifest write transaction while it works |
| **stats publisher** | telemetry, the seeding table | do any pipeline work |
| **publisher** (one thread, `_publisher`, every `PUBLISH_BEAT_S`) | for every SHARED collection, where the mode embeds: the open parts whose vectors the node holds, bundled, laddered, recorded and announced (`contribute.once(build=False)`), one collection at a time, each pass bounded by `PUBLISH_BUDGET_S` and resumable; `embed = "p2p"` adds the build, which is the embed stage's | embed anything beyond a bundle's sixteen challenge rows; run on a pipeline thread (a pass is minutes of part reads and hashing) |
| **discovery scan** (one thread per collection, on demand) | enumerating the corpus into the manifest in batches | hold the write lock across a whole scan |
| **acquisition** (background, budgeted) | fetching corpus and archive bytes | bypass the transfer manager's budget |
| **parse pool** (`app/parallel.py`, spawned children at `nice 10`) | fetch/parse/chunk per document, metadata, SHARD record lines, `prepare` prefetches | write ANYTHING — every writer stays in the worker |
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
| **the encoders** | every collection naming the endpoint, through its gate (§12) | the Arc holds eight model servers at once (25.7 of 34.2 GB, 2026-09-18), so "GPU work is serial by doctrine" and "one model server at a time" are stale. What serialised the GPU was the main loop feeding every endpoint in turn: gutenberg's encoder was idle in 233 of 300 s with 78,178 documents waiting (§12) |
| **disk** | corpora on the HDD pool, `/data` (manifests, Lance, pubmed's container packs) on NVMe | a 437 MB archive-shard read is ~6 s from the HDD; row-group reads keep it to a few MB |

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
| chunk size, this corpus | ~4,000 chars = ~~~3,528 tokens~~ → **980 tokens** per chunk (*corrected 2026-08-29, measured*: counted with the production tokenizer over 40 real chunks, 3.96 chars/token; the 3,528 was inferred) |
| server throughput | ~1,640 tokens/s across 2 concurrent requests (~2.15 s/chunk) — *as deployed then*; on the `-np 1` flags deployed 2026-08-27 **3,054–3,132 tokens/s = 3.1–3.2 chunks/s** (*measured*, llama-intel-arc-b70 `baselines/history.csv`) |
| `EMBED_CONCURRENCY` | 2 — while the server was started with **4 slots** (overlapping requests on a multi-slot server corrupt vectors; `-np 1` since) |
| remaining work | 63,764 documents = **7,182,952 chunks** (avg 113, max 13,003) |
| projection at that rate | ~~~178 days~~ → **~26 days** at 3.2 chunks/s (*modelled*; the 178 followed from both errors above) |

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
3. ~~**The bottleneck is the model, not the pipeline.** 7.18M chunks ×
   ~3,528 tokens is ~25 billion tokens through an 8B embedder.~~
   **Corrected 2026-08-29 (measured; landed 2026-10-18): the opposite.**
   At the real 980 tokens per chunk it is ~7 billion tokens, and the
   encoder alone does 3.2 chunks/s against the pipeline's 0.47 — a 6.4x gap
   that is NOT the GPU, the backend or the model. The lever is the
   pipeline. A smaller model or smaller chunks would still change
   `space_id` (invariant 1/3) and mean re-embedding.

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

**1b. A fix applied to one branch of the same function.**
The backfill walk was bounded into windows in September after it was
measured at 105–153 s on pubmed — but only where a collection DECLARES an
archive, the other branch being believed the small case. Three days later
the same collection lost its archive declaration, fell into the other
branch, and burned a core of twelve on a 57.8 s selection that could not
find a row (2026-09-22). → When you fix a cost, fix EVERY branch that
reaches it, or make the cheap branch impossible; and a condition that
decides between a bounded and an unbounded path is itself a defect waiting
for the day the condition flips.

**1c. A poll for something that cannot happen until a restart.**
Even windowed, the version walk re-read the whole corpus every two minutes
for a re-embed candidate that can appear only from a version bump (a code
change, so a restart) or an import (which moves the coverage epoch) —
both of which clear the cursor anyway. → Ask what has to HAPPEN for a
selection to find something, and wait that long, not the default TTL.

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

**13. A per-document commit whose cost lands on every later READ.**
Pubmed's parallel embed made one Lance commit per abstract. Each commit
was cheap (37 ms), but each one left a small fragment the book_id index
does not cover. Every later per-document read scans all such fragments:
503 ms live, 697–918 ms on a clone as they grew (+0.5 ms apiece), until
the hourly compaction reset them. The read was the cost, the encoder sat
~90 % idle, and the stage ran at ~2 documents a second (2026-09-15).
→ Where the unit is small, read and commit per BATCH: one read of what a
group already holds, one commit for what it embedded (`held_chunks`,
`write_pending`). A write's cost includes what it leaves for the reads
after it.

**14. One call doing three jobs, scheduled by the dearest of them.**
`Table.optimize()` welds version cleanup, compaction and the index update
together, and the hourly "prune" called it: 40–51 minutes of ~10 cores per
pass on wikipedia's 155 GiB table, while the half that mattered never ran
— 4.7 M unindexed rows against a 1.34 M threshold, scanned by every search
of that collection. Three passes in two days were also thrown away
("Retryable commit conflict … Rewrite", "Too many concurrent writers"),
because a prune marked nothing busy and the archive import kept committing
underneath a rewrite. Cleanup alone, once reachable, took 18.5 s.
→ Jobs with different triggers are different jobs: cleanup on the
retention window, compaction on fragment count, the index on unindexed
rows. Schedule each on its own, run the cheap one often, and let the one
that rewrites data hold the table while it does (`maintenance()`).
Diagnosing it needs care too: a pass logs nothing until it ends, and
unindexed rows climbing during one rules out only its index phase, never
the compaction before it.

**15. "Its own thread" is not "low priority".** The maintainer was
documented as one low-priority thread, and nothing lowered it. On
2026-09-18 a wikipedia index refresh ran on pylance's runtime threads at
12.2 of the node's 12 cores, at the pipeline's own priority. A thread of
its own isolates ordering and failure. It does not isolate CPU, and the
Rust runtimes underneath fan out to every core.

The same day also shows how easy it is to misread. pubmed's embedding
looked starved (12 docs/s from the log, against 38–41 the day before),
but the pipeline's Python threads wanted about one core and had it, and
the rate did not move once the maintainer was niced. The real limit was
the split of pubmed's one pipeline thread: embedding ran at 31 docs/s in
its windows and fts had the rest, with 17.2M documents left over from the
09-15 re-pin. Measure the thread's wants (`--rates`, per-thread CPU and
wait states) before blaming the neighbour that is busiest.

→ Background CPU runs niced: the parse pool's children at 10, and the
maintainer's thread at 10, set before its first Lance call. Linux nice is
per thread and inherited at spawn, so pylance's runtime is born at 10.
Mind whoever creates a runtime first: lancedb's, which carries the embed
writes, is created on the main thread at boot (`refuse_boot`) and stays at
0. A test checks both from `/proc/self/task`.

**16. A switch that stops new work and not the work in flight.** The
node's modes were redefined on 2026-09-19 — *pause*: nothing runs;
*download/seeding only*: exactly that — and every test passed. Watched
on the node, "download/seeding only" took no new document and the two in
flight went on: the model pool served 470 embedding requests in the next 100 s,
and the largest book is 5.3 h of them. An archive pass did the same, to
the end of its 300 s budget, and a scan to the end of the corpus (1,083
s on wikivec). Each already had a unit it stops at — a slice, a row, a
ref — and each asked only SIGTERM.

→ Whatever a thing stops at for SIGTERM, it stops at for the mode: one
stop object carries both (`_embed_stop`, `_parse_held`), and what it
interrupts is DEFERRED or OWED (`_scan_owed`), never marked and never
dropped. What a mode runs is one table, `app/node_state.py`: `MODE_RUNS`
— ask `node_state.runs(state, activity)`, never the mode's name. And
WATCH A MODE ON THE NODE after deploying it: 153 requests per 30 s
running, 31 in the 40 s after the switch, 0 after that, 108 a minute
after "run everything". The suite could not have shown the first number.

**17. "Idle" read off a signal the work does not move.** The sweep of
on-demand embedders (`_sweep_unheld_embedders`, every settle beat)
unloads a `vega-` model named only by collections that do not embed
everything once LocalAI's `/system` has shown its backend at 0.0 % CPU
for two beats. On Perugia (AMD, DXG) `share contribute` — a process
the worker does not run, on a collection whose mode is "off" — embedded
non-stop at 142 chunks/min with the GPU at 100 % while the backend's
CPU read 0–2.5 %, exactly 0.0 one sample in four. Two zero beats in a
row came every half hour: seven `/backend/shutdown` in 3.4 h, each a
19 s cold start the next request waited out (~1 % of the pass, and a
fresh load each time). Nothing failed, nothing retried, and the log said
"idle". The node has the same case: its gutenberg embeds on demand, and
its nightly contribute paid 12 cold starts in one day.

→ A process embedding outside the worker says so: an ENCODER LEASE in
`state/node.sqlite` (`node_state.lease_encoder`, one row per server,
model and holder, renewed before every batch, `ENCODER_LEASE_TTL_S` =
600 s, released when the command ends; `louise._leasing` wraps the
embed callable of `share contribute` and `hold vectors build`). The
sweep skips a leased model however idle it reads. A lease a dead process
left expires on its own, so the model is freed within the TTL.

→ *The same night, Perugia again:* the CPU figure was never the encoder's.
LocalAI's `/system` `process.cpu_percent` is the VeGa WRAPPER's process
(`python vega_wrapper.py`, 0.07–2 %, 61 MB), not the llama.cpp child's —
which, idle on ROCm under Docker Desktop's dxg bridge, busy-polls at
283 % CPU and 5.4 GB. So "busy == 0.0" was false in both directions: a
busy encoder read idle (above), and an idle one — never exactly 0.0 —
was never unloaded: the operator's machine sat at 35 % CPU with nothing
running, 24 min after the contribute had exited. The CPU signal is gone
from the sweep. "Idle" is now: no lease, and no process on the node has
asked the model anything for IDLE_BEATS beats — the embeddings client
itself writes `encoder_use (server, model, last_used)` in node.sqlite
after every answer (rate-limited to one write per 5 s per model), from
whichever process embeds: the worker, the web's search, a command.

**18. A quadratic nobody saw, in the one Python thread that holds the
GIL for minutes.** Perugia, the first morning as a full peer on
4e63103: the embed stage at 61 chunks/min against the CLI's 143, the
GPU idle, the worker at 98 % CPU — about 20 user and 80 SYSTEM ticks a
second, no I/O. The py-spy sidecar (§7) put the busy thread not in the
embed producer but in `share-fetch`, taking a fetched bundle in:
`accept.take → land.cut → chunks_for_document → _fixed_windows`, line
102, in both dumps a minute apart — the hard split of a paragraph longer
than the window, which re-sliced the REMAINDER once per window (`p =
p[step:]`). A 3.4 MB book without a blank line (CRLF text, verse) is
~1,000 windows and 1.7 GB of fresh multi-megabyte strings, each
page-faulted in: 0.14 s on a Linux host, where the allocator recycles
the pages, and on Docker Desktop over WSL2, where every fault is the
hypervisor's, 18.5 minutes for one 50-book part (06:38:56 → 06:57:24,
38,273 chunks) with the GIL held throughout — so the embed producer,
whose own selection is Python too (`skip_test → coverage.rows`), crawled,
and the gate read "waited 7,412 s, served 3,249 s" with nobody else in
it. And the part was cut TWICE per bundle: once for the ladder's T3
(`text_of`), once again inside `land`.

→ The hard split cuts by index — the same windows, the same bytes, the
same ids (tests/test_chunker_fixed.py holds the old loop as the oracle,
and a `tracemalloc` cost test pins the peak at the text plus a window):
0.005 s for the same book, 27× faster here and the page-fault churn
gone. `land(cut_=…)` takes the cut the intake already made. The lesson
is pitfall 2's again in another coat: a cost that is a constant on the
machine you measured on is a function of the allocator on another, and
"pure Python, fast enough" is a claim about a platform. What is left
open: the cut still runs in the intake thread under the GIL — a few
seconds per part now; if Perugia's AFTER still shows the embed producer
starved while a bundle lands, the cut moves to the parse pool
(`app/parallel.py`, children at nice 10), as the embed stage's own
chunking did in amendment 50.

**19. A pool that runs out of children, and an exit that waits for it.**
München, 2026-10-25: wikipedia-v8's records build was stopped with SIGTERM.
It committed its part, returned from main, and never exited. py-spy showed
the main thread in `concurrent.futures.process._python_exit`, joining two
pool manager threads. No parse child was left in the container, and two
queue feeders were blocked writing to a pipe nobody read. Two causes, both
reproduced on 3.12.3:
- CPython gh-115634. A child that retires at `max_tasks_per_child` is
  replaced only if no stale "idle" token says otherwise. So a 3.12 pool
  runs out of children with parts still queued: 8 parts, 2 children retiring
  after 2 tasks each, 4 answered and 0 children left. The watchdog then
  throws the pool away, and 2026-10-24's 15 pages re-parsed serially in
  18 h fit this cause.
- `shutdown(wait=False)` leaves a pool's manager thread waiting for its
  children: for ever if one is stuck or none is left. The interpreter's
  exit joins every manager thread with no bound.

→ The library's fix is backported where the interpreter lacks it
(`parallel._Executor`). A pool thrown away is STOPPED (`parallel.discard`):
its children are terminated until its manager has ended, and only then is
it shut down. Doing it in the other order, as 3.14's `terminate_workers()`
does, lets the last child retire unreplaced in between: 2 exits in 8 hung,
then 0 in 12. The exit stops the pool before the library joins it. Tests:
tests/test_parallel_exit.py, in this interpreter and in 3.12 when present.
The lesson: an optimisation's WORKERS can disappear too, so a wait on them
needs a bound (§6, 5), and so does the exit that cleans them up.

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
8. **Deploy ONE change, then verify on the live node with the code's own
   code paths** — not with SQL you copied into a probe (that mistake
   reported a fix as ineffective when it was not even testing it). One
   change per deploy: on 2026-09-23 two went out together and the loss hid
   the win, so the good half had to be re-measured alone.
9. **Open a row in [`scheduling-log.md`](scheduling-log.md) before the
   deploy and complete it after.** The BEFORE numbers and the SCENARIO —
   which collections have work and of what kind, the node mode, where in
   the corpus, what else is running — go in first. A throughput number
   without its scenario is not evidence: wikipedia's documents differ by
   two orders of magnitude, so two windows over different parts of the
   corpus are not comparable, and `docs/s` will happily show a win that is
   only a different set of articles. When `docs/s` and `ms/doc` disagree,
   believe `ms/doc`.

## 7. How to measure

- **`python -m tools.sched_report --queries [collection...]`** — plan +
  timing for every hot query, per collection, flagging bad plans and
  anything slow.
- **`python -m tools.sched_report --phases <collection>`** — where an
  archive pass's time goes (read / lookups / chunking / writes),
  read-only: it replays an already-applied archive shard, so nothing is written.
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
| the round, stage selection, batching, pipeline threads, prefetch, embed producers | `app/worker.py` |
| model requests by priority: levels, gates, the interactive claim (§12) | `app/rage/dispatch.py` |
| the parse pool | `app/parallel.py` |
| archive passes, the same-function proof, coverage | `app/bundles/genesis.py`, `genesis_fetch.py` |
| vector + `book_id` indexes, maintenance | `app/vector_index.py` |
| the embedder sweep and the encoder leases that hold it off | `app/worker.py` (`_sweep_unheld_embedders`), `app/node_state.py` (`encoder_leases`), `app/encoder_use.py` |
| the publisher: finished led parts bundled and announced under "all p2p" | `app/worker.py` (`_publisher`, `_publisher_beat`, `_publish_pass`), `app/share/contribute.py` (`once(build=False)`) |
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
| index maintenance | one thread, one collection at a time | a compaction still makes the others wait (40–51 min on a 155 GiB table), but since 2026-09-17 it runs on a fragment trigger rather than hourly, and the version cleanup it used to drag along takes ~18 s |

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
change can be judged on the general picture. **The figures are CUMULATIVE since the worker start** — turns, units and
seconds add up across rounds, and the line says "(since start)" since
2026-10-28, when `pubmed: 99% in 36 turns (1102s, 1 units, units cost
1067.1s)` was read as 1,067 s a round: it was ONE unit, the facets pass
after a worker start, in 36 idle turns.

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

*Amended 2026-09-23, and the amendment is the point.* "While other
collections are active" was measured by PRESENCE — collections seen in a
30 s window — and presence is not work. On the node gutenberg only
**embeds**, and the embed path chunks in-process on its own thread
(`worker._load_chunks`), never through the pool; wikipedia was the only
collection parsing and indexing and was still sized for half the children,
so six of twelve cores had nothing to run on them. Admission now counts
what is **held**: a collection takes every task slot nobody else is
occupying, recomputed on each refill. Alone — the live case — it takes the
whole pool.

The old share survives as the **floor**, because it was protecting
something real: a collection whose documents cost 30 s each would hold
every child for its whole batch, and the collection queued behind it holds
`BEGIN IMMEDIATE` on its manifest the whole time it crawls. So a collection
inside `compute` counts as *asking*, and is never admitted below
`budget // asking` however much the others hold. Convergence is by the
newcomer TAKING slots, not by the incumbent anticipating: the surplus
queues for one wave, and the incumbent then stops refilling past its own
share.

**The part size was changed with it, and that half was WRONG — recorded
because the reasoning is seductive.** A part is the unit of load balancing,
so cutting finer should stop one dear document idling a child, and it looks
free: a part still carries `MIN_PART` rows, so the bytes pickled are the
same bytes. Deployed at eight tasks per child and measured on the node the
same night:

| | before | after 8/child |
|---|---|---|
| wikipedia rdf | 6.3 ms/doc | **28.8** |
| wikipedia fts | 10.2 ms/doc | **27.2** |
| worker CPU | 77 % of 1200 % | 193 % |
| throughput | 54.2 docs/s | **24.3** |

More CPU for less work is **redundant computation** — that much the numbers
settle. *Which* redundancy was not isolated, and this note does not pretend
otherwise; the likeliest is locality, since the cut is by equal COUNT and
wikipedia's documents come out of shared **multistream blocks**, so every
child holding one of a block's documents decompresses that whole block and
four times the parts is four times the re-decompression. (`PARSE_TASKS_PER_CHILD`,
a different knob with a confusingly similar name, recycles a child every
500 tasks; finer parts recycle more often, but ~45 minutes apart is too
rare to be the main term.) Reverted to two per child, and the ceiling is
now a test.

The error in the reasoning is the part worth keeping: **a part's cost is
not independent of the other parts.** "It costs nothing per row, the bytes
pickled are the same bytes" is true and irrelevant when the parts share
work that each one redoes.

Balance can only be bought once the cut FOLLOWS THE BLOCKS — group a
batch's rows by the origin unit they share, then hand whole groups to
children. That is a real change (it needs the plugin to say what a row's
origin unit is) and it is not made here.

Neither of these touches the GPU feed: the embed thread does not use the
pool, and children sit at the worker's nice + 10, so feeding the encoder
outranks parsing by construction (§12).

**The node profile's cores (2026-10-27, `app/cpu_limit.py`).** Admission
has a second ceiling: the profile's `cpu_cores`
(node-profiles-design.md). The pool keeps its children; what is capped is
the parts in flight, `min(window, cores)`, and the page stream now submits
part by part instead of a whole page at once, so the cap holds exactly.
The limit is read once per page or batch, from a cache trusted 30 s or until
the profile's own end. `0` is a pause at that boundary. In the worker it is
`_cpu_paused`, which stands rdf, fts and the archive pass down; embedding,
bundles and scans go on. A prefetch is not counted in any window, so it is
not sent while a limit binds. Wikidata's pack pass (its entity slices and its
passes over the chunk files, two pools of its own) follows the same limit and
pause, and gets the scan's stop through `DiscoverState.stop`.

Measured on a 4-core VM shared with other jobs (7 alternating runs, median;
before = the code without the applier):

| | before | no profile | profile 12 (not binding) | profile 2 |
|---|---|---|---|---|
| records build, 6,000 gutenberg books, 3 children | 3.85 s | 3.98 s | 3.74 s | 4.75 s |
| wikidata pack pass, 30,000 entities, 3 workers | 4.92 s | 4.76 s | 4.95 s | 5.16 s |

The check is within the ±4 % noise. A cached read costs 10.6 µs and a
re-read 0.32 ms. The first records figures (4.18/4.06/4.20/5.01 s, 5 runs)
came from a harness that set things up at import time, which spawned
children re-ran. They were taken again with the setup inside `main()`.

**Not reached by the profile, and why:**
- `lbzip2 -n <workers>` under the wikidata pass: its thread count is fixed
  at start. Its pipe is read only as fast as the limited pool takes slices,
  so it decompresses ahead by a pipe buffer at most, and it sits blocked
  during a pause.
- tantivy's writer threads (fts) and the Lance runtime (the index
  maintainer): native threads, not a pool Python sizes. fts stands down at
  0; the maintainer is not limited.
- `tools/wikidata_coverage.py`: a hand-run tool, not the node's work.
- The `ThreadPoolExecutor`s in app/ wait on HTTP (embedding, transfers,
  retrieval), and embedding runs in every profile (decision 3).

**And the reason it survived six reworks: nothing measured occupancy.** A
starved pool and a slow pool look identical from outside — both show long
batches. The batch log has duration and size, `docker stats` has the
container, and the sizer will faithfully fit a cost model to a machine
running on one core and shrink the batches, which reads as the sizer
working. The four fixes of the night before had moved throughput
19.9 → 64 docs/s *without moving the CPU*; that was the signal, and it sat
unread for a day. `parallel.occupancy()` now integrates how many children
actually had work, and `_log_shares` publishes it every interval:

```
[node info:sched] parse pool — 1.3 of 11 children busy (12%) over 600s
```

A number well below `processes()` while a collection has parse work queued
is the defect, stated. Integrated rather than sampled, because a sample
lands between batches as often as inside one; and counted as
`min(in flight, processes())`, because a task queued behind a busy child is
not a second busy child.

**Where each stage's write lock begins.** Related, and found the same way:
`stage_rdf` held `BEGIN IMMEDIATE` across the whole pool round trip — 51.7 s
per 2,000-document batch — so every other writer of wikipedia's manifest
queued behind a parse. `stage_fts` had been fixed the same way in September
after one chromosome wedged genome-hg38's manifest for 68 minutes. Both now
open the lock themselves, late, once only SQLite writes remain;
`worker._NO_OUTER_TRANSACTION` is what stops `_execute_step` opening one on
their behalf, and a stage missing from that tuple silently gets the old
behaviour, so a test pins the names.

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

## 12. Model work by priority, fed so the GPU does not wait (2026-09-19)

*Operator, 2026-09-19. There are work queues with priorities, lowest to
highest: corpus embedding, translation, chat, reranking, query
embedding. The operator's rules:*

- *A plugin queues up to two batches per priority level, and prepares a
  third when there is more work, so that a returned batch is replaced at
  once.*
- *The threads feeding the GPU outrank indexing and parsing: "the CPU
  load to feed the GPU is trivial but crucial".*

**Amended 2026-10-25 (operator): "Embedding shall have the lower
priority and query processing the highest; the subsequent steps of a query
in process shall be processed first to avoid wasting the effort spent for
the previous steps."**
- The levels are now a query's steps in order: CORPUS < TRANSLATE < QUERY <
  RERANK < CHAT.
- Corpus work stands aside on the WHOLE CARD while a query holds the claim:
  its next request waits, bounded by its own timeout, and wakes on stop.
- The device-wide yield removed on 2026-09-20 returns, because the GPU
  governor now holds the clock that its absence cost chat.
- Why: the night of 2026-10-24 (scheduling-log.md).

### What was measured first

All on the live node, read-only, with the `xe` driver's per-client
`drm-cycles-ccs` counters.

| measured | value |
|---|---|
| gutenberg's encoder (Qwen3-4B), 5-minute trace at 1 Hz | busy **20.7%**, idle in 233 of 300 s, with 78,178 documents waiting |
| the same encoder while a document was running | 95% busy: the GPU is not slow, it is **not fed** |
| the encoder rotation's own accounting over 607 s | gutenberg 268 s (8 documents); **pubmed 250 s, 0 documents**; wikipedia 40 s |
| pubmed's embed selection, one call | 153 s cold, 105 s warm, nothing found (fixed: f5c4f29) |
| query embedding / rerank against a 95%-busy card | 3–10 ms / 19 ms (10 documents): not hurt |
| chat translating a question into 51 languages | 34.3 s idle, 56–192 s with the GPU busy (§STATUS, 2026-09-18) |

Two py-spy dumps taken while the encoder sat at 0% showed the main
thread busy with work that feeds nothing:
- the backfill walk (`_next_embed → _backfill_rows → _cursor_page`);
- an archive drain, in its encoder turn (`_serve_turn → _run_one_step →
  _drain_remote_archive`).

**The cause is structural.** The main loop serves every encoder
endpoint serially, one turn each, and a turn also drains bundles and
archives and runs the selection. So one collection's fruitless turn
idles every other collection's encoder. The doctrine "GPU work is
serial" dates from when the Arc fitted one model server; it runs eight
now (§2's row on the encoders is stale on this point).

### The model

**Levels.** `CORPUS < TRANSLATE < CHAT < RERANK < QUERY`
(`app/rage/dispatch.py`). Every model request carries one.

**One gate per endpoint.** All requests to one model server pass its
gate (`embeddings._request_vectors`, the reranker, chat, translation).
The gate admits at most the endpoint's capacity at a time (1 for a GPU
llama-server, whose `-np 1` serves one sequence anyway; a CPU encoder's
declared concurrency). Admission is strict by level. Within a level it
is fair by service time: the waiter whose plugin has had the least
goes first. A plugin returning from idle starts level with the others
and banks nothing — §10's "idle must not cost the entitlement, nor earn
credit".

**Levels order requests where they meet, at a shared gate, and nowhere
else.** That happens with query and corpus on one encoder, and with chat
and translation when TRANSLATE_URL is unset.
- **No device-wide yield.** For one deploy, corpus work also stood aside
  from the whole card while anything interactive ran (a claim in
  `state/node.sqlite`, read before each request). Measured, it SLOWED
  chat, because alone on the card the Arc drops its clock (below, "Measured
  after deploying"). It was removed on 2026-09-20.
- **Why interactive levels never wait on each other across models.** A
  device-wide strict ladder would make one user's translation wait behind
  another user's minute-long chat answer.
- **What costs chat is the clock, not corpus work.** The claim stays: the
  web holds it while any interactive call runs, and the **GPU governor**
  (`app/gpu_governor.py`, its own container) holds the card's `min_freq`
  at its maximum for the claim's lifetime. Absent, the System tab says so
  (operator: "it will cost only a performance degradation").

**Two batches submitted, a third prepared (the operator's rule).**
- Each collection has an embed PRODUCER thread. It selects candidates,
  loads chunks and reads what is held: all the CPU work of a batch.
- A batch is one document's requests. **Amended 2026-11-01 (GPU plan,
  step 1 lever 2): a batch is one REQUEST, which may carry several short
  documents' texts up to the gated `[encoder] batch`
  (`embeddings.Coalescer`).** wikidata (1.04 chunks a document) sent ~1
  text a request and its card computed 82 % of the time. The rule's
  purpose is kept: still at most `slots` requests per collection at the
  gate, a successor always waiting. What waits now is up to
  EMBED_COALESCE_DOCS documents whose texts fill the next request.
- At most two batches per (collection, level) hold a slot at the gate,
  and a third is prepared meanwhile.
- When a batch returns, its slot passes at once to the prepared one,
  and the producer starts the next.
- The commit (Lance rows, then the marks in the same transaction,
  invariant 2) happens on the producer, off the gate.

So the gate always has a successor waiting, and the GPU sees
back-to-back requests. Across collections on one endpoint the fair
share comes from the gate's service-time order, which replaces the
main loop's per-turn rotation for embedding. `app/sched.py` still
orders the index maintainer.

**Feeders only feed.** Bundle inboxes and archive drains leave the
encoder turn. They run on the collection's pipeline thread, as remote
archives already did.

**CPU priority.**
- The producers and the main loop run at nice 0.
- Pipeline threads (rdf, fts, archive), acquisition and discovery drop
  to nice 5.
- The parse pool and the index maintainer stay at 10.

Nice is per thread on Linux and cannot be lowered back without
CAP_SYS_NICE, which is why the producers are threads of their own. Nice
does not order the GIL. What protects the feeders is that their CPU
work is short and they wait on HTTP with the GIL released, and that
parsing already runs in other processes (amendment 47).

**Rollback.** `WORKER_EMBED_PRODUCERS=0` restores the main loop's
rotation and the pipeline threads' old priority. The gates stay, in the
embedding client. The rotation still yields between documents while the
claim is live, which was its behaviour before any of this.

### As built

| piece | where | proved by |
|---|---|---|
| levels, gates, the claim | `app/rage/dispatch.py` | `tests/test_dispatch.py` |
| every embedding request through its gate | `embeddings._request_vectors`; `embed_texts` hands the caller's level to its pool threads | the same |
| the web's levels | `retrieval` (query, rerank), `llm` (chat; translation by the chat model), `translate` (MADLAD) | `tests/test_phase_control.py` (the query claims before it embeds) |
| producers, 2 submitted + 1 prepared | `worker._embed_producer`, `_embed_parallel(slots=…)` | `tests/test_embed_producers.py`, on the timeline |
| a long document stopped mid-way is a deferral | `_embed_parallel`'s `EmbedInterrupted` branch | the same |
| nice | `worker._background_priority` (pipeline, acquisition, archive fetch, discovery) | the same |
| the gates in the log | `[sched] gate <url> — level: admitted, waited, served` | — |
| the clock held while a person waits | `app/gpu_governor.py`, its own TrueNAS app (`deploy/truenas-gpu-governor.yaml`); the System tab's GPU panel | `tests/test_gpu_governor.py` |

### What to measure after deploying

- The same 5-minute trace of gutenberg's encoder. Target: busy whenever
  documents wait.
- The `[sched]` gate lines: the waits and the service share per level.
- Chat's latency while corpus embedding runs, against the 34 s idle
  figure.

### Measured after deploying (2026-09-20)

**The encoders are fed.**
- gutenberg's encoder over 5 minutes: 82.3% busy (median 87.9%, never at
  0%), against 20.7% before.
- Its gate's own counters from 00:33 to 06:25: busy 19,913 s of about
  21,100 s, which is 94% of wall time.

**The device-wide yield does NOT help chat, and costs it when the clock is
not pinned.** The chat model (Ollama qwen3:8b) translated one question
into 20 languages, called from the web container. With the claim, corpus
work stands aside. Without it (the claim disabled for that call), corpus
work shares the card. Each call was checked for a foreign claim.

| | Arc clock free (min 400 MHz) | Arc clock pinned (min 2800 MHz) |
|---|---|---|
| without claim: corpus shares the card | 65–72 s, mean 2,695 MHz | 67.5 s |
| with claim: corpus stands aside | **87–92 s, mean 990 MHz** | 64.2 s |

- Why: token-by-token decoding leaves the card idle between steps, so
  alone it lets the Arc drop its clock. Corpus work beside it keeps the
  clock at 2,800 MHz. Pinned, every sample was 2,800 MHz or 0 (idle
  between tokens).
- The yield itself works: gutenberg's encoder read 0.1–0.2% busy during
  every claimed call. But even with the clock pinned it buys chat 3 s
  (5%), and it costs corpus its whole share for the length of the call.
- The first run was confounded by the operator's own query, whose claim
  also stood corpus aside: 2 deferrals at 06:30, which were correctly
  deferrals and not errors.

**Consequences:**
- The device-wide yield should go. The ordering at a shared server (query
  before corpus on one encoder) stays.
- The clock is what matters to chat when the card is otherwise idle:
  87 s → 64 s pinned. Pinning while interactive work runs (the claim's
  lifetime) is the operator's proposal. It needs a writer of the host's
  `min_freq`, which no current container may be.
- Operator's note: the card's fan never slows down under the current
  driver, until a process such as Ollama restarts. So pinning costs power,
  not noise.
