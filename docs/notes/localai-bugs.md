# LocalAI: every action item for the LocalAI team

*One place for everything this project owes or asks `mudler/LocalAI`: the
defects found (B1–B14), the patches we carry (P1–P2), and the requests (R1–R5).
The operator files them upstream. Started 2026-10-02 while mapping LocalAI
against RAGE ([`rage-prior-art.md`](rage-prior-art.md)); the patches and
requests come from [`rage-over-localai.md`](rage-over-localai.md), accepted
2026-10-03.*

*Checked against v4.12.1 (2026-10-27; 308 commits from v4.11.0, read in
the source):*
- *The watchdog and reclaimer are unchanged (`pkg/model/watchdog*.go`), and
  so are `/v1/embeddings` and the llama.cpp `Embedding`, so B3, B4, B9, B11
  and B14 stand.*
- *`backend.proto` only adds fields (+90 lines, nothing renumbered), so a
  v4.11.0 backend still speaks to a v4.12.1 app.*
- *P1 applies cleanly. P2 needs its `prepare.sh` hunk re-based: a context
  line moved and the code hunks are untouched.*
- *v4.12.1 itself fixes one thing, SSRF on user-supplied URLs (#12853).
  It matters only to a LocalAI open to untrusted users, which ours is not.*
- *`LOCALAI_AUTO_UPGRADE_BACKENDS` still defaults to false. Upgrading the
  app alone leaves every installed backend, and so every stated engine
  (invariant 12), as it is.*

| id | kind | what | for us | upstream |
|---|---|---|---|---|
| B1 | defect | transformers backend, OpenVINO: the device test is always true, so the GPU-only settings never apply | — | not filed |
| B2 | defect | transformers backend, OpenVINO: embedding models are re-exported at every load, so OpenVINO's compile cache never applies | — | not filed |
| B3 | defect | watchdog: a backend whose eviction fails keeps running, no longer counted or evicted | availability | not filed |
| B4 | defect | memory reclaimer: memory held by another process makes it evict every model, one per 500 ms | availability: the B70 is shared | not filed |
| B5 | defect | startup: `LOAD_TO_MEMORY` is silently ignored with `max_active_backends = 1` | — | not filed |
| B6 | defect | distributed scheduler: evicts one model and places the new one there whether or not it now fits; an estimate of 0 skips the VRAM check | — (one node) | not filed |
| B7 | defect, may be intended | loader: when busy models cannot be evicted, the load goes ahead over `max_active_backends` | availability | not filed |
| B8 | defect | `POST /models/reload` never forgets a model whose YAML was removed: it lists and serves it until LocalAI restarts | RAGE's `DELETE` answers 502 and says so | not filed; seen on a real LocalAI, 2026-10-03 |
| B9 | defect | `/v1/embeddings`: a vector holding a NaN fails the whole request with 500 ("json: unsupported value: NaN"), losing every other vector and the reason | Louise retries it as the null vector llama-server sends (`embeddings._numeric_failure`) | not filed; seen on the node, 2026-10-04 (15c) |
| B11 | defect | memory reclaimer: a model being loaded has a zero `lastUsed` and is evicted first, as the "least recently used" — under memory pressure the model a request is waiting for is evicted every time, the request fails with 500 | availability: every Ask answer failed with 500 for minutes (2026-10-14) | not filed; seen on the node with v4.11.0, log and source in §B11 |
| B12 | defect | `intel-transformers` (v4.11.0's, as the gallery installed it 2026-10-05): optimum-intel 1.15.0 imports `openvino.runtime`, which the openvino 2026.4.1 beside it no longer has, so EVERY `OVModel*` type fails to load (`ModuleNotFoundError`, checked on the node 2026-10-17). `intel-rerankers` has optimum-intel 2.2.0 and is not affected | none since louise-python (docs/notes/backend-consolidation.md, U4) | not filed |
| B13 | defect | transformers backend, `Embedding`: prints every input in full to stderr ("Calculated embeddings for: …"), a whole document per line in LocalAI's log | — (U3) | not filed |
| B14 | defect | llama.cpp backend, `Embedding`: ignores `EmbeddingTokens`, which `/v1/embeddings` fills when given token ids; it embeds the empty text beside them and refuses it ("Input content cannot be empty"), so no client can embed tokens | **blocking** for a space that cuts by tokens: without token ids an input cannot always be cut to exactly its first N tokens (3 of 512 at 1,024 on the AMD peer); P2 | not filed; found 2026-10-18, source in §B14 |
| B15 | defect | `v4.12.1`: backend downloads are staged at `${basepath}/downloading` = `//downloading`, unwritable by LocalAI's user, so every backend install and auto-upgrade fails | backend updates (the operator wants them); set `LOCALAI_DOWNLOAD_STAGING_PATH` | not filed; §B15 |
| B16 | defect | llama.cpp backend (llama-server's code): never runs the encoder of an encoder-decoder model; a T5-family GGUF is decoded as if decoder-only and answers garbage (`<2cs><2cs>…`) | MADLAD cannot run on llama.cpp, so it runs on torch (17–38 s cold); P3 | upstream llama.cpp #5763 / #26565, no fix; §P3 |
| **P1** | **patch we carry** | `GET /system` lists the options each loaded model was given, with their hash | none since VeGa: the wrapper receives `LoadModel` itself (vega.md §1). It was blocking for RAGE; kept for upstream | not filed; [`deploy/localai/patches/0001-system-list-load-options.patch`](../../deploy/localai/patches/0001-system-list-load-options.patch) |
| **P2** | **patch we carry** | llama.cpp backend: `Embedding` embeds `EmbeddingTokens`, with the special tokens a text gets (B14) | **blocking** for the token cut: VeGa's wrapper sends a cut as its first tokens to a backend that embeds them (`Gate._probe`) | not filed; [`deploy/localai/patches/0002-llama-cpp-embed-token-ids.patch`](../../deploy/localai/patches/0002-llama-cpp-embed-token-ids.patch) |
| **P3** | **patch we carry** | llama.cpp backend: the encoder of an encoder-decoder model runs (B16), as a llama.cpp patch in the backend's own `patches/` | MADLAD on llama.cpp (operator, 2026-10-27: "we need anyway a patched llama.cpp") | not filed; [`deploy/localai/patches/0003-llama-cpp-encoder-decoder.patch`](../../deploy/localai/patches/0003-llama-cpp-encoder-decoder.patch) |
| **P4** | **patch we carry** | llama.cpp backend: encoder-decoder slots are encoded together, in WAVES, and decode in one batch (on P3, which ran one slot) | MADLAD on llama.cpp at torch's speed: a question's 7 languages in one wave instead of 7 × 3 s | not filed; [`deploy/localai/patches/0004-llama-cpp-encoder-decoder-waves.patch`](../../deploy/localai/patches/0004-llama-cpp-encoder-decoder-waves.patch) |
| **P5** | **patch we carry** | llama.cpp backend (ggml-sycl): the batched F16 matmul converts a non-contiguous src1 for the GEMM that RUNS — the oneDNN flat copy only when oneDNN's GEMM runs, the gather for oneMKL (`GGML_SYCL_ENABLE_DNN=0`); before, the KQ product of two or more decoder tokens over an F16 K cache read the wrong queries (the P4 card fault, upstream-bugs.md S1) | the root cause of the wrong text in waves, found by code reading 2026-10-28 and matched by the 32-case pattern of upstream's own suite; the F16 KV cache back for MADLAD (−44 % per token against the f32 workaround) once the rebuilt tar passes its gates | **to file at ggml-org/llama.cpp as a PR** ([upstream-reports/ggml-llama-cpp-sycl.md](upstream-reports/ggml-llama-cpp-sycl.md), second section); [`deploy/localai/patches/0005-llama-cpp-sycl-batched-mul-mat-src1.patch`](../../deploy/localai/patches/0005-llama-cpp-sycl-batched-mul-mat-src1.patch) |
| **P6 (staged)** | **patch staged, not in the series yet** | llama.cpp backend: the encoder output is kept PER SEQUENCE (`llama_memory_enc_dec`, stage 1 of llama-cpp-encoder-decoder.md), so a slot is encoded while others decode; on P3 and P4 | a late request joins the next decode step instead of waiting for the wave; the upstream shape (no public API) | not filed; [`deploy/localai/patches/staged/0006-llama-cpp-encoder-decoder-memory.patch`](../../deploy/localai/patches/staged/0006-llama-cpp-encoder-decoder-memory.patch); moves into the series after its card gates |
| **P7 (staged)** | **patch staged, not in the series yet** | llama.cpp backend: the encoder outputs live ON THE DEVICE (stage 2: a persistent store written by the encoder graph, read by the decoder's `ggml_get_rows`; no host copies), on P5 | the shape upstream asked for (a persistent buffer, no host copies); the per-step upload gone | not filed; [`deploy/localai/patches/staged/0007-llama-cpp-encoder-decoder-store.patch`](../../deploy/localai/patches/staged/0007-llama-cpp-encoder-decoder-store.patch); moves into the series with P5 after the card gates |
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

### B8. `/models/reload` never forgets a removed model

*Seen on a real LocalAI at 58830f7 with P1 (RAGE's smoke test,
docs/notes/rage-over-localai.md §11), and certain from the code.*

[`ReloadModelsEndpoint`](https://github.com/mudler/LocalAI/blob/58830f7ac508845a6f4efa32cfca06af422d4d82/core/http/endpoints/localai/edit_model.go#L123-L140)
calls `LoadModelConfigsFromPath`, whose non-strict path only sets
`bcl.configs[c.Name]` for each YAML it finds
([`model_config_loader.go` L849–L930](https://github.com/mudler/LocalAI/blob/58830f7ac508845a6f4efa32cfca06af422d4d82/core/config/model_config_loader.go#L849-L930)).
A model whose file was deleted stays in `GET /v1/models` and stays loadable
until LocalAI restarts. Nothing says it is a model without a file.

Suggested: on reload, drop a config whose source file lies in the models
path and no longer exists. `LoadModelConfigsFromPathStrict` already builds
an authoritative snapshot; the reload endpoint could use those semantics.
`/models/delete/:name` is not the answer: it is the gallery's, and it
deletes the model's files too.

---

### B9. A NaN in one vector fails the whole `/v1/embeddings` request

*Seen on the node at 58830f7 with P1, the SYCL f16 llama.cpp backend of the
same commit, bge-m3 on an Arc Pro B70 (migration step 15c, 2026-10-04).*

Two of three passes of a 512-text calibration set, sent in batches, ended
in HTTP 500 `json: error calling MarshalJSON for type schema.Item: json:
unsupported value: NaN`. The same texts asked again, alone or in the same
batches, came back finite: the NaN is intermittent and comes from the
backend. Go's `encoding/json` cannot encode a NaN or an Inf in a `float32`
slice, so the response is lost whole: every finite vector in the request,
and which input was bad.

llama-server, on the same card and weights, sends such a vector as nulls,
which a client can recognise and retry. Suggested: check each embedding
for non-finite values before encoding, and either answer an error naming
the input's index, or retry that input once in the backend. Either way the
client learns which text failed rather than losing the batch.

### B10. A wedged backend is "deleted to recreate it" and keeps running

*Seen on the node with stock v4.11.0-gpu-intel, 2026-10-10/11: a Python
backend (louise-medcpt-article-ov) whose four gRPC worker threads were
deadlocked inside OpenVINO.*

The health check failed every 5 minutes for 7 hours, and each time the log
said `GRPC Model not responding … Deleting the process in order to recreate
it`. The process was never ended: the same pid ran on, `/system` kept
listing the model as loaded, `/backend/shutdown` hung past 30 s, and every
`/v1/embeddings` waited until the client's timeout. Ending it by hand
(SIGTERM, then SIGKILL) and loading again worked.

Why, from the source (`pkg/model/loader.go`, `checkIsLoaded`, master
2026-10-11): after the warning, a LOCAL process is deleted only
`if !process.IsAlive()`. A live, wedged backend is logged as "deleting" and
left alone, every time. Suggested: when the health check fails and the
process is alive, force-delete it after N consecutive failures (the busy
watchdog's `ShutdownModelForce` already does the killing), or at least do not
log "Deleting" when nothing is deleted.

**What already covers it:** the busy watchdog (`watchdog_busy_enabled`,
`watchdog_busy_timeout`, default 5m; ON on the node since 2026-10-12 at 10m — its first second killed a pubmed encoder hung for 8 h, and the next call loaded it again). It times each
backend from its oldest in-flight request, ignores pinning, and kills with
`ShutdownModelForce` (go-processmanager: SIGTERM, then SIGKILL after the
graceful timeout). STATUS.md "Open after the migration" weighs turning it
on.

---

### B11. Memory reclaimer: a model being loaded is evicted first, as the "least recently used"

*Reported live, and certain from the code. Seen on the node with stock
`quay.io/go-skynet/local-ai:v4.11.0-gpu-intel`, 2026-10-14 (Intel Arc Pro
B70, 34 GB).*

**What happened.** Every chat request failed with HTTP 500. The GPU stood
at 79.2 % against the reclaimer's 68 % threshold. Each time the chat model
was loaded for a waiting request, the reclaimer chose it as the
least-recently-used model and stopped it about 5 seconds into its load;
the request got 500 and the next one started the cycle again. Six cycles
in 40 seconds, while idle models stayed loaded. The log, ANSI colours
stripped:

```
09:06:28.788 WARN [WatchDog] Memory usage exceeds threshold, evicting LRU backend type="GPU" usage_percent=79.2167126944692 threshold_percent=68
09:06:28.789 INFO [WatchDog] Memory reclaimer evicting LRU model model="louise-chat-qwen3-8b" lastUsed=0001-01-01T00:00:00Z
09:06:37.239 INFO Backend process stopped id="louise-chat-qwen3-8b" address="127.0.0.1:45373" process="run.sh" exitCode="-1"
09:06:37.340 INFO BackendLoader starting modelID="louise-chat-qwen3-8b" backend="intel-sycl-f16-llama-cpp" model="weights/chat/qwen3-8b.gguf"
09:06:37.820 INFO [WatchDog] Memory reclaimer evicting LRU model model="louise-chat-qwen3-8b" lastUsed=0001-01-01T00:00:00Z
09:06:42.574 INFO Backend process stopped id="louise-chat-qwen3-8b" address="127.0.0.1:39129" process="run.sh" exitCode="-1"
09:06:42.649 INFO HTTP request method="POST" path="/v1/chat/completions" status=500
09:06:44.677 INFO BackendLoader starting modelID="louise-chat-qwen3-8b" …
09:06:44.689 INFO [WatchDog] Memory reclaimer evicting LRU model model="louise-chat-qwen3-8b" lastUsed=0001-01-01T00:00:00Z
09:06:49.827 INFO Backend process stopped id="louise-chat-qwen3-8b" address="127.0.0.1:42149" … exitCode="-1"
… the same at 09:06:50, 09:06:56, 09:07:01; six "evicting LRU model" lines
for the chat model, every one with lastUsed=0001-01-01T00:00:00Z
09:07:06.887 ERROR Stream ended with error error=rpc error: code = Unavailable desc = connection error: … connect: connection refused
09:07:06.887 INFO HTTP request method="POST" path="/v1/chat/completions" status=500
```

What `GET /system` listed as loaded meanwhile (`size_vram`):

| model | VRAM | state |
|---|---|---|
| vega-dar-legal-it | 13.6 GB | busy (an embedding stream) |
| vega-qwen3-embedding-4b | 9.9 GB | idle: loaded at 00:11, no request since |
| vega-bge-m3 | 1.1 GB | idle |
| vega-medcpt-query, vega-medcpt-article-ov, vega-granite | 1.1 GB together | idle |

The idle 4B embedder, last used at 00:11, was never chosen; the chat model,
loading for a request that was waiting, was chosen six times. Stopping the
idle 4B model by hand (`POST /backend/shutdown`) let the chat model load,
and the next request answered 200.

**Why, from the source** (`pkg/model/watchdog.go` at `v4.11.0`):
`evictLRUModel` sorts the candidates oldest-first by `lastUsed`
(`a.lastUsed.Compare(b.lastUsed)`). `lastUsed` is written only when a
request begins or ends (`beginRequestLocked`, `finishRequestLocked`,
`UpdateLastUsed`); nothing initialises it when a model is registered, so a
model whose load has started but whose first request has not yet reached
the backend has the zero time (`0001-01-01`) and sorts as the oldest model
on the card. The busy check (`busyTime`) does not cover it either: the
request that caused the load is not yet in flight at the backend. So under
memory pressure the reclaimer evicts exactly the model a request is
waiting for, every time, and the request can never succeed while the
pressure lasts.

**Suggested fix**, any of:
- set `lastUsed[address] = now` when the address is registered
  (`AddAddressModelMap`), so a model being loaded counts as just used;
- or skip, as busy, an address whose load has a request waiting on it;
- or, at least, never choose a model whose `lastUsed` is zero while another
  candidate has a real time.

**What covers it for us:** Louise unloads an embedder whose collection does
not embed everything (`worker._release_embedder`, 2026-10-14), so idle
encoders do not hold the card; the chat model can still lose this race
whenever the card is over the threshold for other reasons.

### B14. llama.cpp backend: `Embedding` ignores the token ids it is given

*Found 2026-10-18 from the source at `v4.11.0` (58830f7), after the AMD peer's
LocalAI (rocm-llama-cpp) refused 3 of the 512 calibration texts at a window
of 1,024 tokens (`tools/vega_cut_scan.py`).*

`/v1/embeddings` takes token ids as well as text. `core/backend/embeddings.go`
(`ModelEmbedding`, L131–L144) puts them in `PredictOptions.EmbeddingTokens`
and leaves `Embeddings`, the text, empty. The llama.cpp backend's `Embedding`
(`backend/cpp/llama-cpp/grpc-server.cpp` L2880 on) reads only the text
(`parse_options`, L371: `data["embeddings"] = predict->embeddings()`);
`EmbeddingTokens` appears nowhere in the file. So the empty text is
tokenized and refused: "Input content cannot be empty". llama.cpp itself
embeds token ids: the `tokenize_input_prompts` the backend calls takes an
array of numbers as the tokens themselves.

**Why it matters to us.** A space that cuts by tokens (09 §9.3, `ctx`;
char-cut-truncation.md) embeds an input's first `ctx` tokens — exactly what
its calibration reference embeds. With text only, VeGa's wrapper has to find
a text with exactly those tokens: a prefix of the input, or the pieces read
back (`Detokenize`). Some inputs have none — a word segmented otherwise once
cut, a cut inside a character spelled as byte tokens, a read-back that
tokenizes differently: 3 of 512 at 1,024 on llama.cpp's tokenizer (i = 339,
Greek decoded as Latin-1; 352, Danish; 372, Latin), all 622 with HF's.

**Fix: P2.** Embed the ids, with the special tokens a text gets, so that
embedding a text's tokens is embedding the text.

### B15. `v4.12.1`: backend downloads are staged at `//downloading`

*Seen on the node 2026-10-27, the first hour on v4.12.1.*

v4.12.1 stages every backend download in `DownloadStagingPath`
(`core/cli/run.go` L54, default `${basepath}/downloading`). In the
container `basepath` is `/`, and LocalAI runs as `apps`, which cannot
write there. So every backend install and auto-upgrade failed:
"failed to create staging directory: mkdir //downloading: permission
denied" (`pkg/oci/image.go` L340). The node's LocalAI keeps
`auto_upgrade_backends: true`, because the operator wants backend updates.

**Set on the node:** `LOCALAI_DOWNLOAD_STAGING_PATH=/backends/.downloading`
in the app's `additional_envs`. It sits on the backends' own filesystem,
so the final rename stays on one disk, and LocalAI does not list a
dot-named entry as a backend. **Caution:** `app.update` replaces the whole
`localai` values block it is given. Sending only `additional_envs`
silently reset `image_selector` to the CPU image for 38 s (12:42:42–43:20).
Nothing was written meanwhile: gutenberg deferred, because its answers did
not come from a validated run. Always send `image_selector: intel_image`
with the envs.

---


## Patches we carry

Our llama.cpp backend images build LocalAI's backend from the base commit in
[`deploy/localai/patches/series`](../../deploy/localai/patches/series) with
these patches applied in order (the RAGE image built LocalAI itself that way
until 2026-10-09). A patch leaves the series when upstream merges it, or
when a merged change makes it unnecessary. `tests/test_localai_patches.py`
holds the series, the files and this section to each other.

### P1. `GET /system` lists the options each loaded model was given

*Base: `v4.12.1` [`ec92740`](https://github.com/mudler/LocalAI/tree/ec9274027eadd552d56f2c6c1fe86cd9216a991a) since 2026-10-27; written on master
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

### P2. llama.cpp backend: `Embedding` embeds the token ids it is given

*Base: `v4.12.1`
[`ec92740`](https://github.com/mudler/LocalAI/tree/ec9274027eadd552d56f2c6c1fe86cd9216a991a) since 2026-10-27 (first `v4.11.0` = `58830f7`),
after P1 (they touch different files). File:
`0002-llama-cpp-embed-token-ids.patch`. 5 files, 99 lines added, none
removed.*

**Why.** B14: the backend drops `EmbeddingTokens`, so a client cannot
embed tokens, and cannot cut an input to exactly its first N tokens.

**What it changes** (`backend/cpp/llama-cpp/`):
- `grpc-server.cpp`, `Embedding`: when `EmbeddingTokens` is set, the prompt
  is those ids with the special tokens a text gets, as an array of numbers,
  which `tokenize_input_prompts` takes as the tokens themselves. A request
  with text only takes the same path as before.
- `embedding_tokens.h`: where the special tokens go. The ids are an input's
  tokens as `TokenizeString` returns them, without special tokens; a probe
  text ("a") is tokenized with and without them, and the ids take the place
  of its own tokens. That covers BOS/EOS, CLS/SEP, BOS only, EOS only and
  none, without naming any vocabulary type.
- `embedding_tokens_test.cpp`: those five cases, and the refusal when the
  probe's tokens are not found; `prepare.sh` copies both files, as it does
  the other helpers; `CMakeLists.txt` registers the test under
  `LLAMA_GRPC_BUILD_TESTS`. `backend/cpp/run-unit-tests.sh` finds it by its
  name.

**Run:** the unit test, `g++ -std=c++17 -Wall -Wextra -Werror` (gcc 14),
passes. **Measured** (2026-10-18, the dev PC): the CPU variant built by
`deploy/localai/llama-cpp/build.sh`'s recipe, bge-m3 (the node's GGUF,
sha256 `daec91ff…`), over gRPC, on the 512 calibration texts:
- a text's token ids give the text's vector bit for bit, from as many
  tokens: the probe, and 64 of 64 whole texts;
- at a window of 1,024: 189 of 189 over-long texts embedded from exactly
  1,024 tokens (1,022 + the 2 specials), 339, 352 and 372 among them;
- the same ids twice, the same bytes: 8 of 8.

**On the AMD peer's RX 9060 XT** (2026-10-18, its session): `build.sh hipblas`
with `AMDGPU_TARGETS=gfx1200` at 5193953 (series digest `724329e027f2`, as
the SYCL build; 3.0 GB), installed as `louise-rocm-llama-cpp`, vega-llama-cpp
rebuilt in front of it. `tools/vega_cut_scan` vega-bge-m3 at 1,024: 0 of 512
refused (3 before P2). `tools/vega_repro_check`, 16 texts x 2 fresh loads x
2 shuffled passes, bit-identical: vega-bge-m3 (`--window 1024`) 16/16,
its CPU twin 16/16, vega-qwen3-embedding-4b 16/16.

**LIVE on the node, 2026-10-18 21:19** (`deploy/vega/switch/rollout-p2.sh`,
the SYCL build of series digest `724329e027f2`, installed as
`louise-sycl-f16-llama-cpp`): the five SYCL vega- models 16/16 bit-identical
over 2 fresh loads x 2 shuffled passes at a window of 511 (bge-m3 at 1,024
too); the cut scan 0 of 512 refused at 1,024; the gate passed on every new
run — gutenberg worst 0.999999, normattiva 1.000000, pubmed document 0.999999
and query 1.000000, wikipedia 0.9997 (whole set 0.9990) — and nothing was
re-embedded. The first run stopped at step 7: a 512-token model sent a
2,465-token text whole; every model is now measured with a window.

**Rebased on `v4.12.1`** ([`ec92740`](https://github.com/mudler/LocalAI/tree/ec9274027eadd552d56f2c6c1fe86cd9216a991a),
2026-10-27, after the app moved to v4.12.1; operator: "a backend update is
advisable, just re-apply the known patches if still needed"). B14 is still
there: v4.12.1's `grpc-server.cpp` never reads `EmbeddingTokens`. P2's only
conflict was its `prepare.sh` hunk (upstream added `slot_cache.h` above it);
the code hunks applied unchanged. llama.cpp moves from `a868c3e` to
`c0fd786`, so each SYCL model on it is a new run: measured (invariant 12)
and gated, as on 2026-10-18. P1 still applies and stays in the series; it
patches LocalAI's server, not the backend image, and the node's server is
stock. louise-python is not rebuilt: v4.12.1's `backend.proto` only adds
(SlotCache, TTSLive, MotionStream, voice fields), its `common/` files are
byte-identical, and the VeGa wrapper forwards any method it does not
intercept as bytes.

**LIVE on the node, 2026-10-27 13:00**
(`deploy/vega/switch/rollout-p2-20261027.sh`, series digest
`e79a808b4808`, replacing `louise-sycl-f16-llama-cpp` in place, with the old
tree in `backends-bak-20261027`):
- bge-m3, granite, medcpt-query and qwen3-embedding-4b: each 16/16
  bit-identical over 2 fresh loads × 2 shuffled passes at a window of 511.
- bge-m3 also 16/16 at 1,024, and the cut scan refused 0 of 512 there.
- Gates passed:
  - gutenberg: 16/16 at 1.000000;
  - pubmed: document whole set worst 0.999998, query worst 1.000000;
  - wikipedia: whole set worst 0.9990.
- wikipedia-v8 was not gated, because its paused records build held the
  collection's lock.
- Nothing was re-embedded.

**Tokens against HF's** (bge-m3's `tokenizer.json`): 475 of 512 identical.
The other 37 are one thing: the text ends with a space, and HF's tokenizer
adds a trailing `▁` that llama.cpp, as SentencePiece, does not. The first
tokens agree, so no cut is affected; a whole text ending with a space is
one token apart, as it is today without the patch.

**How VeGa uses it.** At load the wrapper sends a probe's tokens as
`EmbeddingTokens` WITH NO TEXT and keeps the token path only if the vector
comes back bit-identical to the text's, from as many tokens
(`Gate._probe`). A stock backend refuses the empty text, so it can never
pass for one that embeds the ids; its inputs are cut as text, exactly or
refused, as before.

### P3. llama.cpp backend: the encoder of encoder-decoder models runs

*Base: `v4.12.1` (`ec92740`), after P1 and P2. File:
`0003-llama-cpp-encoder-decoder.patch`. It adds
`backend/cpp/llama-cpp/patches/0003-server-encoder-decoder.patch`, a
llama.cpp patch that the backend's `prepare.sh` applies to llama.cpp
`c0fd786` beside upstream's own two. 80 lines added to
`tools/server/server-context.cpp`.*

**Why.** B16. MADLAD-400-3B-MT is a T5 model. The library runs its encoder
(`llama_model_has_encoder`, `LLAMA_PROCESS_TYPE_ENCODE`), and
`tools/completion` calls it. llama-server never does, so a GGUF of it
answered `<2cs><2cs>…` (2026-09-18), and MADLAD went on PyTorch:
17–38 s cold and 7.2 GB (translator-choice.md).

**Upstream:**
- fairydreaming's `t5-server` branch (Dec 2024) was never merged; 0x4139's
  fork (Mar 2025) is the same 31 lines.
- #26565 (Aug 2026) closed stale with no reply.
- Both forks rewrite the prompt inside the prompt loop. Today's slot holds
  its task as `const`, and the prompt cache would reuse decoder positions
  that belong to another encoder input.

**What it changes:**
- **Which models:** an encoder-decoder target (architecture `t5` with a
  decoder), for generation tasks only. `llama_model_has_encoder()` alone is
  also true of T5ENCODER (encoder-only embedding models) and of EAGLE3 and
  DFLASH (speculative draft heads): none of P3's code runs for them, or for
  any other model (found 2026-10-27, before deploying: the first version
  keyed on `llama_model_has_encoder()` alone).
- `launch_slot_with_task`: for such a model, the prompt's tokens
  become the slot's encoder input, and the task's prompt becomes the decoder
  start token (BOS where none is named). `cache_prompt` is off for that
  task. A multimodal prompt is refused.
- `update_slots`, when the slot starts its prompt:
  - its sequence is cleared first (the encoder batch's positions are
    checked against what the memory holds);
  - the encoder reads the input in one ubatch on the slot's sequence id,
    which the decoder's cross-attention mask pairs with its own;
  - an input longer than `n_ubatch` is refused, rather than asserted on.
- `load_model`: one EOS token is encoded before `common_context_can_seq_rm`,
  whose probe decodes; a T5 decoder asserts "llama_encode must be called
  first" without it.
- More than one slot is warned about, because `llama_cross` holds one
  encoder output per context. Our models run one slot.

**Checked on CPU** (llama.cpp `c0fd786` + the patch, MADLAD bf16 GGUF
converted from our own weights by llama.cpp's converter, 2026-10-27):
- Tokens: all 3,078 query prompts tokenize identically to transformers'.
- Translations: 120 of 130 are identical to the torch backend's
  (`tools/measure/out/xlate_choice/q_madlad.jsonl`), once transformers'
  `clean_up_tokenization_spaces` (true in MADLAD's config) is applied. The
  other 10 diverge greedily at one token, 8 of them in zh/ja/ko.
- Speed: 2.26 s median per translation on 8 CPU threads.
**On the card**, 2026-10-27, measured with gutenberg embedding beside it.
Test backend `louise-sycl-f16-llama-cpp-p3test` (series `5c7ec99992cf`);
test model `madlad-p3-test-f16`, an f16 GGUF converted from our weights,
one slot. The live backend was not touched.
- **Format:** bf16 and f16 give the same translations; f16 is 2–3× faster
  warm.
- **Memory and load:** 6.29 GB on the card (torch: 6.71 GB). The first
  request, load included, takes 9.5–11 s (torch: 17–38 s).
- **The 490 queries (3,078 translations):** 2,815 identical to torch's
  after the cleanup (91.5 %). The differences by target: zh 48/488,
  ja 69/490, ko 69/490, fi 26/466, de 22/421, fr 21/412, en 8/311.
- **Speed:** median 3.03 s per translation (max 10.3 s), one language at a
  time. A question's 6–7 languages take ~18 s, against torch's 3.2 s for
  the batch.
- **Stage 0 (T10, llama-cpp-encoder-decoder.md §9):** 48 output tokens
  take ~170 ms each, whatever the input length (7 → 192 tokens: ×1.10). The
  cross-attention recompute does not dominate; a fixed cost per decode step
  does, and that is what decoding several translations together amortizes.
  Hence translator-choice.md "Waves".

**Only `/completion` with a raw prompt** is right for these models: a chat
template is text they were not trained on. The client sends one request
per segment (`<2xx> segment`), where today's backend takes the whole batch
as one JSON prompt (translator-choice.md, "Code changes").

### P4. llama.cpp backend: encoder-decoder slots are encoded in waves

*Base: `v4.12.1` (`ec92740`), after P1–P3. File:
`0004-llama-cpp-encoder-decoder-waves.patch`. It adds
`backend/cpp/llama-cpp/patches/0004-server-encoder-decoder-waves.patch`,
applied by the backend's `prepare.sh` after P3's. 173 lines added and 40
removed in `tools/server/server-context.cpp`, every one under P3's
`enc_dec` guard (architecture `t5` with a decoder, generation tasks).*

**Why.** P3 ran one slot: llama.cpp keeps ONE encoder output per context
(`llama_context::cross`, tagged per sequence), and every encode replaces
it whole, so a second slot's encode would overwrite the first's. One slot
is 3.03 s per translation on the card and ~18 s per question (its 6–7
languages), against torch's 3.2 s for the batch. Measured first (T10,
above): a decode step costs the same whatever the input length, so the
lever is decoding several translations in one batch — what torch does.
The per-sequence encoder memory (docs/notes/llama-cpp-encoder-decoder.md
§3, ~900 lines in the library) stays the upstream shape; this is the
server-only step (translator-choice.md "Waves"), built 2026-10-27.

**What it changes:**
- `enc_dec_waves()`, run by `pre_decode` before the batch is built: the
  slots in STARTED whose prompt the encoder has not read are gathered for
  a window (`LLAMA_ENC_DEC_WAVE_MS`, default 50 ms — a client's concurrent
  requests arrive within a few ms), or until every slot holds a request or
  the waiting tokens fill one ubatch. Then the longest prefix of them, in
  CONTENT order (their tokens; slot id as the tie-break), that fits one
  ubatch is encoded in ONE `llama_process(ENCODE)` call, each on its own
  sequence id (the library tags the rows per sequence and the cross mask
  pairs them). An input longer than `n_ubatch` is refused, not asserted
  on. While a wave decodes, new slots wait in STARTED.
- The batch follows the wave's content order (`slot_order`), so the KV
  cells a wave takes depend on what is translated, never on which slot
  took which request or on arrival order: the same group gives the same
  bytes.
- `release()` clears an encoder-decoder slot's prompt (its KV cells), so
  the next wave starts from the same empty pool.
- `n > 1` (`n_cmpl`) is refused: a child copies its parent's decoder state
  and never encodes.
- Other models: no code path of theirs changes.

**Checked on CPU** (llama.cpp `c0fd786` + P3 + P4, a random-weight T5 with
the TEST tokenizer, 7 slots, ubatch 1024, 2026-10-27): a lone request is
byte-identical to P3's; the same 7 prompts in 4 arrival orders give the
same 7 outputs (T3); a request arriving while a wave decodes is answered
after it; an input over `n_ubatch` and `n > 1` are refused with 400 and
the server goes on; 60 requests after a burst leave every slot empty.
**On the card:** the gates in tools/measure/t5waves.py (T4 against P3's
one-slot bytes, T3, in-wave vs alone, seconds per question), then the
retrieval comparison on the 451 + 39 queries (translator-choice.md).

**Measured on the card, 2026-10-28 (München, the node paused):** T4 PASSES —
379 of 379 one-at-a-time translations byte-identical to P3's, 0.43 s each;
a 7-slot wave answers a question in 1.2–1.6 s against torch's 3.2 s; T3
PASSES (626 of 626 the same twice: deterministic). **But a wave decodes
WRONG**: in-wave against alone 0 of 626 equal, in-wave against torch 0 of
626. Two identical prompts in one wave come back as two different wrong
texts (`' Le gardien de la lampe.'`, `' Le gardien allum .'` for `' Le
gardien du phare alluma la lampe.'`), the first two or three tokens right,
then divergence; which text a request gets follows its SLOT, not the
request. Ruled out, each with the same bytes: E1, the strict SYCL env
(`GGML_SYCL_ENABLE_FUSION/GRAPH/OPT=0`); **E2, the same backend, GGUF and
weights on the CPU (`gpu_layers: 0`): CORRECT** — so the server logic of
P4 is right and the fault is a SYCL kernel at two or more decoder tokens
(or two or more encoder sequences); E3, `GGML_SYCL_DYNAMIC_PRECISION=F32`
(every weight mul_mat an fp32 gemm): still wrong, so not the fp16-operand
gemm the F16 build uses at N ≥ 2. What differs between one and several
decoder tokens in `ggml-sycl` (read, not yet measured): the attention over
the F16 cache leaves the N = 1 kernels (`mul_mat_vec_p021`, `_nc`) for
oneMKL `gemm_batch` in half at tiny M and N (M = the key count, N = the
slots), `set_rows` writes several cells, the cross-attention's F32,
permuted K goes through the generic driver's 2-D copies with several
columns, and the position bias is a 3-D permute-and-cont. Nobody runs an
encoder-decoder with several slots on SYCL; upstream has no matching
report (searched 2026-10-28: ggml-org/llama.cpp #21893 reorder/OPT
corruption, #30892 fused XMX dequant-GEMM NaNs — quantized types only,
F16 weights never enter it — #19276 and #30624 flash-attention
corruption, which T5 cannot use; intel/compute-runtime #966, #972).
**The instrument:** `deploy/localai/patches/diag/0005-llama-cpp-sycl-cpu-ops-diag.patch`
(`GGML_SYCL_CPU_OPS=OP[:cond]...` makes the device decline an op class, so
the scheduler runs it on the CPU): one build, then one model load per op
class on the real failing probe; and R0 before it, `cache_type_k/v: f32`
in the YAML (no build), which takes the cache attention off the F16
kernels. **R0 (09:05): CORRECT with the f32 cache**, so the fault is in the
F16-cache path (the half `gemm_batch` of the attention, its conversions, or
the cache write), and the f32 cache is the workaround (+9 % time, 2× cache
memory). **NAMED (09:40, the diag build 03c771b88d03, München):** declining
the KQ product over the F16 K cache alone (`MUL_MAT:f16:nc:nN:p`) restores
the bytes; declining the KQV product alone does not; a K cache in f32 alone is
right, a V cache in f32 alone is not. So: `ggml_sycl_mul_mat_batched_sycl`
for the permuted F16 K view and the permuted queries at N ≥ 2 (the half
conversion of q, then oneMKL `gemm_batch` at M = the key count), on this
card. Upstream report drafted: upstream-bugs.md S1; the shape reproducer
`diag/test-backend-ops-kq-shapes.patch`. **Shipped with the f32 cache (both
K and V: the V-side call is the same library function at the mirror shape,
proven only at N = 2); the gates re-run on it. Until they pass the switch
does not happen;
nothing live has changed** (the torch translator serves; the test
backend and model are unloaded). In-wave vs alone (bar 90 %) is now a gate
in tools/measure/t5waves.py, not a report (fb014fe).

**The f32-cache gates, 2026-10-28 08:00–10:30 (München; from 09:51 the card
otherwise idle at the operator's order): PASS.** T3, the same group twice:
625 of 626 identical — the one row (WQ9339507 pl→en, "from his trip" against
"from a trip") came from the contended hour, when a question's group split
into waves of 2–5 slots where the paused run made waves of 6–7, and the batch
shape flips a last bit: a property of waves, recorded, not a defect. In-wave
vs alone 614 of 626 (PASS, bar 90 %; the 12 are one-word or punctuation
variants). Torch agreement: wave 555, alone 556 of 626. T4 as information:
372 of 379 identical to P3, 7 one-word variants, torch 342 against 343.
**Timing, node paused:** waves median **1.12 s per question**, p90 1.56 s,
against torch's 3.2 s (2.9× faster); one at a time 0.72 s per translation;
per token, one slot, paired on the same idle card: **29.2 ms on f32 against
16.3 ms on F16 — the f32 cache's own price, +79 % per token** (the generic
f32 path copies the K view per head; the F16 kernels come back when
upstream fixes the KQ product, upstream-bugs.md S1); 36.1 ms per step per
slot in waves. **Retrieval gate**, paired against torch MADLAD on the same
index (490 questions): descriptive n = 451 MRR 0.254 / R@10 0.446 / R@50
0.627 on both sides (+3/−1, p = 0.625); titles n = 39 identical. **Switched
2026-10-28:** this branch merged to main, the f8b4acdcdcb8 tar installed as
`vega-llama-cpp-t5`, `louise-translate-madlad` is the GGUF with the f32
cache, the torch model kept as `louise-translate-madlad-torch` for backout.
**Backed out the same morning (10:47), torch live again — a CLIENT defect,
not the backend's:** `app/translate.py shape()` told MADLAD-on-llama.cpp from
the torch backend by `/v1/tokenize("<2en>")` returning ONE token; LocalAI's
tokenize route does not parse special tokens and answers `[805, 38]` (two
pieces; the completion path does parse them, which is why every gate passed
with raw `<2xx> seg` prompts), so the probe said "batch" and cached it 600 s,
the warm sent a JSON prompt with no `max_tokens`, and the model generated to
its limit, 92 s, under the operator's paired test (A1, 60 questions through
/api/search beside gutenberg sharing). The gates had never exercised
`shape()`: t5waves.py speaks to the server directly. Fixed: the shape is told
by the VOCABULARY (`_t5_languages`' `/v1/detokenize` scan finds ≥ 16 `<2xx>`
codes; the scan is kept as the language list, so it costs nothing extra),
every batch-shape request carries a `max_tokens` bound (torch ignores it), and
the test fake answers the recorded two-piece tokenize. A1 is rerun on the
fixed client before the switch is tried again.
**Second attempt, 2026-10-28 11:19–11:42 (München, client 0ef4e4a): quality
equal, speed NOT better, backed out to torch at 11:42.** The same 60
questions through /api/search under gutenberg sharing: translation median
1.95 s against torch's 1.68 s (+16 %), p90 2.74 against 2.78, search total
10.81 against 10.65 s; 341/381 translations identical to torch's, 59/60 the
same top hit, 55/60 the same twelve; warm 7.8 s, shape t5, 498 languages, no
split waves. Through Louise torch's one batched call costs 1.7 s for six
languages, and the GGUF wave — 1.12 s on an idle card — lands at 1.95 s with
the f32 cache (+79 % per token) beside the embedder. The switch condition is a
gain; the F16 cache, back with P5, is the way to one (16.3 against 29.2 ms per
token). The GGUF YAML is kept on the node as
`models/louise-translate-madlad.yaml.gguf-20261028b`; results
`/root/madlad-gguf/a1b-{torch,gguf}.jsonl`.
**Root cause of the card fault found the same day: P5 below** — not the
card, not the runtime, not oneMKL: ggml-sycl's own src1 conversion on the
oneMKL path. The f32 cache stays until the P5 tar passes its gates.


### P5. llama.cpp backend (ggml-sycl): the batched F16 matmul converts src1 for the GEMM that runs

*Base: `v4.12.1` (`ec92740`), after P1–P4 (independent of them: a ggml-sycl
file). File: [`deploy/localai/patches/0005-llama-cpp-sycl-batched-mul-mat-src1.patch`](../../deploy/localai/patches/0005-llama-cpp-sycl-batched-mul-mat-src1.patch),
which adds `backend/cpp/llama-cpp/patches/0005-sycl-batched-mul-mat-src1-strides.patch`
(applies to plain `c0fd786`, checked with `patch --dry-run`). Written
2026-10-28 by code reading; München's log of upstream's suite matched the
prediction exactly; the card run of the fixed build is the gate.*

**What.** `ggml_sycl_mul_mat_batched_sycl` (F16 src0 × F32 src1: the KQ
product over an F16 K cache, the KQV product over an F16 V cache) converts
src1 to F16 before its GEMM. A build with oneDNN compiled in
(`GGML_SYCL_DNNL`, ours: the backend needs libdnnl.so.3) made a FLAT copy of
src1's whole extent — right for oneDNN's GEMM, which reads src1's own strides
— and then set `s11 = ne10, s12, s13` (the CONTIGUOUS strides) and
`is_src1_cont_2 = true` whichever GEMM ran. When oneMKL runs instead of
oneDNN — `GGML_SYCL_ENABLE_DNN=0`, which invariant 12 requires (oneDNN's
heuristic drifts), or a device without an optimized oneDNN GEMM — its
`gemm_batch` calls read the flat copy as contiguous: wrong data for every
non-contiguous src1. The fix decides the GEMM once, before the conversion:
flat copy for oneDNN (strides untouched), the gather `to_fp16_nc_sycl` with
the contiguous strides for oneMKL. Upstream: since bdca38376 (`sycl: Hotfix
for non dnnl codepath`, #14677, 2025-07-14) for a oneDNN build with the DNN
switch off; the `has_optimized_gemm` runtime gate (#28985, 2026-10-02) sends
devices without one down the same path with DNN=1. No upstream issue or PR
(searched 2026-10-28, c0fd786 and master e486a4b; none of the 20 ggml-sycl
commits since c0fd786 touch it).

**Why it explains everything measured** (localai-bugs.md P4 "Measured on the
card", upstream-bugs.md S1): the decoder's queries are PERMUTED (`[0,2,1,3]`)
— for ONE token the permuted layout is the contiguous one (right: N = 1 was
right, and takes `mul_mat_vec_p021` anyway), for two or more it is not
(wrong: every N ≥ 2); the V side's src1 is the softmax output, contiguous
(right: V f32 alone changed nothing); an F32 K cache takes the generic
path (right: K f32 alone fixed the text); the GPU runtime is not involved
(26.27 and 26.35 identical); upstream's suite fails EXACTLY its 32 strided
F16×F32 views with `ne13 > 1` — `bs2 = 1` takes `mul_mat_vec_nc`, not the
batched path — and its permuted cases with n = 1 pass (contiguous layout),
while the suite's permuted F16 cases with n ≥ 2 and `ne3 > 1` are "not
supported" by `supports_op`, which is why nobody saw it. The patch adds the
decoder KQ shape (16 heads, n = 2, 4, 7) to `test-backend-ops`.

**Gate (München, after the switch; the card).** (1) `test-backend-ops` on
the fixed build, SYCL F16, DNN=0: `-o MUL_MAT -p "type_a=f16,type_b=f32"`
→ 0 FAIL (32 before), `GGML_TEST_KQ_SHAPES=1` with the diag patch → 0 FAIL
(238 before); (2) the backend tar rebuilt with series P1–P4 + P5 (a new
engine); MADLAD with the F16 KV cache (the two `cache_type_*: f32` lines
dropped from the YAML): T4 bytes against P3, T3 in-wave == alone and the same
group twice, the retrieval gate paired to torch — tools/measure/t5waves.py;
(3) the per-token time back near 16.3 ms. The live shared engine
`louise-sycl-f16-llama-cpp` carries the same wrong path (no live model takes
it: embedders and chat run FA on, the rerankers have no cache); it is NOT
rebuilt for this alone — every space on it would be re-gated — the next
rebuild carries P5 (approved-encoders.md "Known limits").


### P6 (staged). llama.cpp backend: the encoder output is kept per sequence

*Base: `v4.12.1` (`ec92740`), after P1–P4. File:
`deploy/localai/patches/staged/0006-llama-cpp-encoder-decoder-memory.patch`
(staged: built and tested on the CPU 2026-10-28, NOT in `series` until its
card gates pass — staged/README.md). It adds
`backend/cpp/llama-cpp/patches/0005-encoder-decoder-memory.patch`, applied
by the backend's `prepare.sh` after P4's: 323 lines added and 23 removed
across `src/` (new `llama-memory-enc-dec.h/.cpp`, 10 files), `tests/` and
`tools/server/server-context.cpp`.*

**Why.** P4 encodes the slots about to start together because the library
keeps ONE encoder output per context (`llama_context::cross`), and must
therefore hold a new request until the running wave has decoded. Stage 1
of docs/notes/llama-cpp-encoder-decoder.md gives T5 its own memory: the
decoder's `llama_kv_cache` plus a host-side store of encoder rows, each
owned by the sequences it was encoded for. An encode replaces the rows of
the batch's sequences and keeps every other sequence's; a decoder ubatch
uploads the rows its sequences own (by contiguous runs: a lone sequence is
one run, the same bytes as before) and masks each token to its own
sequence's rows; a sequence with no rows is refused (a new internal status
`LLAMA_MEMORY_STATUS_FAILED_INPUT`, `-1` from `llama_decode`). A
whole-sequence removal, `clear` and `seq_keep` release the rows, `seq_cp`
shares them, the sequence state carries them as a suffix (magic `T5XE`,
version 1; `PARTIAL_ONLY` writes none). For `n_seq_max > 1` the context
forces `kv_unified`, so the self-attention mask and the KV views agree.
No public API changes; only `LLM_ARCH_T5` constructs the memory;
`llama_cross` and the graph parameter stay, filled as before, for the
carried patch (deletions wait for the upstream PR).

**Deviations from the note's §3**, each for a reason found in the code:
- `init_update` is delegated to the decoder cache (its update context is
  only used by the shift graphs, which never build the T5 decoder);
- the graph reserve sizes the cross tensors to `n_seq_max × min(n_ubatch,
  n_ctx_train)` rows (the note said `n_seq_max × n_ubatch`): a T5 input
  longer than its window is refused by the server, and the scheduler
  reallocates if a live graph is ever larger;
- the cross inputs decide by `arch == LLM_ARCH_T5 && mctx`, not by a
  dynamic cast; the encoder graph has no memory context and never reaches
  them.

**Server (on P4):** a wave is encoded while earlier ones decode (the
`enc_live` gate is gone; the 1 ms rest only when nothing decodes); the
prompt start skips its whole `seq_rm` for a slot the wave just encoded
(it would release the rows just stored); the load-time probe encode and
`common_context_can_seq_rm` (which decodes after a clear) are skipped for
these models, speculative decoding stays unsupported; the forced unified
cache is mirrored in `params_base.kv_unified`. `n > 1` stays refused.

**Checked on the CPU** (random-weight T5, `tests/test-llama-archs -a
'^t5$'`; `tests/test-enc-dec-store`; the tiny-model server checks of P4):
a sequence alone gives the bytes of a one-sequence context; two sequences
decoding in one batch are each within nmse 1e-4 of their solo runs, NOT
bitwise (as §6 T2 predicted: the batched kernels differ); a whole removal,
a clear and a truncated state restore leave `-1` with no abort and the
other sequence untouched; a partial removal, `seq_cp` and a restored
state keep the output; the store's replace/compact/share/keep semantics
and its bound (`n_seq_max × n_ubatch`) hold; a lone server request is
byte-identical to P3's, the same 7 prompts in 3 orders give the same
bytes, 60 requests after a burst leave every slot empty, and a request
arriving while a wave decodes is answered in 0.06 s (P4: after the wave).

**Card gates before it moves into the series** (staged/README.md): the
f32-cache MADLAD backend rebuilt with it AND P6 — T3, in-wave vs alone (bar
90 %), a lone request against P4's bytes, seconds per question — then
the retrieval comparison. Stage 2 (the device store) is P6 below.

### P7 (staged). llama.cpp backend: the encoder outputs live on the device

*Base: `v4.12.1` (`ec92740`), after P1–P5. File:
`deploy/localai/patches/staged/0007-llama-cpp-encoder-decoder-store.patch`
(staged with P5: built and tested on the CPU 2026-10-28, NOT in `series`
until the card gates pass — staged/README.md). It adds
`backend/cpp/llama-cpp/patches/0006-encoder-decoder-store.patch`, applied
by the backend's `prepare.sh` after P5's.*

**Why.** Stage 2 of docs/notes/llama-cpp-encoder-decoder.md, the shape
upstream asked for (a persistent buffer, no host copies: PR #11213
r1969892524, #22728). P5 kept the encoder rows on the host and uploaded a
ubatch's rows at every decode step; here the store is one F32 tensor in
its own backend buffer, `(n_seq_max + 1)` REGIONS of `n_ubatch` rows
(MADLAD at `-np 7 -ub 1024`: 8 × 1024 × 1024 floats, 32 MiB on the card).
The encoder graph writes its output rows into the lowest free region with
`ggml_set_rows` (its context, `init_encode`, assigns the region; a new
default-nullptr virtual on `llama_memory_i`), and `llama_context::encode`
COMMITS after the compute succeeded — the batch's sequences leave the rows
they owned and own the new ones — so a failed encode keeps the old output
(the §8 question, settled as write-beside plus a post-compute commit). A
sequence owns rows in one region at a time, so a free region always
exists. The decoder gathers the rows its ubatch's sequences own with
`ggml_get_rows` on an I32 index input; the mask is unchanged. The
synchronize and memcpy of the encoder output and the per-step upload are
gone for T5. Sequence operations move no data: they are owner bookkeeping
on the rows. State writes carry the rows through the io's tensor path
(suffix version 2: the owned rows of each used region at their in-region
index), so `llama_memory_get_buffers` sees the store's buffer and a
single-sequence restore lands in a free region. Each state write of the
whole context and each single-sequence restore is region-shaped; a
file from a store of another shape (`n_embd`, region size) is refused.

**Checked on the CPU**: the same tests as P5 — `tests/test-llama-archs -a
'^t5$'` (a sequence alone bitwise equal to a one-sequence context; two
sequences in one batch within nmse 1e-4 of their solo runs; whole removal,
clear and a truncated restore leave `-1`; partial removal, `seq_cp` and a
restored state keep the output), `tests/test-enc-dec-store` rewritten for
the device-backed store (regions chosen lowest-free and released by the
commit, shared rows, `seq_cp`/`seq_keep`, the `n_seq_max + 1` bound, state
whole and per sequence, truncated and foreign files refused), and the P4
server checks (a lone request byte-identical to P3, the same 7 prompts in 3
orders, bursts, a late request answered in 0.06 s).

**Card gates** with P5 (one rebuild): plus `test-backend-ops -o
SET_ROWS,GET_ROWS` on the node for F32, which the device store relies on.

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
  `vram_budget` (`runtime_settings_registry.go` L222–L246);
- a RANDOM seed at every load when `parameters.seed` is unset or -1
  (`core/backend/options.go`, `getSeed`). RAGE's smoke test saw each
  reload become another run: "Seed 551695963 → 1045691491".

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
