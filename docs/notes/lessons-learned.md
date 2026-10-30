# Lessons learned

One entry per case that cost time, newest first. Each entry gives what happened, what it cost, the general lesson, and where the rule now lives. Pitfalls of scheduling stay in [scheduling.md](scheduling.md) §5, and measuring rules in [measuring.md](measuring.md).

## 2026-10-30 — the multi-core day

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
