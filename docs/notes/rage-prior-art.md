# RAGE's load policy: prior art, and the way into LocalAI

*2026-10-01. Research only; no code changed. Operator's ask: which systems
already preload, load and unload models the way RAGE does, so that LocalAI's
scheduler can be extended from an existing template instead of from nothing.*

Every line below was read in source at the commit named, unless marked
UNVERIFIED. The clones are not kept.

## 1. What is being looked for

RAGE's policy is [`rage.md`](rage.md) §7 and §19 (`app/rage/admission.py`,
`app/rage/placement.py`). Split into the features the survey scored:

| id | RAGE does |
|---|---|
| F1 | per-model DECLARED MiB per device; `declared(loaded + loading) + need + reserve <= capacity`; unknown capacity refuses |
| F2 | two budgets on every load: the card's and the host's (a card model holds host RAM too) |
| F3 | observed memory recorded beside the declaration; observation never admits |
| F4 | what processes outside RAGE hold of the card is read and subtracted |
| F5 | evict-on-load: idle only, strictly LOWER priority only, only if the simulated freed amount suffices; lowest tier first, then the least-MiB subset; idleness re-checked under the model's lock |
| F6 | pressure loop (3 s): card squeezed by a stranger → give up idle models, spilled ones first |
| F7 | `preload` through admission; preloaded models restored after a quiet period when there is room |
| F8 | autoload on first request through admission; single-flight per model |
| F9 | refusal = 503 + Retry-After + the ledger; deduplicated event log |
| F10 | device chosen per model at load; per-device argv; load/unload/restart/list API; desired state in files |
| F11 | child processes, `oom_score_adj`, SIGTERM drains |

## 2. The answer

**Nobody does F1 and F5 together.** Every system has a piece. The admission
ledger exists in GPUStack and the Kubernetes scheduler; priority eviction with
the "only if it suffices" check exists only in Kubernetes preemption; a
pressure loop exists in the kubelet and in Lemonade. Nothing surveyed subtracts
a stranger's memory from a DECLARED ledger (F4) or restores preloaded models
after a quiet period (F7). Nothing sets `oom_score_adj`.

So the template is not one system. It is a composition (§5), and LocalAI
already holds about half of the plumbing (§3).

## 3. LocalAI today — `mudler/LocalAI` b9c975e (2026-10-01), MIT, Go

**Single node** (`pkg/model`):

