# One app for every model — survey and proposal

*Operator, 2026-09-19: "each plugin uses one to three models, on
llama.cpp, Ollama, PyTorch. I want one container (a TrueNAS app) to
add/load/unload/remove models, run them sequentially or concurrently,
on CPU or GPU as chosen at load."* **Proposal, not built.** It extends
[`embedder-pool-spec.md`](embedder-pool-spec.md), which chose llama-swap
when every encoder was a llama.cpp build, and it is the server
[`gpu-time-slots.md`](gpu-time-slots.md) was waiting for.

## 1. What runs today

One TrueNAS app per model, each on its own port, started by hand:

| runtime | models | device |
|---|---|---|
| llama.cpp, Intel SYCL build b10630, pinned by digest | Qwen3-Embedding-4B, bge-m3, MedCPT article + query, bge-reranker-v2-m3 | Arc |
| our FastAPI + PyTorch XPU (`deploy/dnabert2*`, `deploy/madlad-translate`) | DNABERT-2, MADLAD-400-3B | Arc or CPU (`DEVICE=`) |
| our FastAPI + OpenVINO (`deploy/medcpt-rerank`) | MedCPT cross-encoder | Arc |
| TEI 1.8 CPU | bge-m3, MedCPT (the older endpoints) | CPU |
| Ollama | qwen3:8b (chat) | Arc |

Measured 2026-09-18 by the GPU panel: eight servers resident on the Arc
at once, 25.7 of 34.2 GB. The line "the Arc fits one model server at a
time" in `gpu-time-slots.md` and `step6-preflight.md` predates that.

## 2. Three constraints that decide the choice

1. **The serving image is part of the space.** `space_id` hashes
   `encoder.image` — "the digest of the image that serves them" (02 §2.4,
   09 §9.3). A manager that serves models from its own image becomes
   that image: every rebuild of it (a new runtime, a manager upgrade)
   re-keys every space it serves, and invariant 1 refuses every such
   collection at start. And a manager that brings its own llama.cpp
   build is not b10630 SYCL, so every space would need re-embedding or a
   new calibration. **The manager must launch each runtime from its own
   digest-pinned image, unchanged.** This rules out every
   all-runtimes-in-one-image product.
2. **Weights arrive through Louise.** An encoder is content-addressed and
   travels between peers like any artefact (02 §2.4, 04 §4.3), and no
   bytes move outside the transfer manager's budget (invariant 5). A
   manager that pulls from Hugging Face or its own gallery is a bypass.
   "Add a model" means: refer to weights Louise has already fetched and
   checked against their digest.
3. **Intel only.** An Arc Pro B70 (`xe`, SYCL / Level Zero / OpenVINO)
   and no CUDA. Products built around CUDA work on this node only on
   the CPU.

## 3. What exists

| candidate | runtimes | verdict |
|---|---|---|
| **llama-swap** | any server started by a `cmd`, `docker run` included | **fits**: it launches and stops processes and does not serve models, so each runtime keeps its own image (§2.1). Already chosen in `embedder-pool-spec.md` |
| LocalAI | its own builds of llama.cpp, transformers, rerankers…, with SYCL variants | fails §2.1 (its own builds and backend images) and §2.2 (a gallery that downloads). DNABERT-2, MADLAD and the MedCPT pair tokenizer would be custom backends anyway. It has the best load/unload semantics: `pinned`, LRU, `concurrency_groups` |
| Xinference | transformers, llama.cpp, vLLM | `n_gpu=None` = CPU at launch is exactly the ask, but it is NVIDIA-centred with no documented XPU support, and it has open bugs where `--device cpu` still ends up on the GPU. Also fails §2.1 |
| GPUStack | llama-box, vLLM, vox-box | a cluster manager; Intel Arc support not evident. Overkill for one node |
| OpenVINO Model Server | OpenVINO only | runtime add/remove via `config.json`, `target_device` per model, embeddings and rerank on the Arc. But no GGUF and no arbitrary PyTorch, and its rerank zeroes `token_type_ids`, which breaks MedCPT (`reranker-scope.md` §12). Usable as **one runtime under** the manager, not as the manager |
| Ollama | GGUF only | is itself a model swapper; no PyTorch |
| Triton | PyTorch, ONNX, OpenVINO, Python | CUDA-centred and heavy; PyTorch on XPU is not a supported backend |

## 4. Proposal: llama-swap as the orchestrator of sibling containers

