# One app for every model — survey and proposal

*Operator, 2026-09-19: "each plugin uses one to three models, on
llama.cpp, Ollama, PyTorch. I want one container (a TrueNAS app) to
add/load/unload/remove models, run them sequentially or concurrently,
on CPU or GPU as chosen at load."* **Proposal, not built.** It extends
[`embedder-pool-spec.md`](embedder-pool-spec.md), which chose llama-swap
when every encoder was a llama.cpp build, and it is the server
[`gpu-time-slots.md`](gpu-time-slots.md) was waiting for.

*Revised the same day. The first version ruled out every all-in-one
design because `space_id` hashes the serving image's digest (02 §2.4).
The operator: the specification is in progress and has one user, so a
rule that gets in the way is amended, not obeyed. §5 records the
amendment, now made.*

## 1. What runs today

One TrueNAS app per model, each on its own port, started by hand:

| runtime | models | device |
|---|---|---|
| llama.cpp, Intel SYCL build b10630 | Qwen3-Embedding-4B, bge-m3, MedCPT article + query, bge-reranker-v2-m3 | Arc |
| our FastAPI + PyTorch XPU (`deploy/dnabert2*`, `deploy/madlad-translate`) | DNABERT-2, MADLAD-400-3B | Arc or CPU (`DEVICE=`) |
| our FastAPI + OpenVINO (`deploy/medcpt-rerank`) | MedCPT cross-encoder | Arc |
| TEI 1.8 CPU | bge-m3, MedCPT (the older endpoints) | CPU |
| Ollama | qwen3:8b (chat) | Arc |

Measured 2026-09-18 by the GPU panel: eight servers resident on the Arc
at once, 25.7 of 34.2 GB. The line "the Arc fits one model server at a
time" in `gpu-time-slots.md` and `step6-preflight.md` predates that.

## 2. What decides it

- **The operator's ask.** One app. Add, load, unload and remove models.
  The device chosen at load. Models concurrent where they fit.
- **The card.** An Arc Pro B70 on `xe`, with no CUDA. What already works on it cost six
  probes to find (DEPLOYMENT.md, "Running PyTorch on the Arc"):
  - the compute runtime from the Launchpad PPA that llama.cpp's image uses;
    Intel's own channel installs cleanly and sees zero devices;
  - llama.cpp's SYCL build;
  - torch's XPU wheels;
  - OpenVINO.

  Any runtime that is not one of these starts that search again.
- **Three models no generic backend serves as they are:**
  - DNABERT-2: remote code and its own pooling;
  - MADLAD: a seq2seq translator;
  - the MedCPT cross-encoder: its pairs need segment ids, which llama.cpp
    and OVMS both zero, `reranker-scope.md` §12.

  Our three FastAPI servers exist because of this.

## 3. What exists

| candidate | verdict |
|---|---|
| **llama-swap** | **fits.** One Go binary that starts, health-checks, proxies and stops any server by a `cmd`. It has `ttl`, `groups` (`swap`, `exclusive`, `persistent`), a `matrix` of allowed combinations, a startup preload, `/api/models/load`, `/api/models/unload/<id>`, `/running`, a `/ui`, and it reloads its config. It serves no model itself, so the runtimes proven on the B70 run unchanged under it |
| LocalAI | **the closest off the shelf.** A gallery, add from Hugging Face in its UI, load and unload endpoints, `pinned`, LRU eviction, `concurrency_groups`, idle and busy watchdogs. Against it on this node: every runtime is its own build. Whether its Intel images carry a compute runtime new enough for the B70 is unverified, and its docs do not say which Python backends have Intel variants. DNABERT-2, MADLAD and MedCPT would each be a custom gRPC backend. **The fallback** if §4's proof fails, or if adding from a gallery matters more than keeping the proven runtimes |
| Xinference | choosing the device at launch (`n_gpu=None` = CPU) is exactly the ask. But it is NVIDIA-centred with no documented XPU support, and it has open bugs where `--device cpu` still ends up on the GPU |
| GPUStack | a cluster manager, centred on vLLM and SGLang. Intel Arc support not evident |
| OpenVINO Model Server | runtime add and remove through `config.json`, `target_device` per model, embeddings and rerank on the Arc. OpenVINO models only, so it is one runtime, not the manager |
| Ollama | GGUF only, and itself a swapper |
| Triton | CUDA-centred; PyTorch on XPU is not a supported backend |

## 4. Proposal: one image, llama-swap supervising processes

One TrueNAS app, `model-pool`, one image, one port, `/dev/dri`, and a
models directory. The image holds:
- the B70 compute runtime (the verified recipe);
- llama.cpp's SYCL build, copied from the image we pin today, with its
  oneAPI libraries under its own prefix;
- one Python venv with torch XPU, transformers and OpenVINO, and our
  three servers;
- llama-swap.

Each model runs as a child process. No docker socket.

