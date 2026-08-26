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
