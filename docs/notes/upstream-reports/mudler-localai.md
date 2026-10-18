# mudler/LocalAI — defects found while running it (one issue each)

Line references and full detail: docs/notes/localai-bugs.md. "Seen" = on a
live node (v4.11.0, Intel Arc Pro B70). "Code" = from reading master, not
reproduced.

**B1 (code) — `transformers` backend, OpenVINO: the device test is always true.**
`if "CPU" or "NPU" in device_map:` is always true, so the GPU-only settings
(`GPU_DISABLE_WINOGRAD_CONVOLUTION`) never apply. Fix: `"CPU" in device_map or
"NPU" in device_map`, and give the inner `if` an `else` (`AUTO:-CPU` would
otherwise leave `ovconfig` unbound).

**B2 (code) — `transformers` backend, OpenVINO: embedding models are re-exported at
every load.** `OVModelForFeatureExtraction.from_pretrained(..., export=True)`
writes to a temporary directory, so OpenVINO's model cache never applies and
every load recompiles. Fix: export once into the models directory, or accept an
IR.

**B3 (code) — watchdog: a backend whose eviction fails stays running, untracked.**
Idle, LRU and busy eviction `untrack` before stopping, and a graceful stop can
fail (`ErrModelBusy` after 30 s). The process then runs outside
`max_active_backends`. Fix: untrack after a successful stop, as `evictLRUModel`
does.

**B4 (code) — memory reclaimer: memory held by another process makes it evict
every model.** `UsagePercent` is the whole device's, so when something outside
LocalAI keeps usage over the threshold, one model is evicted every 500 ms until
none is left. Fix: evict only when LocalAI's own share (`ProcessVRAM`) can bring
usage under the threshold.

**B5 (code) — `LOAD_TO_MEMORY` is ignored with `max_active_backends = 1`.**
`startup.go`: `if options.LoadToMemory != nil && !options.SingleBackend`, and
`SetMaxActiveBackends(1)` sets `SingleBackend`. Nothing is preloaded and nothing
is logged. Fix: preload the first model, or log the skip.

**B6 (code, seen by the fork matt-edmondson/LocalAI) — distributed: eviction does
not check that it frees enough.** One LRU model is evicted, and the new one is
placed on that node whether it fits or not; an estimate of 0 skips the VRAM
filter. Fix: evict until the estimate fits; never treat an unknown size as 0.

**B7 (code) — the loader goes over `max_active_backends` when the models to evict
are busy** (`retryEnforce`: 30 retries, then loads anyway). Fix: answer 503 with
`Retry-After`, as the load cooldown already does.

**B8 (seen) — `POST /models/reload` never forgets a model whose YAML was removed.**
`LoadModelConfigsFromPath` only adds. The model stays listed and loadable until
restart. Fix: drop configs whose source file is gone.

**B9 (seen) — one NaN fails the whole `/v1/embeddings` request.** Go's JSON
encoder refuses NaN, so the response is HTTP 500 and every finite vector is lost.
Fix: check each vector, and answer an error naming the input's index (or retry
it).

**B10 (seen) — a wedged backend is "deleted to recreate it" and keeps running.**
`checkIsLoaded` deletes a local process only `if !process.IsAlive()`, yet logs
"Deleting the process" every 5 minutes; seen for 7 hours. Fix: force-delete
after N failed health checks.

**B11 (seen) — memory reclaimer: a model being loaded is evicted first as
"least recently used".** Its `lastUsed` is zero during the load, so under
pressure the model a request waits for is the one stopped: 500s in a loop while
idle models stay. Fix: set `lastUsed[address] = now` in `AddAddressModelMap`, so
a model being loaded counts as just used (or skip, as busy, an address with a
request waiting on its load).

**B12 (seen) — `intel-transformers` (v4.11.0 gallery image): no OpenVINO model
can load.** optimum-intel 1.15.0 imports `openvino.runtime`, which the
openvino 2026.4.1 beside it no longer has: `ModuleNotFoundError` for every
`OVModel*` type. `intel-rerankers` (optimum-intel 2.2.0) is fine. Fix: pin
optimum-intel ≥ 2.x in `requirements-intel.txt`.

**B13 (code) — `transformers` backend: `Embedding` prints every input in full to
stderr** ("Calculated embeddings for: …"), a whole document per line in the log.
Fix: drop it, or log at debug level with a length.
