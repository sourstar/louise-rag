# RAGE — the RAG Engine: one container for every model

*Named by the operator, 2026-09-20. This note called it "the model pool"
until then.*

*Operator, 2026-09-19: "each plugin uses one to three models, on
llama.cpp, Ollama, PyTorch. I want one container (a TrueNAS app) to
add/load/unload/remove models, run them sequentially or concurrently,
on CPU or GPU as chosen at load."* **Design accepted, not built.** It
extends [`embedder-pool-spec.md`](embedder-pool-spec.md), which chose
llama-swap when every encoder was a llama.cpp build, and it is the
server [`gpu-time-slots.md`](gpu-time-slots.md) was waiting for.

*Revised the same day. The first version ruled out every all-in-one
design because `space_id` hashes the serving image's digest (02 §2.4).
The operator: the specification is in progress and has one user, so a
rule that gets in the way is amended, not obeyed. §13 records the
amendments, now made.*

*Rewritten 2026-09-19 after review. [`rage-review.md`](rage-review.md)
found four blocking gaps and seven open decisions in the first
proposal; the operator accepted every recommendation, and this note now
states the design as accepted. The reasons, the alternatives and what
was checked against llama-swap's documentation are in the review, not
repeated here. Where the two differ, this note wins.*

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
- **The specs.** RAGE is the node's trusted processing platform (03
  §3.3): it launches every engine, states the run with every answer, and
  a vector is written only from an answer naming a validated run. Its
  image is the engine a Codex created here freezes, so it must be
  obtainable by a stranger (02 §2.4).

## 3. What exists

| candidate | verdict |
|---|---|
| **llama-swap** | **fits, as a process runner.** One Go binary that starts, health-checks, proxies and stops any server by a `cmd`, with a `/ui` and a watched config. Verified 2026-09-19: it has `/upstream/<id>`, `/running`, unload endpoints and a startup preload, and NO load endpoint; a model loads on its first request. Its groups evict by default (`exclusive: true`). It knows nothing of memory, states no run and exposes no argv. So it runs the processes and holds no policy (§4) |
| LocalAI | **the closest off the shelf.** A gallery, add from Hugging Face in its UI, load and unload endpoints, `pinned`, LRU eviction, `concurrency_groups`, idle and busy watchdogs. Against it on this node: every runtime is its own build. Whether its Intel images carry a compute runtime new enough for the B70 is unverified, and its docs do not say which Python backends have Intel variants. DNABERT-2, MADLAD and MedCPT would each be a custom gRPC backend. **The fallback** if §14's proof fails |
| Xinference | choosing the device at launch (`n_gpu=None` = CPU) is exactly the ask. But it is NVIDIA-centred with no documented XPU support, and it has open bugs where `--device cpu` still ends up on the GPU |
| GPUStack | a cluster manager, centred on vLLM and SGLang. Intel Arc support not evident |
| OpenVINO Model Server | runtime add and remove through `config.json`, `target_device` per model, embeddings and rerank on the Arc. OpenVINO models only, so it is one runtime, not the manager |
| Ollama | GGUF only, and itself a swapper |
| Triton | CUDA-centred; PyTorch on XPU is not a supported backend |

## 4. The design: one policy, in a front; llama-swap runs the processes

One TrueNAS app, `rage`, one image, one published port, `/dev/dri`, a
read-only models directory and a state directory. The image holds:
- the B70 compute runtime (the verified recipe);
- llama.cpp's SYCL build, copied from the image we pin today, with its
  oneAPI libraries under its own prefix;
- one torch XPU, and a venv per Python server created with
  `--system-site-packages`, each with its own pins (§9);
- llama-swap, pinned by version;
- the front, `app/rage/`.

Each model runs as a child process. No docker socket.

**The front** (`python -m app.rage`; FastAPI and httpx, already
dependencies; it imports no GPU stack, invariant 7) owns the published
port and every decision: admission, load and unload (§7), the run
identity (§8), the card's clock (§11), the admin surface (§12). It
proxies `/upstream/<model>/…` to the child and adds `Louise-Run` to
every answer, streamed ones included. It never batches, coalesces or
reorders requests: the gate measures batch invariance at the peer's own
batching (03 §3.3).

**llama-swap holds no policy.** Every model and device is its own
entry, `swap: false`, `exclusive: false`, `ttl: 0`, no groups, no
`matrix`, no preload hook, listening on `127.0.0.1` only. It never
loads, evicts or swaps by itself; only the front reaches it. Its yaml is
generated from `rage.toml` (§5) and never edited by hand. Exit
criterion: if finding a child's pid (§8) or the double hop costs more
than a week of the first proof, the front supervises directly with
`subprocess.Popen`; neither the API nor `rage.toml` changes.