One TrueNAS app, `model-pool`: the llama-swap binary (pinned by version
and checksum) and the docker CLI, the docker socket, one config file, one
port. llama-swap serves no model itself. Each entry starts the runtime
that serves the model today, from the same image digest:

```yaml
models:
  "bge-m3@xpu":
    cmd: >
      docker run --rm --name mp-bge-m3-xpu --network model-pool
      --device /dev/dri -v <models>:/models:ro
      ghcr.io/ggml-org/llama.cpp@sha256:<the digest pinned today>
      -m /models/bge-m3-FP16.gguf --embedding --pooling cls -ngl 99 --port 8080
    cmdStop: docker stop mp-bge-m3-xpu
    proxy: http://mp-bge-m3-xpu:8080
    ttl: 0
  "bge-m3@cpu":                    # same image, same weights: -ngl 0, no /dev/dri, --memory cap
  "dnabert2@xpu":                  # dnabert2-embed-xpu@sha256:…, DEVICE=xpu
groups:
  bge-m3: {swap: true, members: ["bge-m3@xpu", "bge-m3@cpu"]}   # never both at once
```

| the ask | what does it |
|---|---|
| add / remove | an entry in the config file; `--watch-config` reloads it |
| load | `POST /api/models/load` (exact form to verify), or the first request, or `hooks.on_startup.preload` |
| unload | `POST /api/models/unload/<id>` |
| list | `GET /running`, `/v1/models`, the `/ui` |
| CPU or GPU at load | load `<model>@cpu` or `<model>@xpu` |
| concurrent or sequential | `groups` (`swap`, `exclusive`, `persistent`) or the `matrix` of allowed combinations |

**The device is a variant, not a load parameter**, on purpose: the
backend gate's probe baseline is device-specific (the DNABERT-2 XPU app's
notes), so each (model, device) pair is its own endpoint identity anyway.
Both variants use the same image and weights digests, so `space_id` is
unchanged, and so is the cosine agreement across devices that
calibration already accepts.

**No change to Louise's client.** `/upstream/<id>/…` proxies any path to
that model and loads it on first use, so a collection's `[encoder]` URL
becomes `<pool>/upstream/bge-m3@xpu` and the rest of the request is
byte-identical. A migration is one line per collection file, reversible,
and checked by the backend gate as usual.

**Which models are preloaded is desired state** (the config file).
Loading and unloading at runtime are overrides that a restart resets
(invariant 10).

**Ollama:** its qwen3:8b is a GGUF blob. Serve it with the same
llama-server build under the pool rather than nest a second swapper.

## 5. Sequential or concurrent

**Concurrent by default. Sequential only where memory forces it.**

- The node already runs eight servers concurrently and has 8.5 GB
  spare. The card is time-shared whatever we do, so co-residence costs
  only memory. `embedder-pool-spec.md` §3 already says that idle is not
  a reason to unload.
- On `xe`, overcommit does not fail: it moves weights to host RAM
  silently (MedCPT-Article, 2026-09-18). For a 110M model that cost
  nothing measurable; **for Qwen3-4B or MADLAD it is unmeasured**.
  Measure it before any policy relies on overcommit.
- Sequential is the fallback for heavy creation encoders that do not fit
  together: the hourly slot of `gpu-time-slots.md`, as a `swap: true`
  group, rotated by Louise at a batch boundary.
- llama-swap does not know memory. Admission ("does this load fit?")
  belongs to Louise's orchestrator and reads `app/gpu_probe.py`, as
  `embedder-pool-spec.md` §3 already puts it.
- CPU variants need a `--memory` cap. The OOM killer took the CPU TEI
  encoder four times on 2026-09-15.

## 6. Costs and open points

- **The docker socket gives root on the host.** The pool parses no
  untrusted data, and its commands come from a file, never from a
  request. Keep its port on an internal network reachable only by the
  web, the worker and the operator. **This is the operator's decision.**
- Containers started through the socket are not TrueNAS apps: the Apps
  UI does not show them, and they outlive a crashed pool. Each `cmd`
  should `docker rm -f` its name first. Verify how llama-swap behaves
  after a restart.
- The custom servers already expose `/health` and `/v1/embeddings`,
  which is all llama-swap's readiness check needs.

## 7. First proof (before any collection moves)

The pool with two entries:
- `bge-m3@xpu`, the llama.cpp digest that serves :30447;
- `dnabert2@xpu` / `@cpu`, PyTorch.

Done when:
- load, unload and the device swap are timed;
- wikipedia's calibration gate passes through `/upstream/…`;
- a single command shows each collection's `space_id` unchanged.
