# RAGE — the RAG Engine: one container for every model

*Named by the operator, 2026-09-20. This note called it "the model pool"
until then.*

*Operator, 2026-09-19: "each plugin uses one to three models, on
llama.cpp, Ollama, PyTorch. I want one container (a TrueNAS app) to
add/load/unload/remove models, run them sequentially or concurrently,
on CPU or GPU as chosen at load."* **Design accepted; the front is built
and tested (2026-09-19, §17); nothing has run on the node yet.** It
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
read-only models directory and a state directory. The image
(`deploy/rage/Dockerfile`) is BUILT ON the Intel llama.cpp image we pin
today, by digest — the base `deploy/medcpt-rerank` already runs OpenVINO
on, on this card — and adds:
- one torch XPU, and a venv per Python server created with
  `--system-site-packages`, each with its own pins (§9);
- our three servers, one source each, started by
  `deploy/rage/servers/launch.py`, which turns an argument vector into
  the variables each server reads and runs it in the same process;
- llama-swap, pinned by version and checksum;
- the front, `app/rage/`, with the two modules it borrows and no more of
  the platform.

Each model runs as a child process. No docker socket.

**The front** (`python -m app.rage`; FastAPI and httpx, already
dependencies; it imports no GPU stack, invariant 7) owns the published
port and every decision: admission, load and unload (§7), the run
identity (§8), the card's clock (§11), the admin surface (§12). It
proxies `/upstream/<model>/…` to the child and adds `Louise-Run` to
every answer, streamed ones included. It never batches, coalesces or
reorders requests: the gate measures batch invariance at the peer's own
batching (03 §3.3).

**llama-swap holds no policy**, which takes one thing to say in its own
language: ONE group holding every entry, `swap: false`, `exclusive:
false`. Not "no groups", as this note first said: a model in no group
falls into llama-swap's default, which its own example config spells
out — "only one model runs at a time across the entire instance" — so
no groups would have swapped every model out for the next (found while
writing `app/rage/swapyaml.py`, 2026-09-19). Every model and device is
its own entry, `ttl: 0`, no `matrix`, no preload hook, listening on
`127.0.0.1` only. It never evicts or swaps by itself; only the front
reaches it. Its yaml is
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
device_names = { xpu = "Intel(R) Arc(TM) Pro B70 Graphics" }   # 09 §9.3's `device`
pci          = { xpu = "0000:03:00.0" }   # where the card is on the bus: its driver is read there (§18)
capacity_mib = { xpu = 32600 }                    # the CPU's is the container's limit, read
reserve_mib  = { xpu = 2048, cpu = 4096 }
preload      = ["bge-m3", "medcpt-query"]

[models.bge-m3]
engine  = { name = "llama.cpp", build = "b10630", variant = "sycl" }
weights = "/models/bge-m3/bge-m3-FP16.gguf"
device  = "xpu"                                   # default placement
memory  = { xpu = 1400, cpu = 2600 }              # MiB, MEASURED
host    = { xpu = 1700 }                          # host RAM it holds while on the card (§7)
cmd.xpu = ["/app/llama-server", "-m", "${WEIGHTS}", "--embeddings", "--pooling", "cls", "-ngl", "999",
           "-c", "8192", "-np", "1", "-b", "8192", "-ub", "8192", "-fa", "on", "-t", "6", "--metrics",
           "--host", "127.0.0.1", "--port", "${PORT}"]  # today's flags, copied exactly
cmd.cpu = [ … the same with "-ngl", "0" … ]

[models.madlad]
engine  = { name = "louise-madlad-server", build = "2" }     # no variant: absent is null
weights = "/models/madlad/madlad400-3b-mt-bf16"   # a directory: 09 §9.3's manifest digest
device  = "xpu"
memory  = { xpu = 8200, cpu = 12000 }
env     = { BATCH = "32", MAX_SEG_TOKENS = "96" }
cmd.xpu = ["/opt/venv-madlad/bin/python", "-m", "servers.launch", "madlad", "--weights", "${WEIGHTS}",
           "--device", "xpu", "--host", "127.0.0.1", "--port", "${PORT}"]
cmd.cpu = [ … the same with "--device", "cpu" … ]
```

The whole of it, for every model the node runs:
`deploy/rage/rage.example.toml`, which a test loads.

- One complete `cmd` per device. The engine's `args` (09 §9.3) is the
  template of the device that ran, `${WEIGHTS}` and `${PORT}` left as
  placeholders, each an argument of its own, so nothing is inferred.
- **A card is declared, never probed**: its name and its capacity are
  required for every card a model names. The name enters every run
  identity; the size is what admission places against; neither changes
  while the card is in the machine. Reading them would mean importing
  the GPU probe, the one module that sees the host's processes, which
  nothing may import (`tests/test_gpu_panel.py`).
- **Two places, one configuration.** `rage.toml` is hand-written and
  commented, and the front never rewrites it. A model added through the
  API lands whole in `rage.d/<id>.toml`; an id in both is an error, and
  `PUT`/`DELETE` refuse a model that lives in `rage.toml`.
- The three Python servers stay driven by their environment, as apps of
  their own need; `servers/launch.py` takes `--weights --device --host
  --port` and sets it. llama-swap does not expand `${PORT}` inside
  `env`, and 09 §9.3 wants nothing host-specific there. The servers bind
  the `HOST` they are given: `127.0.0.1` under RAGE.
- `name`, `build`, `variant` are readable and declared; `image`, `args`,
  `env` and `weights` are what binds.
- `preload` is applied through admission at start and whenever the file
  changes. A runtime load or unload lasts until the next apply.
- The figures above are placeholders until measured (§16).

## 6. The API

```
GET    /louise/models                 → [{id, state: absent|unloaded|loading|ready|unstated,
                                          device, run, pid, reason, memory: {declared, observed},
                                          idle_s, since, source}]
