# LocalAI: every action item for the LocalAI team

*One place for everything this project owes or asks `mudler/LocalAI`: the
defects found (B1–B14), the patches we carry (P1–P2), and the requests (R1–R5).
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
| B8 | defect | `POST /models/reload` never forgets a model whose YAML was removed: it lists and serves it until LocalAI restarts | RAGE's `DELETE` answers 502 and says so | not filed; seen on a real LocalAI, 2026-10-03 |
| B9 | defect | `/v1/embeddings`: a vector holding a NaN fails the whole request with 500 ("json: unsupported value: NaN"), losing every other vector and the reason | Louise retries it as the null vector llama-server sends (`embeddings._numeric_failure`) | not filed; seen on the node, 2026-10-04 (15c) |
| B11 | defect | memory reclaimer: a model being loaded has a zero `lastUsed` and is evicted first, as the "least recently used" — under memory pressure the model a request is waiting for is evicted every time, the request fails with 500 | availability: every Ask answer failed with 500 for minutes (2026-10-14) | not filed; seen on the node with v4.11.0, log and source in §B11 |
| B12 | defect | `intel-transformers` (v4.11.0's, as the gallery installed it 2026-10-05): optimum-intel 1.15.0 imports `openvino.runtime`, which the openvino 2026.4.1 beside it no longer has, so EVERY `OVModel*` type fails to load (`ModuleNotFoundError`, checked on the node 2026-10-17). `intel-rerankers` has optimum-intel 2.2.0 and is not affected | none since louise-python (docs/notes/backend-consolidation.md, U4) | not filed |
| B13 | defect | transformers backend, `Embedding`: prints every input in full to stderr ("Calculated embeddings for: …"), a whole document per line in LocalAI's log | — (U3) | not filed |
| B14 | defect | llama.cpp backend, `Embedding`: ignores `EmbeddingTokens`, which `/v1/embeddings` fills when given token ids; it embeds the empty text beside them and refuses it ("Input content cannot be empty"), so no client can embed tokens | **blocking** for a space that cuts by tokens: without token ids an input cannot always be cut to exactly its first N tokens (3 of 512 at 1,024 on the AMD peer); P2 | not filed; found 2026-10-18, source in §B14 |
| **P1** | **patch we carry** | `GET /system` lists the options each loaded model was given, with their hash | none since VeGa: the wrapper receives `LoadModel` itself (vega.md §1). It was blocking for RAGE; kept for upstream | not filed; [`deploy/localai/patches/0001-system-list-load-options.patch`](../../deploy/localai/patches/0001-system-list-load-options.patch) |
| **P2** | **patch we carry** | llama.cpp backend: `Embedding` embeds `EmbeddingTokens`, with the special tokens a text gets (B14) | **blocking** for the token cut: VeGa's wrapper sends a cut as its first tokens to a backend that embeds them (`Gate._probe`) | not filed; [`deploy/localai/patches/0002-llama-cpp-embed-token-ids.patch`](../../deploy/localai/patches/0002-llama-cpp-embed-token-ids.patch) |
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

---


## Patches we carry

Our llama.cpp backend images build LocalAI's backend from the base commit in
[`deploy/localai/patches/series`](../../deploy/localai/patches/series) with
these patches applied in order (the RAGE image built LocalAI itself that way
until 2026-10-09). A patch leaves the series when upstream merges it, or
when a merged change makes it unnecessary. `tests/test_localai_patches.py`
holds the series, the files and this section to each other.

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

### P2. llama.cpp backend: `Embedding` embeds the token ids it is given

*Base: `v4.11.0` = master
[`58830f7`](https://github.com/mudler/LocalAI/tree/58830f7ac508845a6f4efa32cfca06af422d4d82),
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