```yaml
models:
  "bge-m3@xpu":
    cmd: /opt/llama/llama-server -m /models/bge-m3-FP16.gguf --embedding --pooling cls -ngl 99 --port ${PORT}
    env: ["LD_LIBRARY_PATH=/opt/llama/lib"]
  "bge-m3@cpu":
    cmd: /opt/llama/llama-server -m /models/bge-m3-FP16.gguf --embedding --pooling cls -ngl 0 --port ${PORT}
  "dnabert2@xpu":
    cmd: /opt/py/bin/python -m servers.dnabert2 --device xpu --port ${PORT}
  "dnabert2@cpu":
    cmd: /opt/py/bin/python -m servers.dnabert2 --device cpu --port ${PORT}
groups:
  bge-m3:   {swap: true, members: ["bge-m3@xpu", "bge-m3@cpu"]}
  dnabert2: {swap: true, members: ["dnabert2@xpu", "dnabert2@cpu"]}
```

| the ask | what does it |
|---|---|
| add / remove | an entry in the config file, reloaded live; a form on the System page later |
| load | `POST /api/models/load` (exact form to verify), the first request, or the startup preload |
| unload | `POST /api/models/unload/<id>` |
| list | `GET /running`, `/v1/models`, `/ui` |
| CPU or GPU | load `<model>@cpu` or `<model>@xpu`. A per-model group means loading one variant unloads the other |
| concurrent or sequential | `groups` or `matrix` (§6) |

- **Ollama and TEI go.** qwen3:8b is a GGUF, and the same llama-server
  serves it. The CPU encoders become `@cpu` variants.
- **Louise's client does not change.** `/upstream/<id>/…` proxies any
  path and loads the model on first use. A collection's `[encoder]` URL
  becomes `<pool>/upstream/bge-m3@xpu`, one line, reversible.
- **Which models are preloaded is desired state** (the file). Load and
  unload at runtime are overrides that a restart resets (invariant 10).

## 5. The amendment

**Settled 2026-09-19** (specs on `main`, `0d85f7f`, operator's choice).
The first draft of this section was wrong on one point: rebuilding the
image never re-keyed an existing collection, whose Codex is fixed. It
would only have forced every endpoint through the gate again. What the
specs now say:

- **The engine.** `space.encoder.engine` = `{name, build, variant,
  image, args, env, device}`: the tested run, frozen inside
  `collection_id` so anyone can reproduce the reference (09 §9.3). For
  this pool:
  - `image` is the pool image;
  - `args` is the model entry's `cmd`, with `${WEIGHTS}` and `${PORT}`
    left as placeholders;
  - `device` is the Arc or the CPU.
- **The space.** `space_id` hashes the weights and the settings, never
  the engine. A newer engine or pool build joins by passing the gate
  (03 §3.3).
- **The fingerprint** — superseded on 2026-09-20 (dd9bef4, §8 below).
  A run is validated once. The pool states the run on every answer, and
  a vector is written only under a validated run. The fingerprint is
  kept as evidence in the statement and is no longer taken after every
  batch; that would have cost 12.5–17 s per commit on gutenberg.

Code to follow on `louise`:
- `app/hold/codex.py`: Codex version 3 and the new `space_id`;
- the version bumps in the bundle header, attestation, digest and
  calibration objects;
- the fingerprint in place of the backend gate.

## 6. Sequential or concurrent

**Concurrent by default. Sequential only where memory forces it.**
- **Co-residence costs only memory.** The node already runs eight
  servers at once with 8.5 GB spare, and the card is time-shared
  whatever we do. `embedder-pool-spec.md` §3 already says that idle is
  not a reason to unload.
- **Overcommit does not fail on `xe`.** It moves weights to host RAM
  silently: MedCPT-Article, 2026-09-18, which cost nothing measurable for
  a 110M model. **For Qwen3-4B or MADLAD it is unmeasured.** Measure it
  before any policy relies on overcommit.
- **Sequential is the fallback** for heavy creation encoders that do not
  fit together: `gpu-time-slots.md`'s hourly slot, as a `swap: true`
  group, rotated by Louise at a batch boundary.
- **Admission belongs to Louise.** llama-swap does not know memory.
  Whether a load fits is decided by Louise's orchestrator from
  `app/gpu_probe.py`'s figures (`embedder-pool-spec.md` §3).
- **CPU variants need a memory bound.** The OOM killer took the CPU TEI
  encoder four times on 2026-09-15.

## 7. First proof

The pool image with bge-m3 (llama.cpp) and DNABERT-2 (torch), each in
`@xpu` and `@cpu`. Done when:
- both runtimes see the B70 **from the same image**. llama.cpp's oneAPI
  libraries and torch's bundled SYCL runtime must not clash, and this is
  the risk this proof exists to retire;
- load, unload and the device swap are timed;
- the vectors match today's endpoints within the calibration tolerance;
- wikipedia's calibration gate passes through `/upstream/…`.

## 8. The pool states the run on every answer (2026-09-20)

Specs dd9bef4 (03 §3.3, 09 §9.12) make the pool the node's trusted
processing platform.
- For each engine it launches, it computes the run identity from what
  actually runs: the image's digest resolved at launch (never a tag), the
  weights file's digest (hashed once at load, cached by path, size and
  mtime), the exact argv and env, the device, and the driver and runtime
  versions.
- It adds `Louise-Run: <run_id>` to every response, and describes a run
  at `GET /louise/runs/<run_id>`.

llama-swap does neither, as far as known (to verify). So the pool's
front is ours: a thin proxy that launches through llama-swap or replaces
it. That is the first piece to prove along with §7's bge-m3 and
DNABERT-2.