POST   /louise/models/<id>/load       {device?}  → 200 the row, once it is up · 409 loaded on
                                          another device · 503 {error, admission: {loaded, free_mib, …}}
POST   /louise/models/<id>/unload     → 200 · 404
POST   /louise/models/<id>/restart    → 200 the row (the fan workaround, §11; operator only)
GET    /louise/runs/<run_id>          → 09 §9.12's object · 404
GET    /louise/gpu                    → the clock's state (§11)
PUT    /louise/models/<id>            → write rage.d/<id>.toml; 409 while loaded, or if rage.toml defines it
DELETE /louise/models/<id>            → remove it; the same 409s
/upstream/<id>/…                      → the child, with Louise-Run; 503 + Retry-After when it does not fit
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
- **Two budgets, and every load is checked against the host's.** A
  model on the GPU is not free on the host. Measured on the node,
  2026-09-19, from each per-model container's cgroup (`memory.peak`
  since it started):

  | container RAGE replaces | host RAM at rest | peak |
  |---|---|---|
  | MADLAD | 3,307 MiB | 8,563 |
  | Ollama, qwen3:8b (unloaded at the time) | 132 | 5,648 |
  | Qwen3-Embedding-4B | 647 | 3,047 |
  | bge reranker | 465 | 1,610 |
  | bge-m3 | 446 | 1,602 |
  | MedCPT cross-encoder | 589 | 781 |
  | MedCPT article / query | 286 / 295 | 691 / 494 |
  | the governor | 40 | 44 |
  | **together** | **6.1 GiB** | **22.0 GiB** |

  So each entry declares `host.<card>`, the host RAM it holds while on
  the card — the PEAK, because loads overlap and the peak is when the
  out-of-memory killer comes — and admission counts it: `memory.cpu` for
  a model placed on the CPU, `host.<card>` for one on a card, all
  against the container's limit. The first version of `admission.py`
  counted host RAM for CPU placements only, and would have let CPU
  models fill what the GPU models already held; the measurement found
  it the same day. The card is asked first, then the host.
