# Lessons learned

One entry per case that cost time, newest first. Each entry gives what happened, what it cost, the general lesson, and where the rule now lives. Pitfalls of scheduling stay in [scheduling.md](scheduling.md) §5, and measuring rules in [measuring.md](measuring.md).

## 2026-10-30 — the multi-core day

### A copy that changed every file's mode and nothing else

- **What happened.** The louise-python-20261030 tree was made by `cp -a` of 20261021b into LocalAI's `backends/`. That dataset is nfsv4 with aclmode passthrough, and its directories carry an inheritable `user:apps:…x…:fd` entry. Every entry the copy created inherited it, so 39,548 files read owner-execute (664 → 764, 644 → 744). `chmod` alone could not clear it. Contents were byte-identical and everything would have imported and run. But a tree digest counts each file's exec bit (vega_wrapper `tree_manifest`), so the "same" tree would have been another engine. The same trap was in a rollout's forward path (`install` of three files into an existing package: a file created there reads 744) and in its backout (`cp -a` back from the saved copy) (München; found by checking modes against the source after the build).
- **Cost.** None paid: caught before any model loaded the tree. The backend-tree-drift case of 2026-10-12/13 was the same mechanism read across a reboot.
- **Lessons.**
  - A copy into an ACL'd dataset is not a copy of the modes. Compare modes, and ACL triviality, against the source after every write into `backends/`.
  - The change that passes every functional check and fails only the digest is the one a functional test never catches.
- **Fix.** deploy/vega/switch/backend_files.sh: `bf_record` the modes before writing, then `bf_restore` (strip to a trivial ACL with `nfs4xdr_setfacl -b`, chmod back, check). `bf_copy` copies with `tar --no-acls --no-xattrs`. rollout-110-20261030.sh uses it on its forward path and its backout. The 20261030 tree was stripped and re-moded entry by entry: 0 mode differences and 0 non-trivial ACLs against its source.

### A verdict measured at the setting it was meant to test against

- **What happened.** Every statement on the node read `batch_invariant: true`, and wikipedia's encoder worked at 2–4 % of its gate benchmark, one text a request. The obvious fix was to raise `EMBED_BATCH`. But the gate's `contribute` pass sends texts the way the node contributes, which on a node at `EMBED_BATCH=1` is one text a request. So "batch-invariant" had been measured without a batch, and raising the env would have admitted batched answers no gate had seen. A batching fault once silently corrupted 714 books.
- **Cost.** None paid: caught before the env changed (München's read; this architecture's review).
- **Lessons.**
  - A verdict names the setting it was measured at. A check against it compares that setting with the current one, never the verdict alone.
  - A benchmark names its request shape. The 56,293 tokens/s benchmark was also one text a request, so the live gap is per-request overhead on short texts, not batching. Batching's gain is measured, not inferred.
- **Fix.** The gate keeps the batching it measured each run at, locally (`runs.record_batching`). A run gated at less than the node's current batching is gated again at it before anything batched is written.

### A path rewrite that handled the first path of a compound value, and a node condition written as errors

- **What happened.** After wikipedia-v8 was renamed to wikipedia, the rename re-rooted only the leading path of each locator. Wikipedia's dump locators name two files, `<dump>::s::e|<page_props>::s::e`, so the page_props half still pointed under the old name. The embed recorded `FileNotFoundError` on 85,943 documents, each one out of every selection. Nobody saw it until the embedding looked slow (München; fixed in 954ed47, the manifest repaired in place).
- **Cost.** Hours of the largest collection's embedding, and an investigation that started from "slow".
- **Lessons.**
  - A rewrite of a stored value rewrites every occurrence; its test holds the compound case and a sibling name.
  - A file that is not where the manifest says is far more often the node's condition (a rename, a mount gone, a corpus moved) than the document's. As a silent error it removes documents en masse, and only a human brings them back.