| F | state | where |
|---|---|---|
| F1 | **count**, not MiB: `max_active_backends`, pending loads counted | `watchdog.go` `EnforceLRULimit` :485 |
| F2 | no | — |
| F3 | `xsysinfo.ProcessVRAM(pid)` (DRM fdinfo, process tree) exists; feeds the system page only | `pkg/xsysinfo/process_vram_linux.go` :18 |
| F4 | implicit: the memory reclaimer reads whole-device usage % | `watchdog.go` `checkMemory` :780 |
| F5 | LRU (or largest-file-first, `size_aware_eviction`), skips `pinned` and busy; no priority; no sufficiency check; `concurrency_groups` evict every conflicting model | `collectEvictionsLocked` :564, `EnforceGroupExclusivity` :605 |
| F6 | reclaimer: usage > threshold (0.95) → evict ONE LRU per tick (500 ms) | `checkMemory` :780, `evictLRUModel` :818 |
| F7 | `LOAD_TO_MEMORY` at start; no restore | `core/application/startup.go` :558 |
| F8 | yes: `ml.loading` channel; failure cooldown | `loader.go` :543–560, :166–223 |
| F9 | cooldown → 503 + Retry-After; saturation → 429 (#12113) | `loader.go` :130 |
| F10 | `POST /backend/load`, `/backend/shutdown`; device per model YAML | `core/http/routes/localai.go` :263–267 |
| F11 | graceful shutdown under an exclusive per-model lock, then forced | `loader.go` :595–630, `operation_locks.go` |

**One behaviour is the opposite of RAGE's.** When busy models cannot be
evicted, LocalAI retries 30 × 1 s and then **loads anyway**, over the limit
(`initializers.go` `retryEnforce` :366). RAGE refuses.

**Distributed mode is closer** (`core/services/nodes/router.go`):
`scheduleNewModel` :1067 estimates VRAM (`pkg/vram`: GGUF metadata, HF repo),
filters nodes by reported free VRAM capped by `LOCALAI_VRAM_BUDGET`
(`pkg/vrambudget`), soft-reserves until the next heartbeat, and when nothing
fits evicts ONE least-recently-used idle model (`in_flight = 0`, not pinned,
above the replica floor) per attempt without checking it is enough
(`evictLRUAndFreeNodeFrom` :2121). An estimate of 0 skips the VRAM filter.

Upstream threads worth knowing: #5352 (open) hierarchical LRU/ARC and manual
reclaim; #9375 LRU eviction on memory pressure; #11970 → PR #12026 per-model
DRM VRAM; #9289 "Intel Arc Pro B70 VRAM not parsed" (closed).

## 4. The survey

`y` yes · `p` partial · `n` no · `—` not applicable. Commits read: ollama
1abe35e, llama-swap 42d8a5d, llama.cpp 32dd62e, lemonade 40827a3, gpustack
4a1b963, xinference f9a592f, modelmesh ea13cdc, seldon-core 402619e (and
v2.6.0), triton core 3af839d, kubernetes 6d805eb, ray fd778d5.

| system | licence | F1 | F2 | F3 | F4 | F5 | F6 | F7 | F8 | F9 | F10 | F11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Ollama | MIT, Go | p | p | p¹ | p | p | n | n | y | p | p | p |
| llama-swap | MIT, Go | n | n | p | n | p² | n | p | y | p | p | p |
| llama.cpp router | MIT, C++ | n³ | p | n | p | p | n | p | y | p | p | p |
| Lemonade | Apache, C++ | n³ | n | n | p | p | **y** | n | y | p | y | p |
| GPUStack | Apache, Py | **y** | **y** | **y** | n | n | n | p | n | p | y | ? |
| Xinference | Apache, Py | n | n | p | p | n | n | p | n | n | y | p |
| KServe ModelMesh | Apache, Java | p | n | p¹ | n | p | n | p | y | p | y | — |
| Seldon Core v2 | **BSL ≥ 2.7** | p | n | n | n | p | n | p | y | n | y | — |
| Triton | BSD-3, C++ | p⁴ | p⁴ | p | p¹ | n | n | p | n | p | y | p |
| Kubernetes | Apache, Go | **y** | **y** | — | p | **y**⁵ | **y** | — | — | p | — | — |
| Ray Serve multiplex | Apache, Py | n | n | n | n | p³ | n | n | y | p | p | — |

¹ the observation admits, which RAGE forbids. ² groups or a "matrix" solved
for the least `evict_costs`; no memory. ³ counts models, not MiB. ⁴ the rate
limiter, which gates execution, not loading. ⁵ priority preemption; it
maximises what it keeps, not the fewest MiB given up.

Briefly, from documentation (UNVERIFIED in source): LM Studio (JIT load, TTL,
auto-evict one JIT model), Foundry Local (TTL), TabbyAPI and koboldcpp (one
model at a time). vLLM `/sleep` level 1 moves weights to HOST RAM — an F2 cost
— and SGLang's release/resume refuses unless the server is idle (both read in
source). Research systems: Clockwork (evicts even when the load still will
not fit), ServerlessLLM (a 0/1 knapsack for the cheapest set that frees the
shortfall — Apache-2.0), AlpaServe (no licence), MuxServe and Aegaeon (no
runtime eviction to borrow).

## 5. The template, per feature

| F | copy from | where |
|---|---|---|
| F1 | GPUStack's ledger: claims per device and RAM, `total − Σ claims − system_reserved`, what-if views | `gpustack/policies/resource_view.py` :99–151 |
| F1 | Kubernetes `fitsRequest`: every resource checked, ALL shortfalls returned, `Unresolvable` when the need exceeds capacity — the shape of RAGE's refusal body | `pkg/scheduler/framework/plugins/noderesources/fit.go` :713, :753 |
| F1 | ModelMesh: an UNLOADING model stays charged until the unload is confirmed (`UNLOAD_BUFF`); a failed unload is lost capacity. **Check RAGE against this** | `ModelCacheUnloadBufManager.java` :130, :312 |
| F2 | Triton `AllocateResources`: two passes over every (device × resource), then commit | `rate_limiter.cc` :1085 |
| F3 | GPUStack: `allocated` (claims) beside `used` (driver), only claims schedule | `gpustack/schemas/workers.py` :47–60 |
| F4 | nobody; Ollama's `min(driver free, total − own)` and `waitForVRAMRecovery` (the driver reports freed memory late) are the nearest pieces | `server/sched.go` :1304, :1450 |
| F5 | Kubernetes `SelectVictimsOnNode`: strictly lower priority, remove all, check fit, put the most important back while it still fits; no new preemption while victims are still terminating | `plugins/defaultpreemption/default_preemption.go` :286–427, :451 |
| F5 | ServerlessLLM's knapsack for the least-MiB subset (= RAGE's `least_given_up`) | `sllm/schedulers/storage_aware_scheduler.py` :279–434 |
| F5 | idleness: llama.cpp `pick_victim` (no request in flight, not wanted by the queue); Lemonade's EVICTING mark and `evict_if_committed` re-check | `tools/server/server-models.cpp` :269; `src/cpp/server/eviction_engine.cpp` :184 |
| F6 | the kubelet eviction manager: hard and soft thresholds with grace, a minimum reclaim, ONE victim per interval then wait for cleanup, hysteresis on the condition | `pkg/kubelet/eviction/eviction_manager.go` :256, `helpers.go` :943, :1031 |
| F6 | Lemonade's `GlobalVramMonitor` + `EvictionEngine`, the same loop in a local server | `src/cpp/server/global_vram_monitor.cpp` :41, `eviction_engine.cpp` :59–186 |
| F7 | llama-swap's preload goes through the scheduler like a request; restore-after-quiet has no precedent | `internal/server/api.go` :390 |
| F8 | already in LocalAI; llama.cpp's `server_lru_sched` if requests should queue | `server-models.cpp` :259–388 |
| F9 | llm-d: 429 when capacity exists but is contended, 503 when there is none, plus a reason header | `pkg/epp/requestcontrol/admission.go` :264 |
| F11 | llama-swap: process group, SIGTERM then SIGKILL of the group, shutdown waits for in-flight | `internal/process/process_command.go` |