**A URL names a model, never a device.** A collection's `[encoder] url`
is `http://<node>:<port>/upstream/bge-m3` (07 §7.8). Where the model
runs is RAGE's placement — the entry's `device`, or a load call — and a
device change is a new run, which the gate tests by itself. Nothing in
a collection's file follows it.

**Ollama and TEI go.** The chat model is a GGUF on the same
llama-server; the CPU encoders become CPU placements. This is not free
on Louise's side: `app/llm.py` speaks Ollama's `/api/chat` today and
moves to `/v1/chat/completions`, with the context size fixed at launch
(`-c 16384`) and thinking turned off through `chat_template_kwargs`.
Verify the chat GGUF carries its template, else `--chat-template-file`.

**What Louise's clients gain.** They read `Louise-Run` and check it
before every write; they send `Louise-Level` (09 §9.12). `RAGE_URL`
(07 §7.8) gives the System page the model list and its controls.

## 5. `rage.toml` — the one desired state

The file the operator and the System page edit (tier 2, invariant 10;
the page calls `PUT /louise/models/<id>` and the front writes the file;
no desired state enters a database).

```toml
[rage]
reserve_mib = { xpu = 2048, cpu = 4096 }
preload     = ["bge-m3", "qwen3-8b", "medcpt-article", "medcpt-query"]

[models.bge-m3]
engine  = { name = "llama.cpp", build = "b10630", variant = "sycl" }
weights = "/models/bge-m3-FP16.gguf"
device  = "xpu"                                   # default placement
memory  = { xpu = 1400, cpu = 2600 }              # MiB, MEASURED
env     = { LD_LIBRARY_PATH = "/opt/llama/lib" }
health  = "/health"
cmd.xpu = ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
           "-c", "8192", "-ub", "8192", "-b", "8192", "-np", "1", "-ngl", "99",
           "--host", "127.0.0.1", "--port", "${PORT}"]
cmd.cpu = ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
           "-c", "8192", "-ub", "8192", "-b", "8192", "-np", "1", "-ngl", "0",
           "--host", "127.0.0.1", "--port", "${PORT}"]

[models.madlad]
engine  = { name = "louise-madlad-server", build = "2", variant = null }
weights = "/models/madlad400-3b-mt-bf16"          # a directory: 09 §9.3's manifest digest
device  = "xpu"
memory  = { xpu = 8200, cpu = 12000 }
env     = { HF_HUB_OFFLINE = "1", BATCH = "32", MAX_SEG_TOKENS = "96" }
cmd.xpu = ["/opt/venv-madlad/bin/python", "-m", "servers.madlad", "--weights", "${WEIGHTS}",
           "--device", "xpu", "--host", "127.0.0.1", "--port", "${PORT}"]
cmd.cpu = ["/opt/venv-madlad/bin/python", "-m", "servers.madlad", "--weights", "${WEIGHTS}",
           "--device", "cpu", "--host", "127.0.0.1", "--port", "${PORT}"]
```

- One complete `cmd` per device. The engine's `args` (09 §9.3) is the
  template of the device that ran, `${WEIGHTS}` and `${PORT}` left as
  placeholders, so nothing is inferred.
- The three Python servers gain `--weights`, `--device`, `--host` and
  `--port` argv and a `build` string, and bind `127.0.0.1`: llama-swap
  does not expand `${PORT}` inside `env`, and 09 §9.3 wants nothing
  host-specific there.
- `name`, `build`, `variant` are readable and declared; `image`, `args`,
  `env` and `weights` are what binds.
- `preload` is applied through admission at start and whenever the file
  changes. A runtime load or unload lasts until the next apply.
- The figures above are placeholders until measured (§16).

## 6. The API

```
GET    /louise/models                 → [{id, state: absent|loading|ready|unstated|failed,
                                          device, run, pid, memory: {declared, observed},
                                          idle_s, since}]
POST   /louise/models/<id>/load       {device?}  → 202 {run} · 409 loaded elsewhere · 503 {reason, loaded}
POST   /louise/models/<id>/unload     → 200 · 404
POST   /louise/models/<id>/restart    → 202 (the fan workaround, §11; operator only)
GET    /louise/runs/<run_id>          → 09 §9.12's object · 404
GET    /louise/gpu                    → the card's live sensors and the clock's state (§11)
PUT    /louise/models/<id>            → write the entry to rage.toml; 409 while loaded
DELETE /louise/models/<id>            → remove it; 409 while loaded
/upstream/<id>/…                      → the child, with Louise-Run
```

| the ask | what does it |
|---|---|
| add / remove | `PUT` / `DELETE`, or the file; the System page's form later |
| load / unload | the two `POST`s, the first request, or `preload` |
| list | `GET /louise/models` |
| CPU or GPU | the entry's `device`, or `{device}` on a load |
| concurrent or sequential | admission (§7): concurrent wherever it fits |

## 7. Concurrent by default; the front admits every load