- **Fix (operator's choice: the self-healing error).** The error names its file (`<stage>: origin missing: <path>`) and is said once per path, loudly. The maintainer clears it by itself once the file is back, checked every 10 minutes (`app/origin_missing.py`).

### A thread lock guarding work that moved to processes

- **What happened.** The pubmed pack is built on first use, under a per-container `threading.Lock`, into a fixed `<pack>.tmp`. #80 moved the records build's plan into the pool's processes. Two children built the same pack into the same file, and a third read it truncated ("Parquet file size is 0 bytes"). The plan recorded the third child's documents as skipped, "never fatal". The deploy gate's test caught it on a loaded node; the test had passed alone 3/3.
- **Cost.** One refused deploy. Unseen, it would have been a build silently missing documents wherever a pack is fresh or stale under a pool.
- **Lessons.**
  - When work moves from threads to processes, every lock it takes moves too. A `threading.Lock` around a file is a defect as soon as a second process can reach the file.
  - The standard is an advisory `flock` on the file, then a temporary name of the writer's own.
  - A "recorded, never fatal" skip in a test's data is a failure of the test.
- **Fix.** `derived.building(dest)` guards the pubmed and wikipedia page-props packs. The wikidata regroup refuses a second run on the same pack.

### A cache capped in units whose size the data decides

- **What happened.** #88 shrank the wikidata pack's row groups from 1,000 lines to 64 and raised the per-process line cache from 8 groups to 128. The cap kept the same number of lines only once the pack was regrouped. The build ran first, on the pack as written. Its children grew from ~0.65 GB to 5.7 GB, and the cgroup killed them at 32 g with 12 children, then with 6 (München). The regroup read each 3.6+ GB file whole, so a 12 g cap held two children.
- **Cost.** The build stopped for the length of the regroup. The regroup fell back to one process.
- **Lessons.**
  - Budget a cache in bytes, never in items whose size the data decides.
  - A rewrite streams its input. The output order allows it: carry the rows a group leaves over into the next group.
  - A change whose gain depends on a later migration has to be correct before that migration too.
- **Fix.** `_MAX_GROUP_BYTES` (256 MiB) and a streamed `_regroup_file`.

### A convenience that disabled the recovery

- **What happened.** That morning, `louise.py` set SIGPIPE back to its default so that `| head` would exit quietly. In the afternoon the kernel killed one of the records build's pool children for memory. The parent was still writing that child's task into the pipe, now with no reader. SIGPIPE killed the parent (exit 141) before the pool could hand the child's parts to the remaining processes, the recovery #89 had added. The supervisor halved the pool only on 137, so its next run started with the same 6 children.
- **Cost.** One build run, and a supervisor that did not back off.
- **Lessons.**
  - Python's "Note on SIGPIPE" is the standard: SIGPIPE stays ignored, and a closed stdout is handled where stdout is written.
  - A recovery path is tested by killing a real child, with every signal handler the real process sets.
  - A supervisor's back-off covers every way a kill can surface: 137 for the process it ran, 141 for a parent taken down with its child.
- **Fixes.**
  - `process_exit.run` exits quietly when its stdout is a pipe nobody reads.
  - `supervise-pass.sh` halves the pool on 141 as well as 137.
  - Every pass is first in line for the OOM killer (`--oom-score-adj 500`).
  - `deploy/docker-memory-ceiling.sh` caps all containers together at 90 % of the host, so when caps add up past it, the OOM happens inside Docker and never globally.

### A cursor that returned at the first window holding a candidate

- **What happened.** After its resume, wikipedia embedded one document per batch, about three batches in 10 minutes. Behind them, 18.8 M documents of the encoder's own work were waiting, outside the archive's span. The backfill walk stopped at the first rowid window holding any candidate. In the archive layer, where 21.5 M of 22 M documents were already embedded, that meant one document per call. Each short call then re-queried every archive residual as well.
- **Cost.** The whole encoder backlog of the largest collection, at a few documents an hour.
- **Lessons.**
  - A walk bounded by a time budget fills its batch within that budget. Returning at the first hit is right only where candidates are dense.
  - A cursor resumes after what it returned. Resuming before it re-walks the same sparse ground on every call.
  - A set re-queried on every call must forget what no longer qualifies.
- **Fix.** #91.

### A storage layout sized for the writer's order, read in another

- **What happened.** The wikidata pack stores entity lines in Parquet row groups of 1,000, in dump order. The records build reads them in slice order, family by family. For a sparse family, each record read decompressed a whole group of 1,000 lines to keep one.
- **Cost.** About 29 % of every build child (München's py-spy), on a 75.9 M-record build of ~9 h.
- **Lesson.** When the reader's order is not the writer's, size the storage unit for the sparser reader, or index it. Measure the read amplification: bytes decompressed against bytes used.
- **Fix.** Row groups of 64 (`ROW_GROUP`). A pack already written is regrouped in place: `louise dev wikidata-regroup`, file by file, lines unchanged. `line()` reads each file's own layout.

### A per-document cost hidden in a helper that looked free

- **What happened.** `pack_dir` resolved the pack's place through the /data links (a realpath) and read meta.json, once per document. This happened in the plan (`family_code_at`) and again in `line`, `labels` and `language_codes`.
- **Cost.** The plan took 1 h 48 min for 75.9 M rows on one core (fixed in #79). Afterwards it still cost ~7–10 % of each build child (fixed in #88).
- **Lesson.** Every call on a per-record path is multiplied by the corpus. A helper's cost has to be known before it goes into a loop of 10⁸: resolve, stat and parse once per process, and keep only the cheap check per call.
- **Rule.** CLAUDE.md, "a per-round cost scales with the work available".

### A whole-collection structure built on a per-document path

- **What happened.** Re-embedding 20,000 pubmed documents grew the worker to 46 GB, and the kernel killed it. Each of the four embed threads built the whole held-bundle index, `possession.index` (40 M uids), because the memo had no lock. `record_hashes` and `_fill_content_hashes` had the same shape.
- **Cost.** The worker was killed by a global OOM; the kernel could as easily have chosen LocalAI. The embed measurement was void.
- **Lesson.**
  - No per-document path builds a structure the size of the collection.
  - A per-process cache of anything large is single-flight and budgeted.
  - Ask the cheap question first: does the table already hold the chunks?
- **Fixes.** #85, #86. **Rule.** CLAUDE.md, "Louise survives a crash without an agent": memory caps, restart policies, the pass supervisor.

### Instruments that lie

- **What happened.** Three readings misled us:
  - the throttled batch log summed to 604 of the 2,000 books a run had indexed;
  - `ps`/`docker top` %CPU is an average over the process's whole life;
  - a `/proc` sum over a pool that replaces its children read 135 %, then −1866 %, while the cgroup counter read 982 %.
- **Lesson.** Read the cgroup's counter, take deltas, and use logs that carry their totals. An instrument is checked against a known answer before it is quoted.
- **Rule.** measuring.md §8. `tools/sched_measure.sh` now reads the cgroup.

### A default that only the deployment's env corrected

- **What happened.** `PARSE_TASKS_PER_CHILD` defaults to 500 in code, and the worker's env sets 1e6. The build's own container had no env, so every pool child was replaced every few seconds.
- **Cost.** About 10 % of the build, paid in spawn imports.
- **Lesson.** A setting the deployment overrides must have the right default in code for every process that does not receive that env.
- **Fix.** #84: operator passes default to 20,000.

### Serial passes nobody had measured as serial

- **What happened.** Several passes over a collection ran in one process. Measured cases:
  - `hold check`: 46 min, now ~7 min for six checks;
  - vectors-sample: 17 min;
  - the next snapshot's delta: est. 1.5–2 h;
  - the build's plan.
- **Lesson.** Look for one process at 100 % of a core (`docker top` over deltas, `py-spy`). Hand per-part work to a pool, ordered and bounded (`parallel.each_part`).
- **Rule.** CLAUDE.md, "Multi-threading is a must". The inventory is [multicore-audit.md](multicore-audit.md).

### A speed-up deferred because "it is only 17 minutes"

- **What happened.** The plan's index deferral was written down for later because the plan was one-off. The operator: "ANYTHING can be postponed to deploy and test a performance improvement".
- **Rule.** CLAUDE.md, "Performance is second only to consistency".

### A log line on a command's stdout

- **What happened.** After #78 a one-document batch printed its line. The deposit test's `hold source list --json`, run after an in-process round, read that line ahead of its JSON. The test failed in three full runs and passed alone, depending on the throttle's state.
- **Lesson.** A command whose answer is machine-read must own its stdout. When a test passes alone and fails in the full suite, look for state shared through a module, such as a throttle or a cache.
- **Fix.** #86: a one-document batch is counted and prints nothing.
