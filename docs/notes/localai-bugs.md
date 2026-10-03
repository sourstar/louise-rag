# LocalAI: every action item for the LocalAI team

*One place for everything this project owes or asks `mudler/LocalAI`: the
defects found (B1–B7), the patches we carry (P1), and the requests (R1–R5).
The operator files them upstream. Started 2026-10-02 while mapping LocalAI
against RAGE ([`rage-prior-art.md`](rage-prior-art.md)); the patches and
requests come from [`rage-over-localai.md`](rage-over-localai.md), accepted
2026-10-03.*

| id | kind | what | for us | upstream |
|---|---|---|---|---|
| B1 | defect | transformers backend, OpenVINO: the device test is always true, so the GPU-only settings never apply | — | not filed |
| B2 | defect | transformers backend, OpenVINO: embedding models are re-exported at every load, so OpenVINO's compile cache never applies | — | not filed |
| B3 | defect | watchdog: a backend whose eviction fails keeps running, no longer counted or evicted | availability | not filed |
| B4 | defect | memory reclaimer: memory held by another process makes it evict every model, one per 500 ms | availability: the B70 is shared | not filed |
| B5 | defect | startup: `LOAD_TO_MEMORY` is silently ignored with `max_active_backends = 1` | — | not filed |
| B6 | defect | distributed scheduler: evicts one model and places the new one there whether or not it now fits; an estimate of 0 skips the VRAM check | — (one node) | not filed |
| B7 | defect, may be intended | loader: when busy models cannot be evicted, the load goes ahead over `max_active_backends` | availability | not filed |
| **P1** | **patch we carry** | `GET /system` lists the options each loaded model was given, with their hash | **blocking**: without it RAGE states no run for any LocalAI model | not filed; [`deploy/localai/patches/0001-system-list-load-options.patch`](../../deploy/localai/patches/0001-system-list-load-options.patch) |
| R1 | request | a response header naming the backend instance (pid and start time) that answered | optional: RAGE checks before and after instead | not filed |
| R2 | request | a batched `Embedding` call: one backend call for N texts, where `/v1/embeddings` now makes N in sequence | only if the node measures a loss (rage-over-localai §8.2) | not filed |
| R3 | request | read API keys from a file, not only from `LOCALAI_API_KEY` | only if LocalAI's own UI is published (invariant 8) | not filed |
| R4 | request | admission by declared memory, priority eviction, refusal instead of overcommit (opt-in), eviction aware of other processes' memory, restore after quiet | availability; what RAGE gives up at the cut-over | not filed; #5352 is the nearest open issue |
| R5 | request | a strict mode: LocalAI fills in no load option the model's YAML leaves unset — no GGUF guess, no hardware tuning, no app-wide setting — or says, per option, where each value came from | settings changed in LocalAI's UI would otherwise give runs nobody asked for; we pin instead (rage-over-localai §10) | not filed |

A global compile-cache directory was considered and is **not to be filed**
(operator, 2026-10-02): the per-model caches already exist (§ at the end).