- **Co-residence costs only memory.** The node already runs eight
  servers at once with 8.5 GB spare, and the card is time-shared
  whatever we do. `embedder-pool-spec.md` §3: idle is not a reason to
  unload. So `ttl: 0`, and the query encoders are preloaded.
- **Fits** = the declared `memory` of what is loaded on that device,
  plus this model, plus the reserve, ≤ the device's capacity. The card's
  capacity from the `xe` device query (the probe's ioctl, reused; RAGE
  has `/dev/dri`); the CPU's from the container's `memory.max` and
  `memory.current`. After every load the front records the observed
  peak — its own children's DRM fdinfo, their RSS — and shows it beside
  the declaration, so a figure that drifts is visible.
- **Does not fit** → `503` with `Retry-After` and what is loaded; never
  an eviction. Eviction is Louise's decision, explicit and event-driven
  (`embedder-pool-spec.md` §3–4); the front gives the facts and the
  lever. For Louise a `503` is already a deferral
  (`worker._ENDPOINT_BUSY_STATUS`).
- **Autoload stays, through admission.** A request for an unloaded model
  admits on its default device; fits → load, wait, serve; else `503`.
  Single-flight per model.
- **Overcommit does not fail on `xe`.** It moves weights to host RAM
  silently: MedCPT-Article, 2026-09-18, which cost nothing measurable
  for a 110M model. For Qwen3-4B or MADLAD it is unmeasured, and no
  policy relies on it until it is.
- **Sequential is the fallback** for creation encoders that do not fit
  together: `gpu-time-slots.md`'s hourly slot, driven by Louise through
  load and unload at a batch boundary. Not built with RAGE.

## 8. The run identity, computable

09 §9.12 (amended 8bbc66a) fixes the rules; this is how RAGE follows
them.
- **`image`.** The deploy pins `image: <registry>/rage@sha256:…` and
  sets `RAGE_IMAGE` to the same digest. The front refuses to start
  unless it matches `^sha256:[0-9a-f]{64}$`. The log also carries the
  digest of each child's `/proc/<pid>/exe`, as evidence.
- **`args` and `env`.** The entry's `cmd` template for the device used,
  and its declared `env`. Once the child is ready the front scans
  `/proc/*/cmdline` for exactly one process whose argv equals the
  template with `${WEIGHTS}` → the path and `${PORT}` → an integer, and
  whose `/proc/<pid>/environ` carries every declared variable at its
  value. No match, or two: the model is **unstated** — its answers carry
  no `Louise-Run`, which 03 §3.3 defines as "search with it, write
  nothing" — and the log says why.
- **`weights`.** Hashed at every launch, streamed, while the child
  loads; no cache. A directory by 09 §9.3's manifest digest, remote
  code included. DNABERT-2's Hugging Face snapshot is materialised under
  `/models/` once.
- **`device`, `driver`.** The card as its driver names it (the probe's
  pci.ids lookup), `"cpu"` on a CPU entry. `driver` = kernel release,
  `xe` module version, the compute-runtime package versions, the GuC
  firmware where readable; `"cpu"` on a CPU run.
- **`run_id`** once per launch, persisted in RAGE's own sqlite with the
  object, so `GET /louise/runs/<id>` survives a restart of the front.
  The same entry relaunched unchanged states the same run.

## 9. One failure domain, bounded

- **`mem_limit` on the app**, so an out-of-memory is the container's
  cgroup and never the host's.
- **Who dies.** Docker's default capabilities exclude
  `CAP_SYS_RESOURCE`, so nothing goes below 0 in `oom_score_adj`;
  raising is free. The front writes `+800` to each child's
  `/proc/<pid>/oom_score_adj`, the largest highest, and stays at 0 with
  llama-swap. The killer takes the child that grew; the rest answer; the
  front marks it `failed`. (The OOM killer took the CPU TEI encoder four
  times on 2026-09-15; admission is the real bound, this is the net.)
- **One torch, three pin sets.** madlad pins `transformers==4.46.3`
  (5.x translates to `ě ě ě`), dnabert2 `transformers<4.50`, medcpt
  `openvino==2026.3.1` and `tokenizers==0.22.2` with no torch at all.
  Each keeps its proven pins in its own venv over one system torch.
- **A rebuild is a revalidation.** `image` is in the run, so a new image
  re-gates every space on the node. Accepted; §14 measures one full
  gate per encoder so the price is known. A model restart is per child
  and costs the others nothing.

## 10. Weights are placed, never fetched (invariant 5)

`/models` is bind-mounted read-only. Every child runs offline
(`HF_HUB_OFFLINE=1`) with its weights by absolute path. An entry whose
path does not exist is `absent` and refused at load, the path in the
reason. Placing a file is the transfer manager's work or the
operator's, as today; a `louise model fetch` drawing from the token
bucket is a later step.

