# Measuring, benchmarking and testing — the common guideline

Written 2026-08-29 after an encoder trial that produced good numbers slowly
and several wrong ones quickly; kept in a working copy and landed
2026-10-18, with that day's lessons added (§7). Read it before you measure
anything: a wrong measurement is worse than no measurement, because it gets
quoted. The order matters; §1 saves the most time.

## 1. Use the harness that already exists

| what you are measuring | the harness |
|---|---|
| encoder throughput, consistency, bit-stability on the Arc B70 | **`llama-intel-arc-b70`** — `scripts/bench.py`, `capture.py`, `compare.py`, `correctness.sh`, `hash_stability.sh`; curated `fixtures/`; thresholds in `config.env`; results in `baselines/` |
| the same, on the AMD RX 9060 XT | **`llama-amd-rx-9060-xt`** — the B70 harness with the same pools and `history.csv` columns, through LocalAI (`scripts/run_bench.sh`) |
| invariant 12: a `vega-` model bit-identical over fresh loads and orders | `tools/vega_repro_check.py` |
| which inputs a `vega-` model refuses to cut at a window | `tools/vega_cut_scan.py` |
| a collection's `vectors-sample` against a twin on another device | `tools/cross_device_check.py` |
| embed endpoint concurrency, safe settings | `louise dev embedder-profile` (tools/profile_embed.py: production-shaped texts, rejects a setting that wins on speed but drifts on cosine) |
| whether stored vectors still match the endpoint | `tools/vector_compliance.py` |
| tolerance for cross-backend comparison | `tools/calibrate_space.py` |
| scheduler cost and round timings | `louise dev schedule-report` (scheduling.md §7) |
| the card's busy time, idle gaps with work waiting, who held each encoder | `louise dev schedule-report --gpu` (the worker's per-second record, app/rage/card_record.py; the probe's GET /busy, app/gpu_busy.py — not `/root/measure/gpu_busy.py` any more) |
| a person's search under corpus load: each step's wait, failures, what the corpus loses | `python -m tools.measure.query_under_load` (an idle run first, then `--baseline` under load; `--card-record` for the corpus side; gpu-saturation-plan.md I4) |

**The incident.** An encoder A/B was run with a purpose-built harness while
`llama-intel-arc-b70` sat unopened in the same workspace: numbers that could
not be appended to its history, a hand-rolled chunk pool instead of the two
curated ones, and a "self-agreement" metric that re-derived — wrongly — what
the sibling project had already characterised (§3).

**A harness you did not write still needs reading.** Proposing
`MODEL=… ./scripts/bench.py` was wrong three ways: `bench.py` reads no
`MODEL`, starts no container (it benchmarks whatever is at `--url`), and
defaults to the gate pool when `config.env` says throughput uses
`BENCH_FIXTURES`. All three were in its first thirty lines.

## 2. Check before assuming — especially our own documents

- **Is the number counted or inferred?** scheduling.md stated "~4,000 chars =
  ~3,528 tokens per chunk". Counted with the production tokenizer over 40 real
  chunks: **980**. Everything derived from it was off by 3.6x — and the
  correction sat in a working copy for seven weeks while the notes kept the
  wrong figure (corrected there 2026-10-18).
- **Is the "known gap" still a gap?** STATUS said "no vector index"; an
  IVF-PQ index had been built two days earlier.
- **Is the alarm still true?** A build flagged "UNAPPROVED — found in
  production" had been chosen deliberately.
- **Does the file you append to have a column for what you measured?**
  `baselines/history.csv` tracks one model across builds; another model
  belongs in another file (`--history`).
- **When a doc and a measurement disagree, measure again before editing either.**

## 3. Measure the configuration that is deployed

Copy the flags **and the environment** from the deployed spec, not from memory.

- A benchmark that omitted `GGML_SYCL_ENABLE_DNN=0` read 1.7 % fast, on a
  path that is not bit-reproducible. The same variable was later dropped from
  the node's LocalAI altogether (found 2026-10-18; CLAUDE.md invariant 12).
- **Do not let a repeat be served from the slot prompt cache.** The same text
  three times to a single-slot server measures the cache (a vector differing
  by 1.26e-3 per component), not determinism.
- **One job at a time.** A concurrency probe taken while the worker held 2 of
  4 slots reported a flat curve; the flatness was the worker.
- **State the shape.** Throughput depends on chunk length, client
  concurrency, server slots and quantisation; a number without them is not
  reusable.

## 4. Watch the disk — measurement is a writer