**Before filing a patch.** LocalAI's policy for AI-assisted contributions
(`CONTRIBUTING.md`, `.agents/ai-coding-assistants.md`) forbids an AI agent to
add `Signed-off-by` or a `Co-Authored-By` naming itself. Our patches carry
neither. Whoever submits one applies it with `git am`, sets themselves as
author, adds their own `Signed-off-by` (the DCO is a human's), and completes
the `Assisted-by:` trailer in the form that policy gives.

---

## Defects

All line references are to master at
[`53cd6c5`](https://github.com/mudler/LocalAI/tree/53cd6c5716ab5fa608a242dee57562456860094a)
(2026-10-01). Each entry says how sure it is: **certain** (the code cannot
do otherwise), **code reading** (a path the code allows; not reproduced), or
**reported live** (seen on a running system by someone). Nothing here was
run against a LocalAI install.

**Already reported?** LocalAI's issues and pull requests were searched for
each one on 2026-10-02: none is. The nearest: #12331 / PR #12333 (the idle
watchdog reaping a stale address, fixed) is near B3 but not it; #5352
(open) asks for LRU/ARC eviction and manual reclaim, not B4's defect.

---

### B1. OpenVINO: the device test is always true

[`backend/python/transformers/backend.py#L163-L167`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/backend/python/transformers/backend.py#L163-L167),
and the same block at
[`#L186-L190`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/backend/python/transformers/backend.py#L186-L190):

```python
if "CPU" or "NPU" in device_map:          # ("CPU") or ("NPU" in device_map): always true
    if "-CPU" or "-NPU" not in device_map:  # likewise always true
        ovconfig={"PERFORMANCE_HINT": "CUMULATIVE_THROUGHPUT"}
else:
    ovconfig={"PERFORMANCE_HINT": "CUMULATIVE_THROUGHPUT","GPU_DISABLE_WINOGRAD_CONVOLUTION": "YES"}
```

A non-empty string is truthy, so `"CPU" or …` is `"CPU"`. The `else` branch
is dead: `GPU_DISABLE_WINOGRAD_CONVOLUTION` is never set, for `AUTO:GPU` or
for a `main_gpu` naming a GPU, in both `OVModelForCausalLM` and
`OVModelForFeatureExtraction`.

**Careful with the obvious fix.** Written as intended
(`"CPU" in device_map or "NPU" in device_map`, then `"-CPU" not in … and
"-NPU" not in …`), a device such as `AUTO:-CPU` passes the outer test and
fails the inner one, and `ovconfig` is then never assigned: an
`UnboundLocalError` at `from_pretrained`. The inner `if` needs an `else`.
Which configuration each case should get is the maintainers' call.

### B2. OpenVINO: embedding models are converted again at every load

[`backend.py#L195`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/backend/python/transformers/backend.py#L195):
`OVModelForFeatureExtraction.from_pretrained(..., export=True, ...)`.

- Every load converts the model from its PyTorch weights to OpenVINO IR
  again, in a temporary directory: a slower start each time.
- optimum-intel sets a default OpenVINO `CACHE_DIR` only when the model is
  NOT in a temporary directory and the device is a GPU (optimum-intel 2.2.0,
  `optimum/intel/openvino/modeling_base.py` L443–450). An exported model is
  always in one, so OpenVINO's model cache never applies either: every load
  compiles the model for the GPU again (the GPU driver's own kernel cache,
  where it works, softens only part of that). `requirements-intel.txt` pins
  no version of `optimum[openvino]`.

Suggested: export once into the models directory and load the IR from
there, or accept an IR as it is.

### B3. Watchdog: a backend whose eviction fails keeps running, untracked

[`pkg/model/watchdog.go#L580`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/watchdog.go#L565-L583)
(LRU and group eviction, `collectEvictionsLocked`),
[`#L742`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/watchdog.go#L722-L745)
(idle, `checkIdle`), and L767 (busy, `checkBusy`): the model is `untrack`ed
under the watchdog's lock BEFORE `shutdownEvicted` stops it, and nothing
re-tracks it when the stop fails.

The stop can fail. By default it is graceful
([`process.go#L20`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/process.go#L20):
`LOCALAI_FORCE_BACKEND_SHUTDOWN` unset), waits up to 30 s for calls in
flight (`gracefulShutdownTimeout`, L32), and returns `ErrModelBusy` after
that
([`loader.go#L619-L641`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/loader.go#L619-L641)).
A model chosen as idle can receive a request between its selection and its
stop: a chat generation longer than 30 s is enough.

Then the backend process is still loaded and still answering, but:
- it is no longer in `addressModelMap`, so `max_active_backends` does not
  count it (`EnforceLRULimit`, L493, counts `len(wd.addressModelMap)`), and
  the next load may push the node over its limit;
- idle, LRU and memory eviction never select it again;
- its next requests set `busyTime` for an address `checkBusy` cannot
  resolve, which logs "Address unresolvable" and untracks it again.

The memory reclaimer does it the safe way:
[`evictLRUModel`, L896–L900](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/watchdog.go#L890-L902)
untracks only after a successful stop. Suggested: the same everywhere, or
re-track on failure.

### B4. Memory reclaimer: another process's memory makes it evict every model

[`watchdog.go` `checkMemory`, L776–L810](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/watchdog.go#L776-L810),
and `evictLRUModel` (L814):

```go
aggregate := xsysinfo.GetResourceAggregateInfo()
if aggregate.UsagePercent > thresholdPercent {
    wd.evictLRUModel()      // one model, every watchdog tick (500 ms by default)
}
```

`UsagePercent` is the whole device's, every process's. When something
outside LocalAI holds enough of the GPU to stay above the threshold alone —
another container, a desktop session, a second inference server — evicting
LocalAI's models never brings usage under it, and the reclaimer gives up
one more non-pinned, non-busy model every tick until none is left. They
reload on their next request, and the cycle repeats.

A second, smaller effect: nothing waits for the driver to report the memory
just freed (Ollama's scheduler waits up to 5 s for it, `waitForVRAMRecovery`
in `server/sched.go`). Where the reading lags the process's exit, the next
tick can evict a second model for the same excess.

LocalAI already computes what its own backends hold
(`xsysinfo.ProcessVRAM`, `pkg/xsysinfo/process_vram_linux.go`). Suggested:
evict only when LocalAI's own share can bring usage under the threshold,
and wait for the reading to settle after an eviction.

### B5. `LOAD_TO_MEMORY` is silently ignored with `max_active_backends = 1`

[`core/application/startup.go#L558`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/core/application/startup.go#L558):
`if options.LoadToMemory != nil && !options.SingleBackend {`, and
[`application_config.go#L514-L522`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/core/config/application_config.go#L514-L522):
`SetMaxActiveBackends(1)` also sets `SingleBackend`.

So on a single-GPU node configured as the documentation recommends
(`--max-active-backends=1`, which replaced the deprecated
`--single-active-backend`), `LOAD_TO_MEMORY=mymodel` preloads nothing and
logs nothing: the first request pays the load. Suggested: load the first
model of the list (or the last), or log that the list was skipped and why.

### B6. Distributed: eviction does not check that it frees enough

[`core/services/nodes/router.go` `scheduleNewModel`, L1067](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/core/services/nodes/router.go#L1067):
- **L1177–L1184:** when no node has room, `evictLRUAndFreeNodeFrom` (L2121)
  evicts ONE idle model — the least recently used anywhere in the candidate
  set (L2164) — and the new model is then placed on THAT node, whether or
  not its VRAM now covers the estimate. The model given up may be a small
  one on a node that still cannot hold the new model, while another node
  would have.
- **L1227–L1236:** if the VRAM reservation then fails, the load "proceeds
  without reservation".
- **L1134:** with an estimate of 0 — any model the estimator cannot parse,
  diffusers checkpoints for instance — the VRAM filter is skipped and the
  node is chosen by idleness or load alone.

**Seen on a live cluster** by the author of the fork `matt-edmondson/LocalAI`
(branch `custom`, `docs/superpowers/specs/2026-06-03-per-gpu-vram-scheduling-design.md`):
two 12 GB GPUs, one full, one empty; the estimate 0 for SDXL kept new
diffusers backends on the full GPU, each failed its readiness probe, was
killed and retried. That fork fixes it (per-GPU placement, a heuristic
estimate when parsing fails, bounded retries); its commits are in none of
upstream's pull requests.

Suggested: evict until the estimate fits on the node being freed (or
refuse), and never treat an unknown size as zero.

### B7. Loader: the load goes ahead over `max_active_backends`

[`pkg/model/initializers.go` `retryEnforce`, L366–L388](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/pkg/model/initializers.go#L366-L388):
when the models that would have to be evicted are busy, the loader retries
`LRU_EVICTION_MAX_RETRIES` times (30 × 1 s by default), logs "enforcement
incomplete after max retries", and loads anyway. The limit is then not a
limit: on a GPU sized for N models, the N+1st loads beside the N busy ones,
and the result is an out-of-memory in the backend rather than a refusal the
client can retry. For concurrency groups the code says this is intended
(`watchdog.go`, `EnforceGroupExclusivity`); for the LRU limit nothing says
so. Worth raising as a question: a 503 with `Retry-After` — which LocalAI
already returns for its load cooldown (`ModelLoadCooldownError`) — would
keep the limit.

---

## Patches we carry

The RAGE image builds LocalAI from the base commit in
[`deploy/localai/patches/series`](../../deploy/localai/patches/series) with
these patches applied in order. A patch leaves the series when upstream
merges it, or when a merged change makes it unnecessary.
`tests/rage/test_localai_patches.py` holds the series, the files and this
section to each other.

### P1. `GET /system` lists the options each loaded model was given

*Base: master
[`58830f7`](https://github.com/mudler/LocalAI/tree/58830f7ac508845a6f4efa32cfca06af422d4d82)
(2026-10-02). File: `0001-system-list-load-options.patch`. 9 files, 201
lines added, none removed.*

**Why.** LocalAI completes a model's config before loading it. It guesses
`ContextSize` and `NGPULayers` from the GGUF (`core/config/gguf.go`
L57–L95), applies the hardware auto-tuning, and fills in the application's
runtime settings (`LoadOptionContextSize(appConfig.ContextSize)`,
`model_config_loader.go` L252). What the backend received therefore
appears in no file and on no endpoint, only in a DEBUG log line
(`initializers.go` L194). RAGE must state, with every answer, the options
the answering run was loaded with (09 §9.3 `options`, §9.12). Without them
it states no run, and Louise writes nothing. The distributed registry
already hashes these options per replica (`EffectiveModelOptionsHash`,
`core/services/nodes/router.go` L410); on a single node, neither the
options nor the hash are kept.

**What it changes.**
- `pkg/model/model.go`: `Model.SetLoadOptions` and `Model.LoadOptions`,
  each with its own copy, under the model's lock.
- `pkg/model/initializers.go`: one line. After a successful `LoadModel`,
  the options sent are recorded on the model.
- `core/schema/localai.go`: `SysInfoModel` gains `load_options` (protojson
  with proto field names; unset fields omitted) and `load_options_hash`.
  Both are omitted when this host did not issue the load.
- `core/http/endpoints/localai/system.go`: fills both fields.
- `swagger/`: the two properties, added by hand. Regenerating with
  `swag` v1.16.6, the version `go.mod` pins, also rewrites unrelated
  definitions on master, which is worth telling upstream separately.

**Tests**, Ginkgo, beside the existing ones:
- `pkg/model/load_options_test.go`: a model loaded through an in-process
  gRPC backend records exactly what `LoadModel` received, including the
  per-load fields set just before the call. This spec fails with the
  one-line hook removed. Neither the caller's copy nor the returned copy
  can change what was recorded.
- `core/http/endpoints/localai/system_info_test.go`: `/system` lists the
  options under their proto names, and a hash equal to
  `EffectiveModelOptionsHash`.

**Run** with Go 1.26.0 after `make protogen-go`:
- `go test ./pkg/model/ ./core/schema/ ./core/config/` passes.
- In `./core/http/endpoints/localai/`, the new spec passes. 42 other specs
  fail identically with and without the patch: they need NATS, a database
  or the network.

**What it exposes.** `/system` is an admin route. The options carry no
key: the proxy options name a key's variable or file, not the key. A
model's `env:` is copied into them (`core/backend/options.go` L580),
which the DEBUG log already shows.

---

## Requests

Searched on 2026-10-02 and 2026-10-03: nobody has asked for R1, R2, R3 or R5.
The nearest hit for R3 is #4576 (CORS and API key, closed), a different
problem. For R5, #11022 (closed) made the Ollama-compatible routes honour
the hardware-aware context clamp: the opposite wish.

### R1. Name the instance that answered

On one node a model has one backend process, so RAGE can learn which
process answered by checking its (pid, start time) from `/system` before
and after the request. A response header naming the instance would make
that a single read, and it would stay exact in distributed mode, where a
model has replicas.

### R2. A batched `Embedding` call

`/v1/embeddings` calls the backend once per input, in sequence
(`core/http/endpoints/openai/embeddings.go` L112). llama.cpp's own server
embeds a batch in one pass. File this only if the node's benchmark (03
§3.3, tokens per second) measures a loss against llama-server, which is
rage-over-localai §8.2.

### R3. API keys from a file

Keys come only from `LOCALAI_API_KEY` or `--api-keys`
(`core/cli/run.go` L78). Our invariant 8 keeps tokens out of environment
values. That matters only if LocalAI's own UI is published: inside the
RAGE image LocalAI listens on loopback with no key, and RAGE's front
guards the admin routes.

### R4. The scheduling RAGE gives up

What RAGE did that LocalAI does not, proposed as opt-in changes to the
model YAML and its defaults in [`rage-prior-art.md`](rage-prior-art.md)
§7:
- admission by declared memory per device, with a reserve;
- priority eviction that checks the freed memory is enough;
- a refusal (503 + `Retry-After`) instead of a load over the limit (B7);
- eviction aware of other processes' memory (B4);
- restoring preloaded models after a quiet period.

To be filed as one issue, next to #5352.

### R5. A strict mode for load options

LocalAI fills in any option a model's YAML leaves unset. The values come
from:
- the GGUF (`context_fit.go` L106);
- the detected VRAM, read again at every load, with a failed reading
  taken as 0 (`hardware_defaults.go` L287, `xsysinfo/gpu.go` L170);
- the app-wide runtime settings `threads`, `context_size` and
  `vram_budget` (`runtime_settings_registry.go` L222–L246).

A setting changed in the UI therefore changes how models already
configured run, at their next load. `LOCALAI_DISABLE_HARDWARE_DEFAULTS`
and `LOCALAI_DISABLE_GUESSING` switch off two of these sources but not
the app-wide settings. Ask for either:
- a strict mode, in which an unset field stays at the backend's own
  default; or
- a per-option record of where each value in `load_options` came from
  (P1).

Until then RAGE's models pin every such field
([`rage-over-localai.md`](rage-over-localai.md) §10).

---

## Not filed: one compile-cache directory for every backend

*Operator, 2026-10-02: not needed — each model is already cached where it
is.* Nobody has requested it either (searched 2026-10-02; the nearest,
#2034, shows optimum-intel's per-model default at work:
`Setting OpenVINO CACHE_DIR to /models/models--…/snapshots/…/model_cache`).

The per-model caches cover it, with two gaps a reader of this file should
know, neither of which a global setting is needed to close:
- **OpenVINO embedding models are never cached** — B2, which is to be filed:
  fixed, they get the same per-model cache as the rest.
- **A read-only models directory** (as RAGE mounts `/models`) cannot hold
  optimum-intel's `model_cache`; there the cache must be pointed elsewhere,
  as RAGE does per model (`OV_CACHE_DIR`, rage.md §20).

Intel's driver cache (`NEO_CACHE_DIR`, default `$HOME/.cache`) lasts as long
as the container, which is not a gap worth naming (operator, 2026-10-02):
the kernels compile once per container, in seconds, and a container lives
through many loads.