- **`mem_limit: 28g`** on the app, from the same figures: 22.4 GiB
  declared for everything on the card, 2 reserved, ~3.6 left for a small
  CPU placement. The bound that matters is the machine — 61.9 GiB, of
  which Louise's worker peaks at 20.6 and the rest takes ~4 — so 28
  leaves ZFS at least 9 GiB of cache in the worst case (18.5 today, and
  it gives memory back slowly: the TEI encoder's OOM loop of 2026-09-15).
  The first draft of the app file said 40g, which with Louise's peak is
  more than the machine has.
- **Fits** = the declared `memory` of what is loaded on that device,
  plus this model, plus the reserve, ≤ the device's capacity. The card's
  capacity is declared (§5); the host's is the container's `memory.max`,
  read. A model still loading counts in full from the moment it is
  admitted, so two loads in the same instant cannot both squeeze in.
  After every load the front records the observed peak — its own
  children's DRM fdinfo, summed once per DRM client, and their RSS — and
  shows it beside the declaration, so a figure that drifts is visible.
  The declaration decides, never the observation: a model that spilled
  to host RAM holds less than it needs.
- **Does not fit** → the front first gives up IDLE models ranked strictly
  BELOW the one asked for, the lowest `priority` first, and asks again;
  still no → `503` with `Retry-After` and what is loaded. For Louise a
  `503` is already a deferral (`worker._ENDPOINT_BUSY_STATUS`).
  **AMENDED 2026-09-21.** This line used to read "never an eviction.
  Eviction is Louise's decision" — because only Louise knows whose batch
  is in flight. Louise never made the decision (`embedder-pool-spec.md`
  §9 carried it as open), so a full card admitted nothing new: that night
  DAR-legal-it was refused 6,200 MiB with 1,125 free while the translator
  sat idle on 8,200. Operator: "RAGE should free VRAM on demand", in the
  order translator (0) → chat (1) → rerankers (2) → encoders (3). What
  replaced Louise's knowledge is `_idle` — nothing in flight and nothing
  asked for `idle_s`; an embedding batch asks its model every fraction of
  a second, so 60 s of silence means it has stopped. Three rules keep it
  from doing harm, each with a test that fails without it
  (`tests/rage/test_evict_for_load.py`): **strictly below** — the
  translator, asked for by a search, cannot push out chat and a reranker,
  and two rerankers of one rank cannot evict each other in turn; **only
  if enough** — nothing is given up unless the ledger, asked with those
  models gone, says yes; **never in use** — `unload(only_if_idle=True)`
  leaves a model a request reached meanwhile. The same ranking now orders
  a stranger's squeeze (§19), which chose by fewest MiB and so always gave
  up the reranker every search uses. And a model ranked below
  `pressure.restore_min_priority` (3) no longer comes back by itself after
  a squeeze: the reranker's did, and the next burst of work pushed it off
  the card again — given up at 02:18:56, back at 02:21:05 with nobody
  asking, given up at 02:23:59, back at 02:26:10.
- **Autoload stays, through admission.** A request for an unloaded model
  admits on its default device; fits → load, wait, serve; else `503`.
  Single-flight per model.
- **Overcommit does not fail on `xe`.** It moves weights to host RAM
  silently: MedCPT-Article, 2026-09-18, which cost nothing measurable
  for a 110M model. For Qwen3-4B or MADLAD it is unmeasured, and no
  policy relies on it until it is.
  **MEASURED THE HARD WAY, 2026-09-20**: a container OUTSIDE RAGE took
  21.4 GiB of the card; every resident model went to host RAM (chat: 476
  KiB on the card, 7,403 MiB off it), the host ran out of memory and the
  kernel killed three models. Admission counts RAGE's own declarations
  and cannot see a stranger on the card ([`STATUS.md`](STATUS.md),
  2026-09-20 afternoon). OPEN: read the card's real free memory before a
  load, and refuse when it contradicts the ledger. **BUILT the same
  day: §19.**
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
- **`device`, `driver`.** The card as its driver names it, declared in
  `rage.toml` (§5); `"cpu"` on a CPU entry. `driver` = kernel release,
  the card's module (`xe`) with its version, and the compute-runtime
  package versions, which the image writes to
  `/etc/rage/runtime-versions` when it is built so that no package
  manager runs at start; `"cpu"` on a CPU run.
- **Checked twice around an answer.** Before a request is passed on, the
  pid that was matched must still show the same argument vector;
  llama-swap restarts a child that died on its next request, and that is
  another launch — hashed and matched again before any run is named. And
  once the answer's headers are in, the pid is checked again: a child
  replaced mid-request gets no `Louise-Run`, and Louise writes nothing
  from it.
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
- **The load amendment** (operator, 2026-09-28) — an engine that is a
  gRPC backend (§21) states its `LoadModel` message in its own field,
  `load`, instead of inside `args`. Codex version 4 is version 3 plus that
  field and is written exactly when an engine carries it, so no existing
  Codex, `engine_id` or run changes: every engine without `load` is the
  same bytes (CLAUDE.md rule 1). The calibration statement
  (`louise-calibration-run`, 09 §9.12) follows the same rule: version 3
  exactly when its engine carries `load`, version 2 otherwise, so no kept
  statement is re-signed.
- **A validation is this node's, and is re-checked on start** (operator,
  2026-09-28). Validations stay valid across restarts (dd9bef4 stands).
  Two guards against one planted in `state/calibration/`:
  - **Signed by this node.** A statement counts only when its `key` is
    this node's own. An envelope verifies under the key written inside it,
    so before this any key at all made a "pass" count. That was a defect in
    `app/hold/runs.py`, and it is fixed.
  - **Re-checked once per process.** The first time a kept statement is
    relied on, its own sixteen sampled texts are embedded again by the run
    and compared with the reference, as the gate did. If it fails, the
    statement is set aside (`state/calibration/set-aside/`) and the run is
    gated from scratch.
  - **Cost.** Sixteen texts per run and role per process start. A write
    that arrives first is deferred until the stage has re-checked.
  - **The web process** makes no re-check of its own: a search must not
    wait for sixteen embeddings. It relies on a kept statement only when
    the worker, in its current start, has re-checked that very statement
    or gated the run itself. The record is `state/rechecked.json`, emptied
    at every worker start and listing each statement by its body's digest.
    Until then the vector leg is withheld and the text leg goes on (when
    quarantine is enforced). A command-line process never writes the
    record.
  - **The limit.** The record, like the statements, is a file in `state/`.
    Against someone holding the node's key it adds nothing: they can write
    both. Keeping the key out of reach (layer 2) is what stands there.
  - **Layer 2: the key out of reach.** Deployment, not code: the key in
    its own dataset, outside `data/` and any share, mode `0400`, mounted
    read-only into web and worker only. `deploy/truenas-app.yaml` gives the
    steps. It guards against file-level writes to `state/`, not against
    code running inside the node.
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

## 16. The operator's values (2026-09-19), and the cut-over