- Check free space before you start and while you run (2026-08-29: the pool
  reached 0 bytes). On the dev PC everything lives on D: (C: is full).
- **Artefacts belong on a host path, never inside a container**: a removed
  container took hours of embedding with it.
- Every Lance commit is a version; `VECTOR_INDEX_KEEP_VERSIONS_H` bounds them,
  and `VECTOR_INDEX_MIN_FREE_GB` keeps maintenance from filling the pool.
- **A ZFS clone is a copy for the measurement, and for the backup too.** A
  clone costs nothing to make (copy-on-write, the same bytes — RULE 1's way
  to measure on a production table without touching it), but a clone made
  inside a pool that a TrueNAS periodic snapshot task and a recursive
  replication task cover is snapshotted at their next tick and replicated
  IN FULL. 2026-10-23: two clones of `ssd4/apps/louise-rag` for normattiva's
  refresh measurement; at 14:00 replication "ssd4 → tank/backup/ssd4" began
  sending clone A as a full stream (353 GiB of 1.1 TiB written to tank before
  it was stopped). So: make clones only on a dataset excluded from the
  recursive snapshot and replication tasks (`midclt call
  pool.snapshottask.query`, `replication.query` first), or create and destroy
  them between two ticks. To stop it once started: `core.job_abort` marks the
  job but leaves zettarepl's `zfs send`/`zfs recv` running — stop those too —
  and a clone's auto snapshot blocks a plain `zfs destroy` (`-r` on the clone
  only). TrueNAS also mounts `-o mountpoint=/x` at `/mnt/x` (its altroot).

## 5. Say where every number came from

Label each number **measured**, **modelled** or **published by its author**,
and keep them apart in the same table. A desk model of encoder speed was wrong
by ~5x both ways at once, from a baseline that was itself a pipeline figure
6.4x below the encoder. Cross-check against an independent measurement;
report the interval, not just the point; measure at the depth the system uses
(recall at the reranker's 40, not at 10).

## 6. Before you publish a result

- [ ] Ran on the deployed flags **and** env, stated in the write-up
- [ ] The vendor's deterministic mode on, and bit-exactness measured (invariant 12)
- [ ] Used the existing harness, or said why not
- [ ] Inputs are real production shapes, not synthetic stand-ins
- [ ] One job on the machine; nothing else contending
- [ ] Free space checked before and after
- [ ] Artefacts on a host path, and cleaned up — no container left running
- [ ] Numbers labelled measured / modelled / published
- [ ] A difference CLASSIFIED: wrong (a bug — finite but unrelated, a NaN, a value outside its definition) or arithmetic order (valid last bits): only the first is reported upstream; the second is controlled, never filed (approved-encoders.md "Bugs against arithmetic order")
- [ ] Written where it belongs

## 7. Added 2026-10-18 (the AMD peer)

- **The vendor's deterministic mode is the first setting, not a tuning note**
  (CLAUDE.md invariant 12). The table had been in approved-encoders.md since
  August and was missed twice in one day.
- **`gpu_layers: 0` on a GPU build is not a CPU run.** llama.cpp still sends
  batched matmuls to the card; a CPU twin needs `no_op_offload:true` (and
  the device hidden). A GPU-vs-CPU cosine taken without it was invalid.
- **"Listed by the driver" is not "computing on the card."** amd-smi over
  WSL's /dev/dxg lists every process with a GPU context; the device is read
  from what the load did to VRAM (app/vega/dxg.py).
- **An offline count is not the live engine.** HF tokenizers cut 622 of 622
  over-long inputs exactly; LocalAI's llama.cpp refused 3 of them. Measure
  through the engine that will run.
- **A monitor's default view can lie.** Task Manager's GPU graphs show 3D;
  WSL compute lands on "Compute 0".

## 8. Added 2026-10-30 (the wikidata build)

- **Read a container's CPU from its cgroup (`cpu.stat` `usage_usec`), not
  from a sum over its processes.** A process that exits inside the window
  takes its CPU out of the second sample. With a pool replacing its
  children every few seconds, `/proc` deltas read 135 % and then −1866 %,
  while the cgroup read 982 %. `tools/sched_measure.sh` now reads the cgroup.
- **`ps`/`docker top` %CPU is a lifetime average.** The worker's "37 %"
  was its average since boot, not the window's. Use deltas: the cgroup
  for the whole container, `/proc/<pid>/stat` for one long-lived process.
- **The worker log's batch lines were throttled samples until #78.** A
  run's lines summed to 604 of its 2,000 books. Each line now carries
  the totals of the batches it stands for.
