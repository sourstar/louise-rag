# RAGE — design review before implementation

*2026-09-19. A review of [`rage.md`](rage.md) at b0f5dad against 02 §2.4,
03 §3.3, 07 §7.8 and 09 §9.3/§9.12 on `main` (27a1332), the code on
`louise`, and llama-swap's current documentation (README and
`docs/config.example.yaml`, read the same day). Nothing here is built.
§2 must be settled before code; §3 are decisions the note leaves open;
§4 are corrections to its text. Part II proposes a solution for each.*

*Outcome, the same day: the operator accepted every recommendation.
[`rage.md`](rage.md) was rewritten to the accepted design and wins where
the two differ; the amendments are on `main` (8bbc66a for RAGE, 98ee4dc
for quarantine); the work is [`migration-plan.md`](migration-plan.md)
steps 13 and 14. This note stays as the record of why. Two things in it
were wrong when first written and are corrected in place, marked: the
name of the third state (§18), and what a burned key is called (§18).*

## 1. What checks out, and what does not, against llama-swap

Confirmed: `/upstream/:id` proxies any path and loads on first use;
`GET /running`; `POST /api/models/unload` and `/api/models/unload/:id`;
`hooks.on_startup.preload`; the config file is watched (`-watch-config`,
on by default in Docker); groups carry `swap`, `exclusive`, `persistent`,
`members`; per-model `env`, `ttl`, `concurrencyLimit`, `macros`,
`cmdStop`; a `matrix` routing engine exists (a DSL with `sets` and
`evict_costs`). It adds no response header and exposes no argv or pid,
so §8's front is needed — confirmed, not "to verify".

Wrong or missing in the note:
- **There is no load endpoint.** The README lists unload only. A model
  loads on its first request or from the startup preload. §4's table and
  §6's "admission belongs to Louise" contradict each other (B3 below).
- **`exclusive` defaults to `true`**: "running a member unloads every
  other group". §4's yaml, as written, unloads dnabert2 when bge-m3
  loads. Every group needs `exclusive: false`. Groups also live under
  `routing.router.settings.groups` in the current version, not at the
  top level: pin llama-swap's version and write the config against it.
- **`${PORT}` does not expand inside `env`**, and two of our three
  servers (`madlad-translate`, `medcpt-rerank`) take their port and
  device from the environment, not from argv. §4's
  `python -m servers.dnabert2 --device xpu --port ${PORT}` names flags
  that do not exist. Give the three servers `--port`, `--device` and
  `--weights` argv: 09 §9.3 wants the port and the weights as `${PORT}`
  and `${WEIGHTS}` placeholders in `args` and nothing host-specific in
  `env`, so this is what the Codex needs anyway.

## 2. Blocking before any code

**B1. The RAGE image must be obtainable, or every Codex created under it
is unverifiable by construction.** 02 §2.4: "an engine whose image is
named by digest and obtainable the same way"; engine images get a DHT
key like weights (0d85f7f). Today the frozen engine is
`ghcr.io/ggml-org/llama.cpp:server-intel-b10630`, public. Under RAGE
the reference vectors of every new collection come from RAGE, so
`space.encoder.engine.image` becomes RAGE's image — a locally built,
`pull_policy: never`, multi-gigabyte image nobody else can pull. That
is the artefact CLAUDE.md warns of: internally consistent, honestly
computed, permanently unreproducible. The note must fix where RAGE
images are published (a public registry by digest at every build, or an
OCI tarball shared over 04 §4.3's path) and its size budget, before a
single Codex is written against it.

**B2. The run identity cannot be computed the way §8 says.**
- *`image`.* A process cannot observe its own image digest; only the
  container runtime knows it. The honest rule: the deploy pins
  `image: rage@sha256:…` and passes the same digest in an env variable;
  RAGE refuses to start when it is absent or a tag. A digest-pinned
  compose reference IS the observation — the runtime runs nothing else.
  As observed evidence beside it, record the digest of each child's
  `/proc/<pid>/exe`.
- *`args` and `env`.* "The exact argv and env" of the process holds the
  concrete port (llama-swap assigns `${PORT}` incrementally and a config
  reload can shift it), the concrete weights path, and `HOSTNAME` — the
  container id, new on every restart. Hashed raw, every restart is a new
  run and re-gates every space. 09 §9.3 already fixes the shape: hash
  `args` with `${WEIGHTS}` and `${PORT}` as placeholders and `env` as the
  entry's declared variables only. RAGE reads `/proc/<pid>/cmdline` to
  check the child matches its template, and hashes the template.
- *`weights` of a directory model.* 09 §9.3 says "the digest of the
  weights file", singular. DNABERT-2 is an HF revision with remote code,
  MADLAD a directory of safetensors plus tokenizer files, the MedCPT IR
  an `.xml` + `.bin` + `tokenizer.json`. §7's first proof includes
  DNABERT-2, so the rule is needed first: propose `H` of a canonical
  manifest `{relative path: sha256}` over the model directory, remote
  code included, and amend §9.3. Every model directory is materialised
  on disk and launched offline (`HF_HUB_OFFLINE=1`) — see D5.
- *`driver`.* Readable from inside: kernel release, the `xe` module
  version, the compute-runtime package versions in the image, the GuC
  firmware version where sysfs exposes it. Fine as written.

**B3. Who admits a load.** With `/upstream/<id>` loading on first use, a
query for a dormant collection loads an 8 GB model with no admission
check, and a `swap: true` group evicts its sibling to do it. Either
RAGE's front admits (it can read the `xe` device query and its own
children's DRM fdinfo — they are its children, no host PID namespace
needed), or a request for an unloaded model gets 503 until an explicit
load. Recommendation: the front owns admission and a load API of its
own (`POST /louise/models/<id>/load`, device as a parameter); this is
also where `gpu_probe`'s memory figures stop being Louise's problem.