| value | given |
|---|---|
| RAGE's folder | `/mnt/ssd5/apps/louise-rage` — `config/`, `state/`, `secrets/` under it. Not created yet |
| the port | **30437**: one the per-model apps used (gutenberg's encoder), free once they stop |
| the chat model | the same file Ollama serves as `qwen3:8b`: `…/ollama_data/models/blobs/sha256-a3de86cd…686f`, 4,983 MiB. Ollama names a blob by its SHA-256, so the file's name IS the weights digest. Checked: it carries `tokenizer.chat_template`, which knows `enable_thinking`. Ollama's `params` for it (top-k 20, top-p 0.95, repeat penalty 1) are the entry's flags |
| `mem_limit` | **28g**, from the measurements of §7 |
| the admin token | a FILE under `secrets/`. Louise has none today: she gets hers as an environment value (`ADMIN_TOKEN`; `ADMIN_TOKEN_FILE` is unset), which invariant 8 tolerates only where a path is not possible |
| **the registry** | **still to give**: where the image is published, and a login on the node kept as a file |

**The cut-over** (operator: "shut them off before loading RAGE"). The
per-model apps and RAGE never run together: every model would sit on the
card twice, and RAGE takes one of their ports. So 13f is one evening,
not "one at a time" as first planned, and its order is in
`deploy/truenas-rage.yaml`:
0. **first, while the apps still answer**, record from each of today's
   endpoints the vectors §14 compares RAGE's against. Once they are off
   there is nothing left to compare with;
1. the folder, `rage.toml`, the token file;
2. `node pause` — an encoder that is away is a deferral, never an error
   on a document;
3. stop `embed-gpu-qwen3-4b`, `embed-gpu-bge-m3`, `embed-gpu-medcpt`,
   `translate-gpu-madlad`, `louise-gpu-governor`. `ollama` is not one of
   them: chat moved by itself afterwards (`CHAT_API=openai`,
   `CHAT_URL=…/upstream/chat`; done 2026-09-19 night, 13 tokens/s against
   Ollama's 8–9) and `chat` joined `preload`;
4. install RAGE; 5. switch the URLs — three collection files edited in
   place, `TRANSLATE_URL`, `RAGE_URL`; 6. `node resume`.
Back out: stop RAGE, start the five apps, restore the URLs. The apps are
deleted only by `legacy-to-delete.md`'s rule.

One thing the inventory turned up: gutenberg's encoder runs llama.cpp
**b10603**, the others b10630. RAGE's image holds one build, so for
gutenberg's space RAGE is another engine, and the gate tests it like any
other (03 §3.3). Each model's `memory` stays a placeholder until
measured under RAGE.

## 17. As built (2026-09-19) — steps 13b and 13c; 13d's files

| piece | where | proved by |
|---|---|---|
| `rage.toml` and `rage.d/`, validated by rule | `app/rage/model.py` | `tests/rage/test_model_and_yaml.py` |
| the yaml: one group, never swaps, never evicts | `app/rage/swapyaml.py` | the same, a golden |
| the weights digest, the engine, the run, the match against `/proc` | `app/rage/runs.py`, `procfs.py` | `tests/rage/test_runs.py` |
| admission | `app/rage/admission.py` | `tests/rage/test_admission_and_clock.py` |
| the clock by `Louise-Level`; the governor's class lifted out | `app/rage/clock.py`, `app/gpu_clock.py` | the same; `tests/test_gpu_governor.py` unchanged and green |
| single-flight loads, a place held from admission, liveness, apply | `app/rage/placement.py` | `tests/rage/test_placement.py` |
| the HTTP surface, the admin token, `Louise-Run` on streamed answers | `app/rage/front.py` | `tests/rage/test_front.py` |
| the runs kept across a restart of the front | `app/rage/store.py` | through the two above |
| boot: refuse a tag, SIGTERM handled before uvicorn is up | `app/rage/__main__.py` | `tests/rage/test_runs.py`, `tests/test_process_signals.py` |
| the launcher of our three servers; they bind `HOST` | `deploy/rage/servers/launch.py`; `deploy/*/server.py` | `tests/rage/test_servers_launch.py` |
| the image, the example config, the TrueNAS app — WRITTEN, NOT BUILT | `deploy/rage/Dockerfile`, `rage.example.toml`, `deploy/truenas-rage.yaml` | `tests/rage/test_deploy_files.py` holds them to the code |

95 tests, 6 s. Three pre-existing guards shaped the code rather than
the other way round: nothing imports the GPU probe (so a card is
declared); no GPU stack under `app/` (the front launches processes and
imports none); every image of ours answers SIGTERM as PID 1 (the front's
boot now does, and the guard learnt the `python -m` form and asks for a
handler, not a mention).

**Not done, and why.**
- **13d, the image and the proof (§14)** — needs the node, the operator's
  values (§16) and a registry: an outward-facing build and publish. What
  only the node can settle: that torch sees the card through this base's
  Level Zero loader; every GPU number of §14; llama-server itself, which
  aborts on a host with no Intel GPU even for a CPU placement ("can not
  find preferred GPU platform": the SYCL backend initialises first).
  Whether llama-swap restarts running children when its yaml changes is
  also unknown; the front copes either way (§8, checked twice), but the
  proof should time it.
- **THE IMAGE WAS TRIAL-BUILT LOCALLY (2026-09-19), never pushed, and the
  build found two defects that would have met the operator on the
  cut-over evening.** All twenty steps build from the pinned base
  (`server-intel-b10630` = `sha256:37dd060b…f3fc7`, 3.5 GB compressed; the
  image is 24.8 GB). (1) EVERY llama-server died at exec:
  "libllama-server-impl.so: cannot open shared object file". Its runpath
  is `/app/build/bin:` — a directory that does not exist, and an empty
  entry, which the loader reads as the current directory — so it loads
  only from the base's WORKDIR, and llama-swap starts children from
  RAGE's. Fixed by registering `/app` with the loader (`ldconfig`), and
  the build now asserts it; never by touching `LD_LIBRARY_PATH`, which is
  the environment every vector on the node was produced under. (2)
  `import torch` died: "undefined symbol: urDeviceWaitExp". torch's XPU
  wheels bring their own SYCL runtime under `/usr/local/lib`, and the
  base's `LD_LIBRARY_PATH` puts oneAPI 2025.3's older `libur_loader` in
  front of it. Fixed where it belongs: a torch model DECLARES
  `LD_LIBRARY_PATH = "/usr/local/lib"` in `rage.toml`, so it is in that
  model's run identity and in no other process; `tests/rage/
  test_deploy_files.py` holds both. After the fixes, in the image: torch
  2.14.0+xpu with transformers 4.46.3 (madlad) and 4.49.0 (dnabert2),
  openvino 2026.3.1, the three server modules import, the runtime
  versions file reads `libze-intel-gpu1 26.18.38308.1-0`, `libze1 1.27.0`.
- **The front, end to end, inside that image**: `python -m app.rage` over
  the real llama-swap with a stub model — the first request loads the
  child (0.40 s) and the answer carries `Louise-Run`; `GET
  /louise/runs/<id>` returns `{driver, engine, weights}` and its canonical
  JSON hashes to the id, which is what Louise's client verifies; a second
  answer names the same run; an admin route without a token is 401;
  SIGTERM ends llama-swap and exits 0.
- **Proved locally meanwhile (2026-09-19), with the real llama-swap v256
  binary and stub children, in the dev container.** The release archive
  is named as the Dockerfile has it, holds `llama-swap` at its root, and
  hashes to its published checksum (the value is in the Dockerfile's
  comment). The binary ACCEPTS the yaml `swapyaml.render` writes from
  `rage.example.toml` — nine models, eighteen variants, one group. With
  two stub models: a `GET /upstream/<variant>/health` loads the child
  (0.26 s) with `${PORT}` filled in from `startPort`; BOTH members then
  run at once, so the one group does not swap; a second request reuses
  the same process; `POST /api/models/unload/<variant>` stops that child
  and leaves the other; the child's parent is llama-swap. And the run
  identity holds against a real `/proc`: `runs.match` finds exactly the
  child llama-swap started, by its argument vector, with its port; the
  unloaded model matches nothing; `oom_score_adj` is writable for it.
  llama-swap adds no header of its own to a proxied answer.
- **The sampler** (`gpu_samples`) stays with the governor app until 13f
  retires it; the front holds the clock and reports it, no more.
- **13a and 13e — BUILT since (2026-09-19)**: Codex v3, and Louise's
  clients. Every model request sends `Louise-Level`; for a version-3
  collection every embedding answer's `Louise-Run` is checked before its
  vectors are written, and a run the peer has not tested is gated at
  once through `GET /louise/runs/<id>`, whose object must hash to the id
  it was asked for (`app/run_check.py`, `app/hold/runs.py`;
  [`migration-plan.md`](migration-plan.md) 13e). Today's collections are
  version 2 or legacy, so RAGE still serves today's Louise unchanged,
  behind `/upstream/<model>` URLs.

## 18. No manufacturer in RAGE (operator, 2026-09-20)

*"OpenArc is dedicated to Intel, RAGE should be independent from a specific
manufacturer."* Said when a survey of other serving stacks put forward an
Intel-only one. It is a rule about RAGE, and it sorts everything into two
layers:

- **THE FRONT, `rage.toml`, THE API AND THE RUN IDENTITY name no
  manufacturer.** A card is whatever name the file declares (`xpu` is this
  node's word, not the code's: the only fixed device name is `cpu`); a
  backend is a command line and a health path; the run identity is the
  image, the arguments, the weights and the driver AS READ. This is also
  Louise's own premise — a space never names a device, and peers run what
  they have (02 §2.4).
- **AN IMAGE IS A MANUFACTURER'S**, by necessity: it carries one compute
  runtime. `deploy/rage/Dockerfile` is the Intel one (llama.cpp's SYCL
  build, torch's XPU wheels, Level Zero). Another card is another
  Dockerfile and another `rage.toml`, never another front.

So a backend is judged by whether it runs on more than one manufacturer's
hardware: llama.cpp (SYCL, CUDA, ROCm, Vulkan, Metal), vLLM (CUDA, ROCm,
Intel XPU, CPU), TEI and SGLang qualify; OpenVINO Model Server, OpenArc and
Intel's llm-scaler build do not — they may serve a model ON an Intel node,
as OpenVINO serves MedCPT's cross-encoder here, and nothing in RAGE may
come to need them.

**Audited against the rule the same day. Where RAGE is tied to Intel:**

1. **FIXED 2026-09-20** (`[rage] pci.<card>`, `runs.card_driver`): the
   driver is read from the PCI device the card is declared to be; with no
   address and several GPU drivers loaded, NO run is stated and the reason
   says what to declare; with exactly one loaded the old reading stands,
   because there it cannot be wrong. As found: **THE DRIVER IN THE RUN
   IDENTITY WAS RIGHT BY LUCK OF ORDER — a defect, latent on this very
   node.** `runs.driver_string` names "the first GPU
   module loaded" from a fixed list, `("xe", "i915", "amdgpu", "nvidia")`.
   This node has TWO loaded: `xe` for the Arc (renderD128) and `amdgpu`
   for the Ryzen's own graphics (renderD129). RAGE states `xe` only
   because `xe` comes first in that tuple; an NVIDIA card beside the same
   AMD graphics would be stated as `amdgpu`. The driver is the one the
   DEVICE is bound to: declare the card's node in `rage.toml` and read
   `/sys/class/drm/<node>/device/driver`. (Built on the PCI address
   instead: render nodes are numbered by load order and can swap across
   boots; an address moves only with the hardware.) On this node the
   DRIVER STRING stays what it was. **The run ids do move, and I first said
   they would not**: the front lives in the image, a new image has a new
   digest, and the image is part of every run (09 §9.12) — a new RAGE
   build is a new run by definition, which is what makes it re-gated.
   **Rolled out the same day**: image `…d7686483…7aa4`, `pci` declared on
   the node, the driver string identical, seven new run ids
   ([`STATUS.md`](STATUS.md), 2026-09-20 midday). **And the warning in
   brackets came true on 2026-09-22**: a reboot made the Arc `card1`, the
   clock's bind still named `card0` — the processor's graphics, which has
   no `tile0` — and RAGE would not start at all. A number is not a name
   anywhere, not only in a run identity; the bind now names a colon-free
   symlink to the address, and a test refuses a card-numbered source
   (f1b9dfc, [`STATUS.md`](STATUS.md) 2026-09-22).
2. **The GPU clock is Intel's and says so nowhere.** `gpu_clock.Governor`
   reads and writes `rp0_freq`, `rpn_freq`, `min_freq`, the `xe`/`i915`
   sysfs names. It is optional (`RAGE_FREQ_DIR` unset = no clock), which
   is why it harms nothing elsewhere; a second manufacturer means a second
   clock driver behind the same three verbs (read the range, pin,
   release), not a change to the front.
3. **Observed card memory comes from DRM fdinfo**, a kernel interface that
   `xe`, `i915` and `amdgpu` implement and NVIDIA's own driver does not.
   There RAGE still places and admits — admission counts what is DECLARED
   — and only `observed` is missing.
4. **The launcher for the torch and OpenVINO servers hard-codes the word**
   `xpu` (`deploy/rage/servers/launch.py`: `choices=["xpu", "cpu"]`, and
   torch's `xpu`, OpenVINO's `GPU`). Image layer, so allowed — but the
   device WORD belongs to the image's table, not to the argument parser.

Worth knowing beside it: llama.cpp's Vulkan backend is the one engine that
runs unchanged on all three manufacturers, and on this card it is the
slower one (published B70 figures: SYCL about 2.2 times Vulkan's
generation speed) — independence of RAGE does not mean the slowest common
engine on each node.

**The models on the reference node, held to the rule** (operator's
question, 2026-09-20: which backend, tied to Intel or not, and would the
same model run elsewhere). DNABERT-2 is left out: the DNA collections are
frozen.

| model | serves | backend here | weights | tied to Intel? | on NVIDIA / AMD |
|---|---|---|---|---|---|
| `bge-m3` | wikipedia, documents and queries | llama.cpp (`llama-server`, SYCL build) | GGUF f16 | no | same file; llama.cpp's CUDA, ROCm or Vulkan build |
| `qwen3-embedding-4b` | gutenberg, documents and queries | llama.cpp | GGUF Q4_K_M | no | same |
| `medcpt-article`, `medcpt-query` | pubmed, documents / queries | llama.cpp | GGUF f16 | no | same |
| `bge-reranker` | reranker, gutenberg and wikipedia | llama.cpp (`--reranking`) | GGUF f16 | no | same |
| `chat` (Qwen3-8B) | answers | llama.cpp | GGUF | no | same |
| `madlad` (MADLAD-400 3B) | translation | PyTorch, our own server (`servers.launch madlad`) | safetensors bf16 | no — the server asks torch for a device; only the image's wheels are XPU | same weights, torch's CUDA or ROCm wheels, the device word `cuda` (item 4) |
| `medcpt-rerank` | reranker, pubmed | OpenVINO, our own server (`servers.launch medcpt`) | OpenVINO IR | **YES — the one model that is** | not as it stands: the IR runs on Intel hardware and on any CPU. The model itself is an ordinary BERT cross-encoder, and its original weights run under PyTorch anywhere; that is a second server entry, not a change to RAGE |

What the same file does NOT promise on another card is the same VECTOR:
another build is another engine digest and another driver, hence another
run, and whether its vectors belong to the space is what the calibration
gate decides (02 §2.4 — by measurement, never by the device's name). A
reranker defines no space, so the one Intel-tied model is also the one
whose replacement costs nothing downstream.

## 19. The card's real memory, a stranger on it, and Louise told (2026-09-20)

**What happened.** A bake-off container, outside RAGE, took 21.4 GiB of the
32 GiB card and 22.8 GiB more of GPU buffers in host RAM. RAGE's admission
counted only what its own models declare, so it saw nothing. `xe` moved
every resident model into host RAM without an error to anyone (chat: 476
KiB on the card, 7,403 MiB off it); the host ran out of memory and the
kernel killed three of RAGE's children ([`STATUS.md`](STATUS.md),
2026-09-20 afternoon).

**The operator's decision**, the same day: read the card's real free
memory before a load — "yes, definitely"; show what is RAGE's and what is
not, in different colours; when a process outside RAGE takes the card,
"wisely unload the unused models to avoid an oom crash of the other
model"; and "if Louise needs a model that RAGE cannot fit in memory I want
a notification to Louise". This amends §7 in two places, named here:
*observed figures never decide* and *never an eviction*.

**Admission.** Our own models are still counted as DECLARED, for §7's
reasons. What a stranger holds is counted as OBSERVED — nobody declared
it: `foreign = the driver's count of what is used − what everything in
this container's PID namespace is seen to hold` (DRM fdinfo; a child still
loading included, so a load in progress is never a stranger AND its own
declaration). The card's budget becomes `declared + foreign + need +
reserve <= capacity`. The reading can only REFUSE a load the ledger would
have admitted. A card that cannot be read — no reader for its driver, no
capability — gives `foreign = 0`, the ledger decides alone as before, and
`GET /louise/gpu` says why. The HOST's budget stays declared: the host's
real free memory excludes the ZFS ARC, which gives way under pressure, so
reading it would refuse every preload on a TrueNAS box that has been up a
day.

**Pressure** (`placement.relieve`, every 3 s: one ioctl and a dozen small
files). A card is SQUEEZED when its real free memory is under the declared
reserve and a stranger holds at least 256 MiB of it. Only IDLE models are
given up — no request in flight (the front counts them), none for
`idle_s` — and `unload` re-checks under the model's lock, so a model is
never taken from under an answer:
1. a model PUSHED OFF THE CARD (most of its GPU buffers in host RAM; read
   from the children's own fdinfo, no capability needed): it holds real
   host RAM and answers slowly;
2. while squeezed: the idle models that TOGETHER cover the deficit with
   the LEAST given up (`least_given_up`: every subset, a node holds a
   dozen), among equals the fewer, then the longer unused.
A model in use is never touched. A preloaded model given up comes back by
itself once the card has been quiet for `restore_s` and REALLY has room
for it, one per look; any other on its next request, through admission.
`[rage] pressure = { unload = true, idle_s = 60, restore_s = 120 }`.

**Measured live on the node** with a 7 GiB stranger of my own, twice. The
squeeze was seen within 3 s (613 MiB free under 2,048 reserved: 1,435
short); no model was pushed to host RAM; the models came back 120–140 s
after the stranger left. The FIRST rule was least-recently-used-first,
and it gave up 9,937 MiB for those 1,435 — two small models and then the
8.6 GiB Qwen3 that happened to be next. The second run, with
`least_given_up`: bge-m3 + medcpt-query + medcpt-rerank, 1,596 MiB. Tests
had passed both times; only the node showed the difference.

**The notification.** A refusal's body carries `admission.foreign_mib`
and its reason says how much processes outside RAGE hold. RAGE keeps its
refusals, unloads and restores — repeated asking is ONE event with a
count — and serves them at `GET /louise/gpu` with each card's real memory
and its own container name. Louise shows a strip on top of the Library
and the System page when RAGE could not load a model, gave models up or
has one pushed off the card, and nothing otherwise; the GPU panel's bar is
segmented RAGE / other processes / held by no process shown / free, with a
"whose" column; and the worker's deferral line carries RAGE's own words.
It stays a deferral, never an error on a document.

**No manufacturer in the front (§18).** The card is read through
`app/gpu_memory.py`: one verb, one reader per kernel driver (`xe`'s ioctl,
which wants CAP_PERFMON — `deploy/truenas-rage.yaml` adds it; `amdgpu`'s
sysfs), the ONLY place a driver is named, shared with the GPU probe so the
ioctl's layout exists once. A driver with no reader reads nothing and
changes nothing. A test greps the front's code for vendor names.

**Corrected here:** "a model pushed off the card does not move back by
itself" (first written 2026-09-18). It does, WHEN IT IS USED and the card
has room: chat went from 476 KiB to 7,170 MiB on the card at its next
request. An idle one stays where it was pushed, which is why RAGE unloads
it.

**What this does not do.** It cannot stop a stranger from overcommitting
the card by itself (the first bake-off wanted 44 GiB of a 32 GiB card), and
it never gives up a model in use. Anything that needs the card beside
RAGE should still go through RAGE, or run with RAGE's models unloaded.

## 20. LocalAI's rerankers backend and MedCPT's segment ids, checked (2026-09-27)

§3 left LocalAI's fit for `medcpt-rerank` an open question — the model is on
OpenVINO today (deploy/medcpt-rerank) specifically because "llama.cpp and
OVMS both zero" the segment ids MedCPT-Cross-Encoder's pairs need (§2).
Checked whether LocalAI's `rerankers` backend, which wraps
[AnswerDotAI/rerankers](https://github.com/AnswerDotAI/rerankers), does the
same.

**Method.** huggingface.co is blocked by this environment's egress policy,
so the real `ncbi/MedCPT-Cross-Encoder` weights and tokenizer could not be
pulled. Read the real source instead — `rerankers==0.10.0` from PyPI, and
LocalAI's `backend/python/rerankers/backend.py` from its repo — and ran the
real `rerankers.models.transformer_ranker.TransformerRanker` code path
against a synthetic model: a real `BertForSequenceClassification`
(`type_vocab_size=2`, same family PubMedBERT/MedCPT-Cross-Encoder is built
on) with random weights and a small local vocab, no download needed. Random
weights don't matter for the question asked — whether the wiring passes
`token_type_ids` through and whether a BERT embedding layer consumes them —
only real pretrained weights would matter for score *quality*, not for this.

**Result: it passes them correctly, through the path LocalAI actually
uses.**
- `backend/python/rerankers/backend.py` calls `self.model.rank(query=...,
  docs=..., doc_ids=...)` — never the library's other method, `.score()`.
- `TransformerRanker.rank()` builds `[(query, doc.text) for doc in docs]` —
  proper `(text, text_pair)` tuples — and tokenizes the batch. Given that
  shape, the tokenizer emits one joint sequence per pair with
  `token_type_ids` correctly split (`0…0 1…1`, query then doc), and
  `model(**tokenized_inputs)` forwards them straight in. Zeroing
  `token_type_ids` on the same encoding measurably changed the logit, so the
  embedding layer does consume them — this is not a no-op field.
- So on LocalAI, unlike llama.cpp and OVMS, `medcpt-rerank` would not need
  the segment-id workaround that put it on our own OpenVINO server in the
  first place. Still unverified: score *quality* against the real weights,
  and everything else in §3's LocalAI row (Intel image provenance, D5's
  offline-fetch requirement, one gRPC backend per exotic model for MADLAD
  and DNABERT-2) — none of that is settled by this.

**A bug found on the way, irrelevant to LocalAI's path but worth knowing if
anyone reaches for the library by hand.** `TransformerRanker.score(query,
doc)` calls `self.tokenizer((query, doc))` — a bare tuple, not
`[(query, doc)]`. HF's tokenizer reads a bare tuple of two strings as a
*batch of two unrelated single-sentence texts*, not one text pair: shape
`[2, N]` instead of `[1, N]`, `token_type_ids` all-zero in both rows, no
`[SEP]` joining them, no cross-attention between query and doc at all — not
"segment ids zeroed" but no joint encoding whatsoever. LocalAI never calls
`.score()` for `/rerank` (confirmed above), so this doesn't affect it; it
would silently corrupt scores for anyone scripting against the `rerankers`
library's `.score()` method directly.

## 21. LocalAI's backend binaries, spawned by RAGE directly (2026-09-27)

Not "RAGE in front of LocalAI's own core" (ruled out above: LocalAI's
`spawnGRPCModel` sends the weights path over a `LoadModel` gRPC call
*after* the process starts, never on argv, so RAGE's `/proc/<pid>/cmdline`
match — the whole mechanism 09 §9.12 uses to turn "what was declared" into
"what was observed" — has nothing to match against). A narrower question:
can RAGE use the same backend *binaries* LocalAI ships, spawned and owned
by RAGE itself, with LocalAI's own Go core out of the picture entirely.

**Checked against the source, not inferred.** `backend/python/rerankers/backend.py`
(every LocalAI Python backend follows the same `backend/python/common/libbackend.sh`
pattern) starts as:

```
venv/bin/python backend.py --addr 127.0.0.1:${PORT}
```

— one flag, no LocalAI core required, a plain standalone gRPC server. It
implements `backend.proto`'s `LoadModel`/`Predict`/`Embedding`/`Rerank`/`Health`.
`LoadModel` takes `{Model: "<name or path>"}` over the wire, not argv — so
if RAGE spawns it directly and makes that call itself, synchronously, as
the launch step, RAGE is the one that hashed the weights file at that path
and bound it to the pid, not a report from the child. That is what 09
§9.12's `weights` leg asks for — hashed at every launch, from the bytes on
disk — and it is RAGE doing the hashing and the call, not trusting either.

**A second benefit, not asked for but true.** The `/proc` argv-match exists
because llama-swap sits between RAGE and the child and can restart it
without RAGE commanding it (rage.md §8: "llama-swap restarts a child that
died on its next request, and that is another launch — hashed and matched
again"). A RAGE-spawned, RAGE-parented child cannot be swapped out from
under it silently: RAGE gets `SIGCHLD`/`wait()` on every exit of a process
it directly forked, the same as it already does for the three custom
Python servers. The run identity's weights leg becomes "the file RAGE
hashed and bound with the `LoadModel` call it made to the pid it is still
parenting" — first-hand, not parsed out of a cmdline string.

**Cost.** RAGE needs a small `backend.proto` gRPC client — `LoadModel`,
`Predict`, `Embedding`, `Rerank`, `Health` — replacing the plain HTTP proxy
it speaks to llama-swap and the three FastAPI servers today. Real, not
free, but a stable documented protocol, not a screen-scrape.

**A version bump's cost, checked against existing collections.** Nothing
at the space level: `space_id` (02 §2.4, amendment `0d85f7f`) hashes the
weights digest, the chunker layers, `cleaning_version` and the space
block — "never the engine" — so a vendored backend's version has no path
to touch stored vectors, and invariant 1's dim/space refuse-to-start check
has nothing to trip. But it is NOT free at the run level, and the cost
lands somewhere non-obvious: every vendored backend shares one
`deploy/rage/Dockerfile`, one image, one digest, and the run identity's
`image` leg is that whole container's digest (09 §9.12). Bumping
`medcpt-rerank`'s vendored `rerankers` version mints a new RAGE image,
which mints a new `run_id` for EVERY engine RAGE launches — `bge-m3`,
`qwen3-embedding-4b`, `medcpt-article`/`medcpt-query` included — not only
the one whose backend actually changed. §9 already names this as accepted,
general behaviour: "A rebuild is a revalidation... `image` is in the run,
so a new image re-gates every space on the node." So a one-line dependency
bump for a reranker forces wikipedia's and gutenberg's encoders to
re-pass calibration on next restart too, purely from image co-residence.
It stays a deferral, never silent corruption — 03 §3.3: an untested run
answers no query while its space is in quarantine, and a peer that fails
may still search with what it holds — but it is a real, node-wide cost of
one small change, and worth knowing before treating a backend bump as
routine. (§18 already notes "a reranker defines no space": `medcpt-rerank`
itself never needs to pass a gate; the cost above is entirely secondhand,
from sharing an image digest with collections that do.)

**Where it lives.** `deploy/rage/Dockerfile`, one venv per backend, exactly
the shape §9/D4 already committed to for madlad, dnabert2 and medcpt's own
servers — this is three (or more) additional venvs of the same kind, not a
new pattern.

**Scope: not llama.cpp.** LocalAI's `llama-cpp` backend is its own
gRPC-wrapped build (`backend-assets/grpc/llama-cpp`), a different artifact
from the `ghcr.io/ggml-org/llama.cpp:server-intel-bXXXXX` image §16 pins
today. Adopting it means re-earning its Intel/SYCL provenance from
scratch — the same "any runtime that is not one of these starts that
search again" cost §2 already paid once — for models (`bge-m3`,
`qwen3-embedding-4b`, `medcpt-article`/`medcpt-query`, `bge-reranker`) that
already have a working engine. The candidates worth the integration cost
are the ones that don't: `medcpt-rerank` (§20 — already checked, correct
`token_type_ids` handling through `.rank()`), and possibly MADLAD and
DNABERT-2 via the `transformers` backend, following the same standalone
launch pattern — seq2seq/translation support there is still unverified
(rage.md §3 raised it, unresolved).

**Licensing, checked 2026-09-27.** `mudler/LocalAI`'s `LICENSE` is MIT
(copyright Ettore Di Giacinto), no restriction on vendoring `backend.py`
and its venv into `deploy/rage/Dockerfile` beyond keeping the copyright
notice and licence text alongside the vendored source — MIT's one real
condition. Not checked: each backend's own runtime dependencies'
licences individually before actually vendoring one — `rerankers` and
`transformers` (both pulled for §20's test) are themselves Apache-2.0,
nothing found here suggests a blocker, but a licence is a property of
each dependency, not of LocalAI, and should be confirmed per backend
adopted, not assumed from these two.

**Not built.** Design note only; nothing on the node runs this. Follow it
if `medcpt-rerank`, MADLAD or DNABERT-2 ever move off their own servers.