## 6. Forks of LocalAI that already do it

**One, partly, and not upstreamed.** Method: the 465 forks GitHub lists as
pushed to in the last year; every branch head `git ls-remote`d; heads already
in upstream (branches, tags, every `refs/pull/*/head`) dropped; the remaining
1,987 heads fetched, giving 2,882 commits found in no upstream ref. Of those,
148 touch the scheduler's paths (`pkg/model`, `core/services/nodes`,
`pkg/vram*`, `pkg/xsysinfo`, startup and config) or name its subjects, once
commits whose subject upstream already carries (squash merges) are removed.
Read by subject and files, all but one series are ports, logging, platform
fixes, routing or auth; that series is 44 commits in one fork.

**`matt-edmondson/LocalAI`, branch `custom`** (2026-05-21 → 06-18; spec
`docs/superpowers/specs/2026-06-03-per-gpu-vram-scheduling-design.md`):
per-GPU VRAM scheduling for DISTRIBUTED mode.
- a `node_gpus` table with a soft reservation per GPU, fed by a per-GPU
  heartbeat (F1, per device);
- `planGPUSet` (`core/services/nodes/gpuplan.go` :22): best fit on one GPU,
  else the fewest GPUs that cover the estimate;
- `model_vram_estimates`: heuristic from file size, GGUF metadata, and a
  MEASURED footprint (per-GPU used delta around the load) that overrides
  both — the opposite of F3's rule;
- the estimate is bumped after a failed load, retries are bounded, and a
  terminal error replaces the retry loop;
- the chosen GPUs are enforced with `CUDA_VISIBLE_DEVICES` (NVIDIA only).

No priority, no host-RAM budget, no pressure loop, no change to single-node
mode, nothing for Intel. As a base it is the right shape for distributed mode
(per-device ledger, reservation, bounded failure); for a single node it adds
nothing LocalAI does not already have.

No fork changes the single-node eviction policy (`pkg/model/watchdog.go`).
Other branch names that look relevant are copies of upstream work
(`feat/system-model-vram-11970` in seven forks, `bot/issue-11483-persistent-vram-cache`).

Not covered: forks with no push in a year; commits reachable only from tags.

## 7. What extending LocalAI would mean (proposal, not decided)

Single-node first: RAGE runs on one node, and single-node is where LocalAI
has least.
1. **Ledger admission** in place of the count in `EnforceLRULimit`:
   per-model `memory: {<device>: MiB, cpu: MiB}` and `host: {<device>: MiB}`
   in the model YAML, per-device `capacity` and `reserve`; undeclared falls
   back to `pkg/vram`'s estimate. Shape from GPUStack and `fitsRequest`.
2. **Refuse, don't overcommit**: a `NeedMore` that survives eviction returns
   503 + Retry-After with the ledger, instead of loading after 30 retries.
   This changes LocalAI's default behaviour, so it would need to be opt-in.
3. **Priority eviction**: `priority` in the model YAML; Kubernetes'
   victim selection with RAGE's least-MiB subset inside the deciding tier;
   idleness re-checked under LocalAI's existing per-model operation lock.
4. **Stranger-aware pressure**: `checkMemory` from "usage % > threshold" to
   "free < reserve while foreign ≥ X", foreign = driver used − Σ
   `ProcessVRAM(child)`; the kubelet's one-victim-then-wait rhythm.
5. **Restore**: `LOAD_TO_MEMORY` through admission, and restore after quiet.

LocalAI is MIT, multi-vendor and actively maintained, which fits §18 of
`rage.md`. Each of these is a change to LocalAI's model YAML and defaults;
upstream would see it first as an issue (#5352 is the nearest open one).