**B4. The order of work, and a step in the migration plan.** Codex v3 is
not on `louise`: `app/hold/codex.py` still reads `space.encoder` as
`{image, weights, label}`, and `hold/embedder.py` writes a version-1
statement with no run. RAGE's run identity embeds 09 §9.3's engine
object; `embedder check` must accept `Louise-Run`; `vectors build` must
check it per answer before the write. `migration-plan.md` names no step
for any of this, and on `louise` the plan is binding. Proposed order:
(1) Codex v3 — engine object, `engine_id`, `space_id` without the image,
version bumps; (2) the RAGE front with the run identity, proved by §7;
(3) the client — read the header, check it before every write, run the
gate automatically on an unknown run, statement version 2; (4) retire
`app/verify/backend_gate.py`. Each with its tests.

## 3. Decisions the note leaves open

**D1. The device in the model id.** `bge-m3@xpu` in a collection's
`[encoder] url` puts the device in Louise's desired state, while §2 says
the device is chosen at load. Load `@cpu` by hand and the next embedding
request to `…@xpu` reloads it and evicts `@cpu`: a flip-flop, each flip a
launch (MADLAD's is 300 s). Recommendation: the URL names the model
(`/upstream/bge-m3`), the device is RAGE's placement — desired state in
RAGE's config, a runtime override through the load API — and the front
routes to whichever variant is up. The run identity carries the device,
so a device change is a new run and the gate re-validates it by itself:
that is exactly what dd9bef4 designed. Also check with the probe that a
`-ngl 0` SYCL build holds no DRM fd; if it does, "cpu" is not cpu.

**D2. RAGE's own config, generating llama-swap's.** A `rage.toml` per
model — weights path, engine `{name, build, variant}`, the cmd template
with `${WEIGHTS}` and `${PORT}`, declared env, device, group, memory
need — from which the llama-swap yaml is written, solves B2's
normalisation, gives the System-page form a file to write (tier 2,
invariant 10), and gives B3's admission its numbers. llama-swap's yaml
becomes a derived artefact, never edited by hand.

**D3. One container is one failure and restart domain.** Today a torch
bump restarts MADLAD alone. Under one image it restarts the chat model
and every encoder, and — because `image` is in the run — re-gates every
space on the node: 512 texts per encoder, minutes for Qwen3-4B. That is
the price of the operator's ask and acceptable, but §7 must measure a
full gate per encoder so the price is known. Worse: the OOM killer
inside one cgroup takes the largest process, whichever one grew. A CPU
variant that outgrows RAM (the TEI OOMs of 2026-09-15) can take
llama-swap or the front with it, and every model. §6 says "CPU variants
need a memory bound" and offers no mechanism; `RLIMIT_AS` breaks mmap'd
GGUFs and torch's reservation, and cgroup delegation inside a container
is normally read-only. What works: `oom_score_adj` — the front and
llama-swap at −1000, children positive by size — so a kill takes the
model that grew and nothing else, and admission (B3) as the real bound.

**D4. One venv or three.** madlad pins `transformers==4.46.3` (5.x
translates to `ě ě ě`), dnabert2 `transformers<4.50`, medcpt
`openvino==2026.3.1` + `tokenizers==0.22.2` on the llama.cpp base. One
venv couples every future bump; a venv per server keeps today's proven
pins byte for byte at the cost of disk. Recommend three. And §7
overstates the clash risk: each child is its own process with its own
`LD_LIBRARY_PATH`; what the runtimes actually share is the system Level
Zero loader and driver (`libze1`, `libze-intel-gpu1`,
`intel-opencl-icd`). The proof is "one compute-runtime version that
satisfies llama.cpp b10630, torch 2.13+xpu and OpenVINO 2026.3.1 at
once", and it should include the MedCPT cross-encoder and MADLAD, not
only bge-m3 and DNABERT-2.

**D5. RAGE fetches nothing (invariant 5).** LocalAI's gallery is listed
as an attraction, and two of today's servers download into `HF_HOME` on
first start. Under RAGE no child may move a byte: weights are placed by
Louise's transfer manager or by hand, RAGE launches offline and refuses a
model whose weights are absent. This also makes B2's weights digest
well-defined at launch.

**D6. What RAGE inherits, unmentioned.** The GPU governor moves into
RAGE (`deploy/truenas-gpu-governor.yaml`, scheduling.md §12): RAGE sees
every interactive request itself, so the claim can be local to it; the
sysfs frequency bind and `gpu_samples` follow. The fan workaround —
restarting an engine — was assigned to RAGE and to RAGE only (operator,
2026-09-20). `gpu_probe` attributes memory per container by cgroup;
under RAGE every model falls in one container, so the panel's per-app
rows need the probe to label by model (it already reads `-m` and the
server directory from cmdlines, so per-process rows survive). One
section in the note, before the panel silently loses its rows.

**D7. The admin surface.** Load, unload, add and remove are operator
actions on a LAN-facing port. llama-swap has `apiKeys`; the front should
take the admin token by file path (invariant 8) as `hold` does, and
inference endpoints stay open as today.

## 4. Corrections to the text

- **"Louise's client does not change" is false twice.** `app/llm.py`
  speaks Ollama's `/api/chat` with `think`, `keep_alive` and
  `options.num_ctx`; llama-server serves `/v1/chat/completions`, has no
  per-request context size (`-c 16384` fixed at launch) and turns
  thinking off through `chat_template_kwargs`. "Ollama goes" is a
  rewrite of `llm.py`. And every writer reads `Louise-Run` and checks it
  before the write (03 §3.3). Small, but budgeted, not "one line".
- **Ollama's qwen3 blob** may carry its chat template in a separate
  blob, not in the GGUF's `tokenizer.chat_template`; verify, else
  `--chat-template-file`.
- **The weights cache "by path, size and mtime"** is premature: hashing
  5.5 GB costs seconds and a launch reads the file anyway. Hash at every
  launch; if a cache is ever needed, key it on inode too (a path is a
  name, not an identity).
- **The front never batches or reorders requests.** The gate measures
  batch invariance at the peer's own batching (03 §3.3); a front that
  coalesced would measure a path production does not take.
- **A cold load through `/upstream`** is a deferral for corpus work and
  a slow answer for a query. Preload the query encoders
  (embedder-pool-spec §3 already says idle is no reason to unload).
- **§5's "Code to follow"** should point at B4's order and at the
  migration-plan step, with tests named per step.

## 5. §7, restated as a done-when list

- One image builds with the compute runtime, llama.cpp b10630's binary
  and libraries, three venvs and llama-swap pinned by version; the image
  is published by digest where a stranger can pull it (B1).
- From that image: bge-m3 and MedCPT-Article on llama.cpp, DNABERT-2 and
  MADLAD on torch, the MedCPT cross-encoder on OpenVINO, each on the Arc
  and each on the CPU, and `@cpu` holds no DRM fd. DNABERT-2 is a
  runtime check only, ungated (§17.1).
- The front states `Louise-Run` on every answer, streamed ones
  included, and `GET /louise/runs/<id>` returns 09 §9.12's object; two
  restarts of the same entry give the same run id; a changed flag, a
  changed weights file and a changed image each give a different one.
- Load, unload and device change timed per model; a full calibration
  gate timed per encoder (D3).
- Vectors from every `@xpu` entry match today's endpoints within each
  space's tolerance; wikipedia's gate passes through `/upstream/…`.
- The OOM test: a CPU child driven past RAM dies alone; the front and
  llama-swap survive and the other models keep answering.

---

# Part II — proposed solutions (2026-09-19)

*One proposal per item of Part I, decision-ready. The recommended option
comes first; alternatives are named only where the operator might prefer
them. §14 lists what only the operator can decide; §15 names the spec
amendments, for `main`.*

## 6. The shape: one policy, in the front (B3, §1)

*Corrected while building (2026-09-19):* "no groups" below is wrong. A
model in no group falls into llama-swap's default — one model at a time
across the instance, as its own example config says — so the policy-free
configuration is ONE group holding every entry, `swap: false`,
`exclusive: false` (`app/rage/swapyaml.py`). The rest of the paragraph
stands.

llama-swap stays, as the process runner it is good at — start, health,
stop, per-model proxy, log buffer, UI — and holds **no policy**: every
model is its own entry, `swap: false`, `exclusive: false`, `ttl: 0`, no
groups, no `matrix`, no preload hook. It listens on `127.0.0.1` inside
the container; nothing reaches it but the front. So it never loads,
evicts or swaps anything by itself; the front is the only scheduler.

The **front** (`app/rage/`, FastAPI + httpx, both already dependencies;
run as `python -m app.rage` from the RAGE image) owns:
- the published port, and `/upstream/<id>/…` proxied to llama-swap's
  `/upstream/<id>@<device>/…` with `Louise-Run` added, streamed answers
  included;
- admission (§8), load and unload, the run identity (§7), the clock
  (§12), the admin surface (§13).

Exit criterion for llama-swap: if finding the child's pid (§7) or the
double hop costs more than a week of the first proof, the front
supervises directly with `subprocess.Popen` — about two hundred lines —
and neither the API nor `rage.toml` changes. Nothing else in this note
depends on the choice.

## 7. The run identity, computable (B2)

**`image`.** The deploy pins `image: <registry>/rage@sha256:…` and sets
`RAGE_IMAGE` to the same digest. The front refuses to start unless
`RAGE_IMAGE` matches `^sha256:[0-9a-f]{64}$`; a tag, or nothing, is a
boot error naming the variable. A digest-pinned reference is the
observation: the runtime runs no other image. As evidence, the run's
description logs the digest of each child's `/proc/<pid>/exe`.

**`args` and `env`.** The engine's `args` is the entry's `cmd` template
for the device used, `${WEIGHTS}` and `${PORT}` unsubstituted (09 §9.3);
`env` is the entry's declared variables, nothing from the process. What
the front verifies, once the child is ready: it scans `/proc/*/cmdline`
for exactly one process whose argv equals the template with `${WEIGHTS}`
→ the weights path and `${PORT}` → an integer, and whose
`/proc/<pid>/environ` carries every declared variable at its declared
value. No match, or two: the model is **unstated** — its answers carry
no `Louise-Run`, which 03 §3.3 already defines as "search only, nothing
written" — and the front logs why. Same weights path in two variants
(`@xpu`, `@cpu`) is fine: the templates differ in `-ngl` or `--device`.

**`weights`.** Hashed at every launch, streamed, concurrently with the
child's own load (MADLAD's 5.5 GB is seconds against a 300 s start); no
cache. A directory model hashes as §15.1 amends: `H` of the canonical
JSON `{"<relative path>": "sha256:…", …}` over every regular file,
sorted, symlinks refused. DNABERT-2's HF snapshot — weights, config,
tokenizer and the remote-code `.py` files — is materialised under
`/models/` once; the code is in the digest because it is part of the
function.

