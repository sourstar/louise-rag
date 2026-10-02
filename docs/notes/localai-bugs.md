# LocalAI: defects found while studying its scheduler

*2026-10-02. For the operator to report upstream (`mudler/LocalAI`). Found
while mapping LocalAI against RAGE ([`rage-prior-art.md`](rage-prior-art.md)),
by reading its code; nothing here was run against a LocalAI install.*

All line references are to master at
[`53cd6c5`](https://github.com/mudler/LocalAI/tree/53cd6c5716ab5fa608a242dee57562456860094a)
(2026-10-01). Each entry says how sure it is: **certain** (the code cannot
do otherwise), **code reading** (a path the code allows; not reproduced), or
**reported live** (seen on a running system by someone).

**Already reported?** LocalAI's issues and pull requests were searched for
each one on 2026-10-02: none is. The nearest: #12331 / PR #12333 (the idle
watchdog reaping a stale address, fixed) is near item 3 but not it; #5352
(open) asks for LRU/ARC eviction and manual reclaim, not item 4's defect.

| # | where | what | sure |
|---|---|---|---|
| 1 | transformers backend, OpenVINO | the device test is always true: the GPU-only settings never apply | certain |
| 2 | transformers backend, OpenVINO | embedding models are re-exported at every load, so OpenVINO's compile cache never applies | certain |
| 3 | watchdog | a backend whose eviction fails keeps running, no longer counted or evicted | code reading |
| 4 | memory reclaimer | memory held by another process makes it evict every model, one per 500 ms | code reading |
| 5 | startup | `LOAD_TO_MEMORY` is silently ignored with `max_active_backends = 1` | certain |
| 6 | distributed scheduler | it evicts one model and places the new one there whether or not it now fits; an estimate of 0 skips the VRAM check | code reading, reported live in a fork |
| 7 | loader | when busy models cannot be evicted, the load goes ahead over `max_active_backends` | certain; may be intended |

A global compile-cache directory was considered and is **not to be filed**
(operator, 2026-10-02): the per-model caches already exist (§ at the end).

---

## 1. OpenVINO: the device test is always true

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

## 2. OpenVINO: embedding models are converted again at every load

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

## 3. Watchdog: a backend whose eviction fails keeps running, untracked

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

## 4. Memory reclaimer: another process's memory makes it evict every model

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

## 5. `LOAD_TO_MEMORY` is silently ignored with `max_active_backends = 1`

[`core/application/startup.go#L558`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/core/application/startup.go#L558):
`if options.LoadToMemory != nil && !options.SingleBackend {`, and
[`application_config.go#L514-L522`](https://github.com/mudler/LocalAI/blob/53cd6c5716ab5fa608a242dee57562456860094a/core/config/application_config.go#L514-L522):
`SetMaxActiveBackends(1)` also sets `SingleBackend`.

So on a single-GPU node configured as the documentation recommends
(`--max-active-backends=1`, which replaced the deprecated
`--single-active-backend`), `LOAD_TO_MEMORY=mymodel` preloads nothing and
logs nothing: the first request pays the load. Suggested: load the first
model of the list (or the last), or log that the list was skipped and why.

## 6. Distributed: eviction does not check that it frees enough

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

## 7. Loader: the load goes ahead over `max_active_backends`

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

## Not filed: one compile-cache directory for every backend

*Operator, 2026-10-02: not needed — each model is already cached where it
is.* Nobody has requested it either (searched 2026-10-02; the nearest,
#2034, shows optimum-intel's per-model default at work:
`Setting OpenVINO CACHE_DIR to /models/models--…/snapshots/…/model_cache`).

The per-model caches cover it, with two gaps a reader of this file should
know, neither of which a global setting is needed to close:
- **OpenVINO embedding models are never cached** — item 2, which is filed:
  fixed, they get the same per-model cache as the rest.
- **A read-only models directory** (as RAGE mounts `/models`) cannot hold
  optimum-intel's `model_cache`; there the cache must be pointed elsewhere,
  as RAGE does per model (`OV_CACHE_DIR`, rage.md §20).

Intel's driver cache (`NEO_CACHE_DIR`, default `$HOME/.cache`) lasts as long
as the container, which is not a gap worth naming (operator, 2026-10-02):
the kernels compile once per container, in seconds, and a container lives
through many loads.