## 11. What RAGE inherits

- **The clock.** The front holds the card's `min_freq` at `rp0_freq`
  while any request at `Louise-Level` `translate` or above is in flight
  (scheduling.md §12 measured why: 87–92 s against 64 s for one chat
  call). The claim in `state/node.sqlite` and the separate governor app
  (`deploy/truenas-gpu-governor.yaml`) retire together. The frequency
  bind, by its colon-free `/sys/class/drm` path, and the sampler
  (`gpu_samples`, a week at 10 s) move into RAGE's state directory;
  the System tab reads `GET /louise/gpu`.
- **The fan.** `xe` leaves it high after a workload until a GPU process
  restarts. `POST /louise/models/<id>/restart`, by the operator from the
  System page, restarts one child and nothing else (operator,
  2026-09-20: never restart other apps). Never automatic.
- **The panel.** `gpu_probe` keeps its per-process rows. To label them
  by model the web joins the probe's host pids with RAGE's container
  pids through `NSpid` in `/proc/<pid>/status`, which the probe reads
  from the host namespace. One field added to the probe's snapshot.

## 12. The admin surface

Load, unload, restart, `PUT` and `DELETE` require
`Authorization: Bearer` with the token read from `ADMIN_TOKEN_FILE`,
mounted by path (invariant 8). `/upstream/…`, `GET /louise/models`,
`/louise/runs/…` and `/louise/gpu` are open on the LAN as the model
ports are today. SIGTERM forwards to every child; answers in flight
finish within `stop_grace_period` (60 s). A request cut short is a
deferral for Louise; the unit of work is Louise's, never RAGE's.

## 13. The amendments, all made on `main`

- **0d85f7f** — the engine, `{name, build, variant, image, args, env,
  device}`, frozen in the Codex inside `collection_id`; `space_id`
  hashes the weights and the settings, never the engine. A newer engine
  or RAGE build joins by passing the gate. (The first draft of this
  section was wrong on one point: rebuilding the image never re-keyed an
  existing collection, whose Codex is fixed.)
- **dd9bef4, 27a1332** — validate a run once; RAGE states the run on
  every answer; the fingerprint is evidence in the statement, not a
  check repeated after every batch (12.5–17 s per commit on gutenberg,
  [`fingerprint-implications.md`](fingerprint-implications.md)).
- **8bbc66a** (2026-09-19) — the run identity made computable, the
  weights digest of a directory, `Louise-Level`, the model's URL,
  `RAGE_URL`.
- **98ee4dc** (2026-09-19) — quarantine: a peer's own tests come before
  any use of a copy, and a run not yet tested answers no query. It is
  wider than RAGE and is migration step 14; `rage-review.md` §18 has the
  reasoning. For RAGE it means the web never handles a pending run: the
  worker learns the runs (from `GET /louise/models` when `RAGE_URL` is
  set, from its own answers otherwise), gates any new one, and the
  space's collections are in quarantine meanwhile.

## 14. First proof — done when

- One image builds with the compute runtime, llama.cpp b10630, one
  torch and three venvs, llama-swap pinned; it is published by digest
  where a stranger can pull it.
- From that image: bge-m3 and MedCPT-Article on llama.cpp, MADLAD and
  DNABERT-2 on torch, the MedCPT cross-encoder on OpenVINO, each on the
  Arc and each on the CPU; a CPU placement holds no DRM fd. One
  compute-runtime version satisfies all three runtimes — the children
  are separate processes, so the shared risk is the Level Zero loader
  and driver, not their oneAPI libraries. DNABERT-2 is a runtime check
  only, ungated: DNA collections are a non-binding work in progress
  (operator, 2026-09-19) and no DNA calibration set exists.
- `Louise-Run` on every answer, streamed ones included; `GET
  /louise/runs/<id>` returns 09 §9.12's object; two restarts of one
  entry give one run id; a changed flag, weights file or image gives
  another.
- Load, unload and device change timed per model; a full calibration
  gate timed per encoder.
- Vectors from every Arc placement match today's endpoints within each
  space's tolerance; wikipedia's gate passes through `/upstream/…`.
- The OOM test: a CPU child driven past RAM dies alone; the front and
  llama-swap survive and the other models keep answering.

## 15. The order of work

[`migration-plan.md`](migration-plan.md) step 13, parts a–f: Codex v3;
the servers' argv; `app/rage/` with its tests; the published image and
§14 on the node; the clients; the retirements. Quarantine is step 14.
`app/verify/backend_gate.py` stays for legacy collections, which have no
Codex to gate against, and retires with them at step 12.

## 16. Values the operator still gives

The registry and image name, and a registry login on the node stored by
path; RAGE's published port (30437–30448 are taken); its state
directory; which qwen3 GGUF serves chat and where it lies. Each model's
`memory` is measured on the node, not chosen.