**`device` and `driver`.** `device` as the driver names the card (the
probe's pci.ids lookup, reused), `"cpu"` for a CPU entry. `driver` =
`kernel <release>; xe <module version>; libze-intel-gpu1 <version>;
intel-opencl-icd <version>; guc <firmware>` where readable, `"cpu"` on
a CPU run. Runtime package versions are inside the image already;
naming them again costs nothing and reads well in a statement.

**`run_id`** = 09 §9.12's `H({driver, engine, weights})`, computed once
per launch, kept in memory with the pid; `GET /louise/runs/<id>` returns
the object for every run the front has stated since it started, and
404 for others. A child that dies and is relaunched is a new launch: the
same id if nothing changed — the test in §5 — and a different one if
the weights file or a flag did.

## 8. Admission, and the load and unload API (B3)

Each entry declares `memory = {xpu = <MiB>, cpu = <MiB>}`: the resident
need, measured. After every load the front records the observed peak
(its own children's DRM fdinfo for the card, `/proc/<pid>/status` RSS
for the CPU) and shows both, so a declaration that drifts from the
measurement is visible on the System page.

**Fits** = declared needs of the models loaded on that device, plus this
one, plus the configured reserve, ≤ the device's capacity. Capacity of
the card from the `xe` device query (the probe's ioctl, reused; RAGE has
`/dev/dri`); of the CPU from the container's `memory.max` and
`memory.current` in `/sys/fs/cgroup`. **Does not fit** → `503` with
`Retry-After` and a JSON body naming what is loaded; never an eviction.
Eviction is Louise's decision (embedder-pool-spec §3–4): the front only
gives the facts and the lever.

**Autoload stays, through admission.** A request to `/upstream/<id>/…`
for an unloaded model triggers admission on the entry's default device;
fits → load, wait for ready, serve; does not fit → `503`. Single-flight
per model id, so ten concurrent first requests admit once. For Louise
nothing changes at the URL: a `503` is what an unreachable endpoint
already is — a deferral. For a person, a dormant collection's first
query waits one load.

```
GET    /louise/models                 → [{id, state: absent|loading|ready|unstated|failed,
                                          device, run, memory: {declared, observed},
                                          idle_s, since}]
POST   /louise/models/<id>/load       {device?}  → 202 {run} · 409 loaded elsewhere · 503 {reason, loaded}
POST   /louise/models/<id>/unload     → 200 · 404
POST   /louise/models/<id>/restart    → 202 (the fan workaround, §12; operator only)
GET    /louise/runs/<run_id>          → 09 §9.12's object · 404
PUT    /louise/models/<id>            → write the entry to rage.toml (§9); 409 while loaded
DELETE /louise/models/<id>            → remove it; 409 while loaded
/upstream/<id>/…                      → the child, with Louise-Run
```

`preload = [...]` in `rage.toml` is desired state: applied through
admission at start and whenever the file changes. A runtime load or
unload lasts until the next apply — invariant 10, as the note says.

## 9. `rage.toml`, generating llama-swap's yaml (D1, D2)

The one file the operator and the System page edit. llama-swap's yaml is
written from it at start and on every change, never by hand.

```toml
[rage]
reserve_mib = { xpu = 2048, cpu = 4096 }
preload     = ["bge-m3", "qwen3-8b", "medcpt-article", "medcpt-query"]

[models.bge-m3]
engine  = { name = "llama.cpp", build = "b10630", variant = "sycl" }
weights = "/models/bge-m3-FP16.gguf"
device  = "xpu"                                   # default placement
memory  = { xpu = 1400, cpu = 2600 }              # MiB, measured
env     = { LD_LIBRARY_PATH = "/opt/llama/lib" }
health  = "/health"
cmd.xpu = ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
           "-c", "8192", "-ub", "8192", "-b", "8192", "-np", "1", "-ngl", "99",
           "--host", "127.0.0.1", "--port", "${PORT}"]
cmd.cpu = ["/opt/llama/llama-server", "-m", "${WEIGHTS}", "--embedding", "--pooling", "cls",
           "-c", "8192", "-ub", "8192", "-b", "8192", "-np", "1", "-ngl", "0",
           "--host", "127.0.0.1", "--port", "${PORT}"]

[models.dnabert2]
engine  = { name = "louise-dnabert2-server", build = "3", variant = null }
weights = "/models/dnabert2-117m@a1b2c3d"         # a directory: §15.1's digest
device  = "xpu"
memory  = { xpu = 1200, cpu = 1800 }
env     = { HF_HUB_OFFLINE = "1" }
cmd.xpu = ["/opt/venv-dnabert2/bin/python", "-m", "servers.dnabert2", "--weights", "${WEIGHTS}",
           "--device", "xpu", "--host", "127.0.0.1", "--port", "${PORT}"]
cmd.cpu = ["/opt/venv-dnabert2/bin/python", "-m", "servers.dnabert2", "--weights", "${WEIGHTS}",
           "--device", "cpu",  "--host", "127.0.0.1", "--port", "${PORT}"]
```

- One `cmd` per device, each a complete template: the engine's `args`
  is the template of the device that ran, so a device change is a new
  run with nothing to infer.
- Generated llama-swap entries `<id>@xpu` and `<id>@cpu`, internal. A
  collection's `[encoder] url` names the model only:
  `http://nas:30450/upstream/bge-m3`. The front routes to the loaded
  variant; the device is placement, chosen by `device` or by the load
  API, and the gate re-validates a device change on its own (dd9bef4).
- The three Python servers gain `--weights`, `--device`, `--host`,
  `--port` argv and a `build` string, and bind `127.0.0.1`. Small
  changes, and what 09 §9.3's `args` needs regardless of RAGE.
- `name`, `build`, `variant` are readable and declared, as the spec
  says; `image`, `args`, `env` and `weights` are what binds.
- The System page's form calls `PUT /louise/models/<id>`; the front
  writes the file. No desired state ever enters a database.

## 10. One domain, bounded (D3, D4)

- **Container limit.** `mem_limit` on the RAGE app, so an out-of-memory
  is the container's cgroup and never the host's: the kill stays inside
  RAGE.
- **Who dies.** Docker's default capabilities exclude
  `CAP_SYS_RESOURCE`, so nothing can go below 0 in `oom_score_adj`;
  raising is free. After finding a child's pid (§7) the front writes
  `+800` to its `/proc/<pid>/oom_score_adj`, the largest models highest.
  The front and llama-swap stay at 0. The killer then takes the child
  that grew and nothing else; the others keep answering; the front marks
  it `failed` and reports it. (`cap_add: SYS_RESOURCE` and `-1000` on
  the front is available if the operator wants belt and braces.)
- **One torch, three pin sets.** torch XPU installed once for the
  image's compute runtime; each Python server in its own venv created
  with `--system-site-packages`, carrying its own `transformers`,
  `tokenizers` or `openvino` pins. MedCPT's venv needs no torch at all.
  Today's proven pins survive byte for byte and the image carries one
  torch, not three.
- **Rebuild is revalidation.** Accepted, and measured in §5: one full
  gate per encoder. Model restarts are per child and cost nothing to
  the others; image rebuilds are releases and rare.

## 11. Weights are placed, never fetched (D5)

`/models` is bind-mounted read-only. Every child runs with
`HF_HUB_OFFLINE=1` and its weights by absolute path. An entry whose
weights path does not exist is `absent` and refused at load, with the
path in the reason. Placing a file is Louise's transfer manager's job or
the operator's, as today — a `louise model fetch` that draws from the
token bucket is a later step and not this one.

## 12. What RAGE inherits (D6)

- **The clock.** Every Louise model request carries a `Louise-Level`
  request header — `dispatch` already knows the level of every call
  (`corpus | translate | chat | rerank | query`), so this is one line in
  each of the four clients. The front holds the card's `min_freq` at
  `rp0_freq` while any request at `translate` or above is in flight, and
  releases it when the last one ends: the claim in `state/node.sqlite`
  and the separate governor app retire together. The frequency bind and
  the sampler (`gpu_samples`, a week at 10 s) move into RAGE's own state
  directory; the System tab reads `GET /louise/gpu`.
- **The fan.** `POST /louise/models/<id>/restart`, by the operator from
  the System page, restarts one child and nothing else. Never automatic.
- **The panel.** `gpu_probe` keeps its per-process rows (it already
  reads `-m` and the server directory from cmdlines). To label them by
  model, the web joins the probe's host pids with RAGE's container pids
  through `NSpid` in `/proc/<pid>/status`, which the probe can read from
  the host namespace. One field added to the probe's snapshot.

## 13. The admin surface (D7)

Load, unload, restart, `PUT` and `DELETE` require
`Authorization: Bearer` with the token read from `ADMIN_TOKEN_FILE`,
mounted into RAGE by path (invariant 8); `/upstream/…`, `/louise/models`
(read) and `/louise/runs/…` are open on the LAN as the model ports are
today. One published port.

## 14. Decided by the operator (2026-09-19: "I accept your suggestions")

1. **RAGE images are published to a public registry by digest at every
   build** (B1). Size to expect: llama.cpp's SYCL layers plus one torch
   XPU plus OpenVINO, roughly 5–6 GB compressed. An OCI tarball over
   04 §4.3 is the fallback when a registry forgets. The registry and
   image name are still to be given (§17).
2. **The `Louise-Level` request header** (§12) is accepted: the governor
   moves into RAGE and its app retires.
3. **Chat's context size is fixed at launch**, `-c 16384` for the chat
   entry, in place of Ollama's per-request `num_ctx`.

Everything else in Part II is accepted as recommended.

## 15. Amendments to name on `main` — made 2026-09-19, 8bbc66a

1. **09 §9.3, `weights`** (and the `weights` of the run identity, §9.12):
   "the digest of the weights file; where the model is a directory, `H`
   of the canonical JSON object mapping each regular file's path
   relative to the directory to its `sha256:…` digest, paths sorted,
   symlinks refused". Needed at once for the run identity of MADLAD and
   the MedCPT IR, which RAGE states like every engine; needed in a Codex
   only when a DNA collection becomes binding (§17.1).
2. **09 §9.12, the run identity**, one sentence: "The platform learns
   its `image` from a deployment pinned by digest and refuses to start
   otherwise; a tag is not an image." A clarification of "computes it
   from what it launched", so the rule of §7 is read as compliance, not
   as a shortcut.
3. **07 §7.8**, the `[encoder]` example: `url =
   "http://nas:30450/upstream/bge-m3"`, and, if §14.2 is accepted, the
   `Louise-Level` header beside `Louise-Run` in 03 §3.3.

## 16. The order, as migration-plan steps

| step | delivers | proved by |
|---|---|---|
| a | Codex v3: `space.encoder = {weights, engine, label}`, `engine_id`, `space_id` without the image, version bumps in header, attestation, digest, calibration; statement version 2 with `run` | golden Codex v3 = worked example byte for byte; v2 still read, never rewritten |
| b | the three servers take `--weights --device --host --port`, bind localhost, carry `build` | their existing checks, plus argv tests |
| c | `app/rage/`: `rage.toml` → yaml (golden), run identity from a fake `/proc` tree, admission arithmetic, header on streamed answers, refuse-to-start without a digest, manifest digest golden, `oom_score_adj` write | `tests/rage/` |
| d | the image, published by digest; §5's done-when list on the node | the numbers in the commit message |
| e | the clients read `Louise-Run`, check it before every write, run the gate on an unknown run; `llm.py` to `/v1/chat/completions`; `Louise-Level` if accepted | `tests/test_run_check.py`: an answer naming an unvalidated run writes nothing and triggers the gate; a stated-then-changed run stops the write |
| f | retire the governor app, Ollama and TEI; `legacy-to-delete.md` gains the three apps with their conditions. `app/verify/backend_gate.py` stays for legacy collections and retires with them at migration step 12 (§18) | every pre-existing test green |
| g | quarantine (§18): the three derived states, the five exit conditions, `hold collection ack --origin-unchecked`, search and share excluding a quarantined collection and naming it under `withheld`. Reported first, enforced in a second deploy | `tests/test_quarantine.py`: each reason enters and clears; a changed run re-quarantines the space's collections; a legacy collection is never quarantined; the state is derived, no row is written for it |

## 17. Still open after acceptance (2026-09-19)

Checked and closed the same day: a `503` from RAGE is already a deferral
in the worker (`_ENDPOINT_BUSY_STATUS` holds 429, 502, 503, 504); and
the resume hole of `fingerprint-implications.md` §3 closes by
construction under per-answer checking, since every stored slice came
from a validated run and 03 §3.3 allows a document vectors from two.

Open:

1. **There is no DNA calibration set — deferred (operator, 2026-09-19:
   "DNA collections are a non-binding work in progress").** `calibration/`
   holds `text.jsonl` only, while the Codex admits `modality = "dna"`, so
   no run can be validated for a genome space and no genome root can be
   created compliant. Nothing about RAGE or v1.0 waits on it. In the first
   proof DNABERT-2 stays as a runtime check only — torch on the Arc and
   the CPU from the same image, vectors matched against today's endpoint
   — and is not gated. The set itself (512 sequences up to the largest
   genome `ctx`, degenerate inputs, every alphabet the corpora use,
   ordered so 09 §9.12's sixteen span the lengths) is composed when DNA
   collections become binding, and not before.
2. **Who runs the gate when the web sees an unknown run** — superseded
   by §18: under quarantine the web never handles a pending run, the
   worker discovers runs by itself, and no table is needed.
3. **Concrete values** before `rage.toml` is real: the registry and image
   name; RAGE's published port (30450 in the examples is a placeholder;
   30437–30448 are taken); its state directory; each model's `memory`
   figures, measured, not guessed; the chat model's GGUF (which qwen3,
   and its path in Ollama's blob store).
4. **`RAGE_URL`** for the web's System page — the models list, load,
   unload, restart — is a new identity-tier variable. The collections'
   `[encoder]` URLs, `CHAT_URL`, `TRANSLATE_URL` and `RERANK_URL` switch
   one at a time to `/upstream/<id>`, each reversible.
5. **DONE 2026-09-19 (8bbc66a, 98ee4dc; plan steps 13 and 14).** The amendments of §15 land on `main` before step a, and the
   migration-plan step is written before the code. Specs on `main`, code
   on `louise`: nothing is staged with `git add -A`.
6. **The front persists its run table** in RAGE's own sqlite, so
   `GET /louise/runs/<id>` survives a front restart. Decided here: it is
   cheap and nothing argues against it.
7. **Stop semantics.** SIGTERM to RAGE forwards to every child; answers
   in flight finish within `stop_grace_period` (60 s proposed). A request
   cut short is a deferral for Louise; the unit of work is Louise's,
   never RAGE's (07 §7.7).

## 18. Quarantine: a collection is not used until it is tested (operator, 2026-09-19)

*The operator: "adding a quarantine status to a collection before the
testing is done. Before testing is done no query or extra commitments
can be made. A collection to be shareable must already carry enough
data to be tested by other peers. It is a user decision to download and
use a new collection."*

**What the specs already have, scattered.** 07 §7.8 withholds a
collection "from every use" until its licence is acknowledged, and
refuses one whose Codex hash, vector width or schema is wrong — "not
served, not shared and not searched", reason printed by `hold
collection show`. 03 §3.3 withholds writes from an unvalidated run and
the vector leg from a failed one, but lets a *pending* run answer
queries. 03 §3.5 also used the word "quarantined" for a burned KEY. The
proposal gathers the first two under one name and tightens the third.

*Corrected while writing the amendment.* This paragraph first said 07
had "already renamed" the key's quarantine to `hold key trust --deny`,
and proposed calling a burned key *denied*. That misread 07's table:
`--deny` replaced the OLD operator command, while a burn is a separate
state no command creates (`--restore` lifts it), kept in its own list in
the trust file. A burned key is **burned** — the specs' own verb — and
is not denied. The trust file's `quarantined` field and the code's 86
uses of the word for keys (a database column among them) keep their
names; only the prose changed (98ee4dc).

**The state.** A held collection is in exactly one of three states on
this node, all DERIVED from facts and never written (invariant 10):

| state | meaning | clears |
|---|---|---|
| **refused** | the copy cannot be right: Codex inner hash ≠ id, vector table not the Codex's space or width, unknown schema (07 §7.8) | only a human |
| **quarantine** | the copy is not yet tested here, or its encoder run is not yet tested, or its licence not acknowledged | by the checks, the gate, or `hold collection ack` — no other command |
| **in use** | searched, extended and shared as its file says | — |

*Corrected:* the third state was first named **held** here. The specs
already use "held" for possession — "the file existing is the collection
being held" (07 §7.8), and a quarantined copy is held in that sense — so
the state is **in use** (`"in-use"` in `--json`), the operator's own
word for it.

A collection LEAVES quarantine when, on this node: (1) `roots` and
`chunks` pass on every shard held; (2) every vector bundle held passes
T0–T2; (3) the gate has RUN against the run the collection's encoder
states — and the query encoder's, in an asymmetric space; (4) the
licence acknowledgement exists where the class requires one. On (3),
settled while writing the amendment: what quarantines is a run NOT YET
TESTED. A run that was tested and FAILED keeps what 03 §3.3 already
said — the vector leg withheld, the text leg in use, nothing written —
because 02 §2.4 promises that "a peer that fails may still search with
what it holds", and a weak peer's text index needs no encoder. The
operator's rule is "no use before the test is done"; a failed test is a
done one, and its consequence stays with what the run produces. All four are the node's own work, deterministic or
against the Codex's reference, needing nobody else. `records-vs-origin`
and the peers' attestations stay what they are: inputs to the trust
level of 03 §3.6 (`unverified · checked · verified · flagged`), shown
beside the state, never a condition of leaving quarantine — that would
make an offline node's every download unusable.

A collection RE-ENTERS quarantine when the run its encoder states is
not the validated one: RAGE relaunched a model with another flag, a new
image, a new driver. Every collection of that space on this node goes
with it, and the worker runs the gate at once (dd9bef4: automatic, no
`--accept`). A `roots` or `chunks` failure on one shard stays per shard,
as 03 §3.6 has it: that shard withheld and re-fetched, the collection
not quarantined for it.

**What quarantine withholds.** No query: the collection enters no search
list, neither leg, and the response names it under `withheld` with
`shards: "all"` and the reason (05 §5.3: never a silent drop). No
commitment outward: not announced, not served to peers, no `vectors
build`, no `share contribute`, no certification, no bundle import.
Inbound work continues — fetching the rest of the copy is what makes it
testable, and the checks and the gate are the whole of what the worker
does for it. `hold collection show` prints the state, every reason, and
the command or check that clears each; `node show` lists quarantined
collections.

**What it changes for RAGE.** The open question of §17.2 disappears.
The worker, each round, asks RAGE `GET /louise/models` once — one call
per round, a dictionary lookup per collection, no walk — and for every
(space, run) pair without a statement runs the gate, bounded and
deferrable, at CORPUS level. The web never meets a pending run: a
quarantined collection is not embedded for, and if an answer ever names
a run without a statement the vector is discarded and the collection
shows as quarantined until the worker's statement lands. No table, no
hand-off. 03 §3.3's "a query answered by a run not yet validated is
used" is withdrawn: the operator prefers one gate's wait, minutes, to a
search over an untested run.

**"Enough data to be tested by other peers."** Already what a drag
carries — the Codex with its calibration member and reference vectors,
the signed snapshot sidecar with every shard's roots, the terms text —
and why a collection with none of them cannot be created (06 §6.6).
Nothing to add; worth stating once as the reason a copy is testable by
anyone who downloads it. Downloading stays the user's act: nothing is
adopted without `share fetch` (07 §7.7).

**Scope: Codex collections only.** A legacy TOML collection names no
Codex (`app/collections.py`: "every collection that predates step 8"),
so it has no calibration member to gate against and no validation plan
to sample by: quarantine cannot test it and must not apply to it, or it
would never leave. Legacy collections keep today's behaviour —
`app/verify/backend_gate.py` included — until migration step 12 retires
the legacy shape (`codex_only` defaults to false until then).

**The first deploy.** The day quarantine is enforced, every Codex
collection on the node has no run statement and no origin check of its
own, so every one of them would leave search at once and return as its
gate and its sample finish. Produce the statements BEFORE enforcing:
the release that adds the states reports them without withholding
anything, the gates and origin samples run, and enforcement is switched
on once `hold collection show` reads `in-use` for every collection meant
to be. Two deploys, no search outage.

**Amendments on `main` — made 2026-09-19, 98ee4dc.**
1. 07 §7.8: the three states and their reasons in one paragraph,
   replacing the two separate withholdings; `hold collection show` and
   `node show` print them; the `--json` object of 09 §9.3 gains `state`
   and `reasons`.
2. 03 §3.3: a pending run quarantines the space's collections on this
   node; it never answers a query.
3. 03 §3.5, §3.6: a key a proof of forgery mutes is *burned*, no longer
   also "quarantined", so the word has one meaning. Not "denied": see
   the correction above. The trust file's field keeps its name.
4. 05 §5.3: `withheld` entries may carry `shards: "all"` with a
   `quarantine: …` reason.

**The origin check (operator, 2026-09-19, revised the same day).** *"If
the origin is not available for testing, but everything else fits, ask
the user to confirm — an informed decision. Then either the collection
is trusted as another that had the origin testing, or it is rejected."*
This replaces an earlier version of this paragraph that let the
collection out with a standing warning. So the origin check is the
FIFTH condition of leaving quarantine, with one human way through:
- The node runs its OWN `records-vs-origin` on every copy it holds, over
  the plan's sample — `validation.sample["records-vs-origin"]` (09
  §9.3) is the "reasonable amount", fixed by the creator — and the bytes
  it re-fetches draw from the transfer budget (invariant 5). It runs in
  parallel with the other four checks.
- **Pass:** the collection leaves quarantine when the other four hold;
  the attestation is published as today.
- **Cannot run** — origin unreachable, or past its bounded wait — while
  the other four hold: a deferral, retried on the worker's schedule, and
  the collection stays in quarantine with the reason `origin unreachable
  — awaiting your decision`, shown with what passed, what could not run,
  and the last attempt's time. The node ASKS: on the Library card, and
  in `hold collection show`, which names the two commands. This is the
  licence acknowledgement's shape (04 §4.4, invariant 9): a gate that
  waits for a recorded human act, with no timeout and no default.
  - `hold collection ack <name> --origin-unchecked` records the
    decision — time and key, beside the licence ack in the collection's
    state — and the collection is **in use like any other**: no warning,
    no mark in search. `hold collection show` lists the record, as it
    lists the licence ack: a fact, not a caveat. A flag on the existing
    verb and not a new one: 07 has `show`, `create`, `ack`, `forget` and
    `derive`, and `ack` already means the operator's recorded, informed
    consent.
  - `hold collection forget <name>` (07 §7.7) rejects it.
- After acceptance the check is still scheduled when the origin returns,
  as it is for every held collection (03 §3.6 counts an attestation for
  two cadences, so held collections are re-checked anyway); a pass
  publishes the attestation the node could not produce before.
- **Fail** on a shard whose root matches the signed snapshot, whenever
  it happens: 03 §3.6's flag is published as today, and here the
  collection goes back into **quarantine** with the failing shard as the
  reason, cleared only by a human. A copy that does not reproduce its
  origin is not used on this node while that stands.
- Amendments, in addition to the four above: 07 §7.7 the
  `--origin-unchecked` flag of `hold collection ack` and its record;
  07 §7.8 the pending decision in `hold
  collection show`; 03 §3.6 a sentence that a local `records-vs-origin`
  failure quarantines the collection on the peer that found it. No
  `warnings` list in the search response: an accepted collection carries
  nothing.
