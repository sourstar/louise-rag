# RAGE over LocalAI: the protocol, the guarantee, the runner

*2026-10-02. **Accepted 2026-10-03**: the patch (§4, carried), §5's
choice and §6's text (applied to 02, 03 and 09). The operator: "RAGE grants the
compliance of the vectors only, while LocalAI will manage pre-build,
preload, caching and eviction policies. RAGE addresses a fundamental RAG
need that no platform addresses today." And: "Louise implements a new
protocol to create, derive, share and contribute collections."*

*Operator, 2026-10-03: "1. Patch LocalAI, note all LocalAI patches in the
same file reporting its bugs. 2. and 3. approved." The work is migration
step 15.*

LocalAI was read at `mudler/LocalAI` 58830f7 (2026-10-02, master), except
where a line says UNVERIFIED.

## 1. Three layers, one job each

| layer | its job | what it gives the layer above | who else does it |
|---|---|---|---|
| **Louise** | the collection protocol: create, derive (02 §2.7), share, contribute (04 §4.5), check (03), ask (05) | — | nobody |
| **RAGE** | vector compliance: every answer states which run produced it, read from what was actually launched | `Louise-Run` on every answer, and `GET /louise/runs/<id>` (09 §9.12) | nobody: no surveyed system states which run answered ([`rage-prior-art.md`](rage-prior-art.md)) |
| **LocalAI** | the model lifecycle: backends, building, preloading, caching, eviction | answers from a model process | many; LocalAI fits this card's mix best (rage-prior-art §3) |

Only the middle layer is new work. The top layer is the reason for it, and
the bottom layer is reused.

## 2. Louise touches a model only through RAGE

| protocol act | what it needs from a model | what it reads from RAGE |
|---|---|---|
| create (`hold collection create`, 06 §6.6) | the reference vectors, and the engine the Codex freezes (02 §2.4, 09 §9.3) | the run object: its `engine` |
| derive (02 §2.7) | nothing: no encoder, no gate (03 §3.3) | nothing |
| embed, contribute (`vectors build`, `share contribute`) | a vector is written only from an answer naming a validated run | `Louise-Run` on each answer; the calibration statement names the run (09 §9.12) |
| check (T3, T4, `vectors-sample`) | runs under a validated run (03 §3.6) | the same |
| ask (05) | query runs validated; quarantine while one is gated | the same |
| every request | — | sends `Louise-Level` (advisory) |

So the protocol's whole contact with models is three items fixed by 09
§9.12: the header, the run object and the level. Moving the lifecycle into
LocalAI changes none of their bytes, on one condition: the run object can
still be computed from what was launched. That condition is §4.

## 3. Why the runner's policy cannot break the guarantee

Everything LocalAI decides changes when an answer comes, or whether it
comes. It never changes what an answer says without the answer saying so.
That covers what is loaded, evicted, preloaded or pinned, and when a
backend restarts. It holds because RAGE states the run of every answer and
Louise checks that run before every write (03 §3.3).

So a bad eviction costs a reload. An overcommit costs speed, or an
out-of-memory kill, which Louise treats as a deferral. Three cases could
break this, and each is handled:

1. **Options LocalAI fills in itself.** LocalAI completes a model's file
   before it loads it. It guesses `ContextSize` and `NGPULayers` from the
   GGUF (`core/config/gguf.go` :57–95), and app-wide runtime settings
   change `ContextSize` (`runtime_settings_registry.go` :229). So the
   model's YAML file is not what loaded, and any of these defaults can
   change the numbers. This is safe only if RAGE reads the options exactly
   as LocalAI sent them (§4, gap 1).
2. **Batching across callers.** The fingerprint (03 §3.3) measures
   batching at the peer's own settings only. On a single node, LocalAI's
   `/v1/embeddings` sends one text per backend call, in sequence
   (`core/http/endpoints/openai/embeddings.go` :112). It does not batch
   requests from different callers. That is compliant; the cost is
   throughput (§8.2).
3. **Spill.** When LocalAI overcommits (it loads anyway after 30 retries,
   rage-prior-art §3), `xe` moves memory to system RAM. The same kernels
   run, so the numbers should not change, but this is UNVERIFIED. Nothing
   would catch a difference, because the run stays the same. The first
   proof measures the fingerprint under spill (§8.1).

## 4. What RAGE needs from LocalAI

| RAGE needs | for | LocalAI at 58830f7 | gap |
|---|---|---|---|
| the process serving model *m*: pid and start time | the check before and after each answer (rage.md §8) | `GET /system` → `loaded_models[].process.pid`, `.started_at` (`core/schema/localai.go` :201–227; admin route, `routes/localai.go` :457) | none |
| the program that process runs | `engine.image`, `build`, `variant` | `/proc/<pid>/exe`; the backend's `metadata.json` (`version`, `uri`, `digest` at install, `core/gallery/backend_types.go` :13) | none, if the backends are in the image (§8.5) |
| its environment | `engine.env` | the model YAML's `env:` reaches the process (`pkg/model/process.go` :293), together with LocalAI's own environment | none: `/proc/<pid>/environ` is checked as today |
| **the options its weights were loaded with** | the run (§5) | sent through gRPC `LoadModel(ModelOptions)`, so they are not in argv. They are hashed only in distributed mode (`EffectiveModelOptionsHash`, `nodes/router.go` :410), and on a single node not even the hash is recorded | **gap 1: blocks** |
| the weights file | `weights` | `ModelOptions.Model` (covered by gap 1); a GGUF also shows as an mmap in `/proc/<pid>/maps` | RAGE hashes the file itself, as today |
| proof that the answer came from that process | the run per answer | single node: one process per model; no header names it | none: checking (pid, start) before and after is enough. A response header naming the instance would be cleaner (gap 2, optional) |

**Gap 1 is the only blocker, and it is patched.** Without it RAGE can state no run for any
model LocalAI serves. Louise would then search and write nothing (03 §3.3),
so the node could not embed at all. Nobody upstream has asked for it: a
search on 2026-10-02 found no issue. The fix is small. LocalAI already
builds these options and hashes them for its distributed registry. The
single-node path needs to keep them on the loaded model and list them,
with their hash, in `/system`'s `loaded_models`.

**Operator, 2026-10-03: carry the patch.** It is P1 in
[`localai-bugs.md`](localai-bugs.md), the one list of LocalAI action
items, and `deploy/localai/patches/` holds it with its base commit. With
it, `GET /system` lists `load_options` and `load_options_hash` for each
loaded model.

## 5. The engine object for a LocalAI run — a wire-format decision

The engine object (09 §9.3) has no field for options sent over gRPC. Its
`args` is "the argument vector the engine was started with", and a LocalAI
backend is started with `--addr` alone. Everything that shapes its
numbers arrives in `LoadModel`: context size, batch, pooling, GPU layers,
flash attention, threads, seed.

- **A (accepted 2026-10-03): add `options` to the engine.** Type: object,
  present only where needed (below). It holds the load options the engine received over its own
  protocol, as canonical JSON (§9.1): proto field names, with non-integer
  values as the shortest decimal string that reads back to the same f32.
  The weights path is written `${WEIGHTS}`. Any other path is refused, so a
  run that loads a second file (a projector, a LoRA, a draft model) cannot
  be stated yet. The runner's models directory is written `${MODELS}`.
  *Amended 2026-10-31 (operator: "Extend for future use in any case … a
  lora is a smart way to decouple the embedder from a tuning"): a second
  file is written `${WEIGHTS:<field>}` and hashed into the weights with the
  LoRA scales (09 §9.3, `runs.load_weights_digest`); it must reach the
  backend as an absolute path.*
  - **Absent, not null, for an engine started by `args` alone.** This was
    proposed as "`null`, required". Applied as "absent" under CLAUDE.md
    RULE 1, and named here as that amendment: with `null`, every engine
    in use today would gain a key. Every `engine_id`, every run and every
    calibration statement would change, and every space would be
    re-gated for nothing. Absent, an argv engine keeps its bytes, and a
    version-3 `space` is a valid version-4 one.
  - What it bumps: `louise-codex` 3 → 4 and `louise-calibration-run`
    2 → 3, the two objects that carry the engine whole. Version-2
    statements stay valid and are read beside version 3. The bundle header,
    the attestation and the digest carry only `engine_id`, so their
    schemas stay.
  - What it leaves alone: Codexes already written stay at version 3, and
    their engines stay reproducible from the images they name.
  - What it costs: LocalAI serving the same weights is another engine, so
    the gate admits it like any new build (03 §3.3). Every space on the
    node is re-gated once at cut-over, and only then. rage.md §9 already
    accepted that cost for a rebuild.
  - **`EnvVars` is the engine's `env`, stated once** (amended 2026-10-03,
    operator). LocalAI copies a model's `env:` into the load call
    (`core/backend/options.go` L579), and the options' path rule refused
    it there: `NEO_CACHE_DIR=/state/neo-cache` on all six YAMLs made every
    run unstatable. The smoke model had no `env:`, so §11 missed it;
    `tests/rage/test_localai_deploy.py` now states a run for every
    shipped YAML. 09 §9.3 says the environment is `env`'s, where paths
    were always allowed, and never again an option. No recorded engine
    changes: none is a LocalAI one yet.
- **B, rejected: options written into `args`,** for example as
  `--louise-options=<json>`. No byte changes, but `args` would stop being
  the argument vector. A verifier who ran it would pass a flag the backend
  refuses.
- **C, rejected: encoders stay on llama-swap and everything else moves to
  LocalAI.** This contradicts the direction. It would also put two
  lifecycle managers on one card, each a stranger to the other, which is
  exactly the case rage.md §19 exists to survive.

With A, the readable fields take this form: `name` is
`"localai/<backend>"`, `build` is `"<LocalAI release>+<backend version>"`,
and `variant` is the backend's variant (`"intel-sycl-f16"`). `args` is the
backend's own argv, with `127.0.0.1:${PORT}`.

## 6. The amendment (applied 2026-10-03)

**03 §3.3**, in the paragraph "Validate once": "the exact arguments and
environment" becomes "the exact arguments, load options and
environment". The sentences from "It is the platform that serves the
models" up to "That makes it" become:

> It is the platform that serves the models, in two parts. A model
> runner — LocalAI on this node — launches, loads, keeps and evicts the
> engines; what it decides changes when an answer comes, never what it
> says. RAGE, the RAG Engine, stands in front of the runner and does one
> thing: it computes the run identity of every answer from what was
> actually launched — the process the kernel shows, the image it runs
> from, the weights it holds, the options the runner sent it — never from
> what a configuration declares, and states it with that answer. Together
> they are a trusted processing platform: …

The rest of the sentence is unchanged.

**09 §9.12, the run identity.**
- "What RAGE, the node's model container, states" becomes "What RAGE
  states, for every engine the node's model runner launched,".
- A bullet is added after `args` and `env`:

  > **`options`.** For an engine that receives its load options over its
  > own protocol, the options as the runner sent them, read at every
  > launch from the runner's record of that load — never from its
  > configuration, which the runner completes with defaults of its own.
  > Absent for an engine started by its argument vector alone.

- "The platform checks that the process it launched matches them" becomes
  "The platform checks that the process serving the answer is the one it
  read them for — by pid and start time, before and after the answer".

**09 §9.3** gains A's `options` row. **09 §9.18** moves the two versions.
**02 §2.4** names the load options among what the engine is, and **02
§2.7** says a version-3 space copies into a version-4 Codex as it is.
**rage.md** §2 now says who launches, and §4 is marked superseded at the
cut-over.

## 7. What RAGE keeps, and what goes

| today (`app/rage/`) | becomes |
|---|---|
| `front.py`: the proxy and `Louise-Run` | kept. It proxies to LocalAI on loopback and sets the body's `model` to the URL's: the URL, from the collection's file, is the only thing that names a model (operator, 2026-10-03). URLs stay `/upstream/<model>/…` (07 §7.8) |
| `runs.py`, `procfs.py`, `store.py`: the identity, `/proc`, the runs | kept. `match` reads `/system` and `/proc` in place of llama-swap's argv |
| `clock.py`: `Louise-Level` holds the card's clock | kept. The front is the only process that reads the header, and the header changes no answer |
| `front.py`: the admin routes, behind `ADMIN_TOKEN_FILE` | kept, as the only door to LocalAI's admin routes. LocalAI takes keys only from an environment value (§8.4), so it listens on loopback with no key and publishes nothing. The front's `load` and `unload` pass through to LocalAI's `POST /backend/load` and `POST /backend/shutdown`, and a request without the token never reaches LocalAI |
| `front.py`: `PUT` and `DELETE /louise/models/<id>`, adding and removing a model | kept, and they now write LocalAI's model YAML, the file for the desired state (operator, 2026-10-03; invariant 10). **Where:** LocalAI reads the YAMLs and the weights from one models path, and refuses weights outside it (`VerifyPath`, `pkg/utils/path.go` L34). So the models path is a writable directory under RAGE's state holding the YAMLs, and the weights are bind-mounted read-only inside it, as today (rage.md §10, invariant 5). `HF_HOME` is set elsewhere, because LocalAI points it at the models path when it is unset (`initializers.go` L122). **`PUT`** writes `<id>.yaml` and calls `POST /models/reload`. It refuses with 422 a YAML that leaves any option in §10's list unpinned, so the API cannot bring back the drift §10 removes. If the model is loaded, `PUT` also shuts it down, so the change arrives as a new run, named and gated. **`DELETE`** shuts the model down, removes its YAML and reloads. LocalAI's reload does NOT forget a config whose file is gone (§11; localai-bugs.md B8), so `DELETE` answers 502 and says the model goes at LocalAI's next start. **Hand-written YAMLs:** today's `rage.d/` rule carries over. The API overwrites only the files it wrote, which it marks with a first-line comment; a hand-written YAML is edited by hand (409) |
| `front.py`: `GET /louise/gpu`, the System tab's panel | kept, fed by other sources. The clock and the container name stay as they are. The card's real memory is still read from the driver (rage.md §19), now split by model using `/system`'s `size_vram`; whatever is left over is a stranger's. Each GPU process is labelled with its model from `/system`'s `process.pid`, joined to the host's pids through `NSpid` as before (rage.md §11). The events become what RAGE sees rather than what it decided: a model loaded or unloaded, from one `/system` reading to the next, and a new run with the options that changed (§10) |
| `front.py`: `POST /louise/models/<id>/restart`, the operator's button for the fan (rage.md §11) | kept. It calls `/backend/shutdown` and then `/backend/load` for that model and no other: never another model, never another app (operator, 2026-09-20). With the options pinned (§10), the reload states the same run |
| `model.py`: `rage.toml` | shrinks to what the identity needs: card names, `pci`, the device per model. The lifecycle keys (`memory`, `priority`, `preload`, `park`, `cmd`) move to LocalAI's model YAML |
| `admission.py`, `placement.py`, `swap.py`, `swapyaml.py`, and the `park` route | retired at cut-over, through [`legacy-to-delete.md`](legacy-to-delete.md). Until then they are the reference for the upstream proposals (rage-prior-art §7) |
| `deploy/rage/servers/` and the three servers | become LocalAI Python backends (`deploy/localai/backends/`, done 2026-10-03): DAR-legal-it → `Embedding`, MedCPT → `Rerank`, MADLAD → `Predict` (DNABERT-2 is frozen, and served by neither front). Each wraps its server's model code unchanged and reads its settings from the load call: `context_size` and `batch` where they mean what LocalAI means, `options` for the rest. MedCPT is read as `api = "jina"` at `/v1/rerank`. **The translator's batch travels in one prompt** (operator, 2026-10-03): LocalAI calls `Predict` once per prompt, so the prompt is the JSON request and the completion's text its JSON answer; Louise's client speaks it under `TRANSLATE_API=completion`, beside today's `/translate` until 15g |

**What the node loses until upstream takes it:** the declared ledger,
priority eviction, stranger-aware pressure and parking (rage.md §7, §19,
§21). LocalAI's count-based LRU, `pinned` and `concurrency_groups` take
their place, and the encoders are pinned. The loss is availability, never
compliance (§3).

## 8. To measure first, on the node

1. **Go/no-go: LocalAI's Intel images on the B70.** rage.md §3 left this
   unverified. Test the llama-cpp SYCL backend with bge-m3, gated by hand:
   fingerprint, batch invariance, and the fingerprint again under spill
   (§3.3).
2. **Throughput.** Compare one text per call (§3.2) against llama-server's
   batched `/v1/embeddings` on the 03 §3.3 benchmark, in tokens/s. That
   number is also the weight 04 §4.5 gives to contributions. If it drops,
   ask upstream for a batched `Embedding` call (gap 3); none was found
   requested.
3. **Weights hashed at every launch** (09 §9.12). RAGE learns of a LocalAI
   launch only after it happens, so the hash no longer runs during the
   load. The first answer after each reload waits for it: about 5 s for
   8 GB, an estimate. LocalAI evicts more often than RAGE did. If this
   costs too much, the way out is a digest cache keyed on a ZFS snapshot
   (rage.md §20), which needs its own amendment.
4. **Invariant 8.** LocalAI takes API keys only from an environment value
   or a flag (`LOCALAI_API_KEY`, `core/cli/run.go` :78). LocalAI therefore
   listens on 127.0.0.1 inside the image, as llama-swap does today, with no
   key set. The front's `ADMIN_TOKEN_FILE` guards admin requests. With no
   key set, the admin routes RAGE uses answer without one (§11, on the
   real LocalAI). Publishing LocalAI's own UI would
   need upstream to read the key from a file.
5. **Backends in the image.** The run's `image` is the node image's digest
   only if every backend is in that image. Install them at build time, from
   backend images pinned by digest, and turn off installs from the gallery
   at runtime. A process whose `exe` is not from the image states no run.
6. **A reload inside a live process.** If LocalAI ever sends a second
   `LoadModel` to a live process, the (pid, start) check misses it. Reading
   gap 1's options before and after each answer catches it. Whether LocalAI
   ever does this is UNVERIFIED.

## 9. Order of work

0. **Done 2026-10-03:** the split, §5's choice and §6's text approved
   and applied; P1 written, tested and carried.
1. **Upstream:** everything in [`localai-bugs.md`](localai-bugs.md), filed
   by the operator.
2. **On the node:** §8.1 and §8.2. This is where the go/no-go is decided.
   If it fails, today's RAGE keeps running unchanged.
3. **The front over LocalAI.** Tests first: `test_runs` and `test_front`
   run against a fake `/system` and a fake `/proc`.
4. **The three backends.**
5. **Cut-over, as 13f:** record today's vectors first, `node pause`, swap
   the image, re-gate every space, `node resume`. To back out, deploy
   today's image by its digest.
6. **Retirements**, through `legacy-to-delete.md`.

This is migration step 15 (`migration-plan.md` (deleted 2026-10-08)).

## 10. What a change of options touches (operator's question, 2026-10-03)

*"A trivial options change, from LocalAI's point of view, may disrupt the
formal compliance of all the collections." Checked against 09 and
LocalAI 58830f7.*

**The identifiers.**

| identifier | holds `options`? | when an option changes |
|---|---|---|
| `space_id` (09 §9.3) | no: the engine is outside it | nothing. Vectors stay comparable and nothing stored changes |
| `collection_id` | only through the engine a Codex freezes at creation | nothing for an existing collection. A collection created afterwards names the new engine, as a changed flag does today |
| `engine_id`, `run_id` (09 §9.12) | yes | a new run |

**What a new run costs.** Stored vectors carry no run (03 §3.3), and
attestations already made stand, so nothing published changes and no
wrong vector gets in. The cost is availability on this node:
- Every space the model serves goes into quarantine: the vector leg of
  search is withheld and writes are deferred.
- This lasts until the gate, which starts at once by itself, passes the
  new run.
- If the change really alters the numbers, the gate fails, or limits the
  peer to verifying, and contribution stops. That is the gate doing its
  job. A context below the space's `ctx` is the likely case.

The cost scales with the number of distinct option sets, not with
reloads. A statement is kept per run, so a run that comes back is valid
at once. The gate's duration is not measured yet (rage.md §14).

**Why this is worse than llama-swap.** Today only an edit to `rage.toml`
changes a run. LocalAI fills in every field a model's YAML leaves unset,
from inputs nobody edits per model:

| option | filled from | where |
|---|---|---|
| `ContextSize` | the app-wide `context_size` runtime setting; otherwise `autoContextSize`, which steps down with the detected VRAM | `model_config.go` L1617, `context_fit.go` L106–L136 |
| `NBatch`, for an embedding or rerank model with a context above 512 | `SinglePassBatchForContext(detected VRAM)` | `core/backend/options.go` L345–L356 |
| `Options` `parallel:N` | detected VRAM and context | `hardware_defaults.go` L262 |
| `Threads` | the app-wide `threads` runtime setting | `model_config.go` L1599, `runtime_settings_registry.go` L222 |
| all three VRAM-driven rows | the `vram_budget` runtime setting caps the detected VRAM; detection runs at every load and a failure reads as 0, which takes another branch | `runtime_settings_registry.go` L244, `xsysinfo/gpu.go` L170–L200, `hardware_defaults.go` L287 |
| a companion artifact's path | a cache key that changes when the artifact is resolved again | `core/backend/options.go` L362–L367 |
| `Seed` | a RANDOM number at every load, when `parameters.seed` is unset or -1. Found by the smoke test (§11), not by reading: each reload was another run | `core/backend/options.go`, `getSeed` |

So one change in LocalAI's settings page, `vram_budget` say, can give
every unpinned embedding model a new run at its next load. Every space on
the node then goes into quarantine in turn, at whatever moment LocalAI
reloads each model. That is the disruption the operator foresaw.

**The answer: pin, and make drift visible. The identity stays as it is.**
1. **Pin (15e).** Every model RAGE serves sets, in its YAML, every field
   the table names: `context_size`, `batch`, `threads`, `gpu_layers`,
   `seed` (not -1), and `parallel` in `options`. Add `f16`, `flash_attention` and `mmap` while
   at it. The image sets `LOCALAI_DISABLE_HARDWARE_DEFAULTS=true` and
   `LOCALAI_DISABLE_GUESSING=true`. Explicit config always wins in
   LocalAI, so no runtime setting reaches a pinned field. A run then
   changes only by a deliberate edit, as a llama-server flag does today. A
   test holds every model YAML to the list, which is re-read whenever the
   LocalAI base moves.
2. **Visible (15d).** When a model's run changes, RAGE records an event
   naming the options that differ from its previous run, on the System
   page, for example "bge-m3: new run — NBatch 512 → 2048".
3. **Not narrowing `options` to what "matters".** Dropping options
   believed inert is a declaration, not a measurement (02 §2.4). If one
   such option did change the numbers, the vectors it produced would pass
   every check while being wrong. An extra gate is the safe way to fail.
4. **Not proposed: lift quarantine on a fingerprint match.** A new run
   whose sixteen fingerprint vectors equal a validated run's bit for bit
   (03 §3.3: "stronger evidence than any tolerance") could stop the
   quarantine before the full gate ends. That is a spec change, and it is
   worth considering only if pinning leaves the window costly once the
   node has measured it.

The one LocalAI-side improvement is R5 in
[`localai-bugs.md`](localai-bugs.md): a strict mode in which LocalAI
fills in nothing.

## 11. Smoke test on a CPU (2026-10-03)

*`deploy/rage/Dockerfile.localai`, target `smoke`: LocalAI built from the
series' base with P1, its CPU llama.cpp backend from the same commit, and
RAGE's front. The model is `tools/rage_smoke_model.py`, a 480 KB GGUF with
random weights. Built and run in the development sandbox. No card is
involved, so this is not 15c.*

```
docker build -f deploy/rage/Dockerfile.localai --target smoke \
  --build-arg LLAMA_CPP_IMAGE=ubuntu:24.04 -t rage-smoke .
python tools/rage_smoke_model.py <dir>/models/weights/tiny/tiny.gguf
docker run -p 18080:8080 -e RAGE_IMAGE=sha256:<64 hex> -e ADMIN_TOKEN_FILE=/run/token \
  -v <dir>/config:/config -v <dir>/models:/state/localai-models \
  -v <dir>/token:/run/token:ro rage-smoke
# PUT /louise/models/tiny with the token, then POST /upstream/tiny/v1/embeddings
```

**What held.**
- The series applies at the base commit, and the build gives
  `58830f7+patches.17b54353635e`.
- `PUT` writes the YAML and LocalAI reads it.
- The first answer arrives in 2.17 s with the load included; the next in
  0.027 s.
- Both carry `Louise-Run`. The run object is built from the real process:
  - argv `…/llama-cpp/lib/ld.so …/llama-cpp-cpu-all --addr 127.0.0.1:${PORT}`,
    under the system backends path;
  - device `cpu`, driver `cpu`;
  - options with `${WEIGHTS}` and `${MODELS}`.
- A body naming another model was answered by the URL's.
- The admin routes refuse without the token.
- With the seed pinned, three answers across two restarts (pids 25, 52
  and 78) stated ONE run, and the events said "the same run".

**What it found.**
1. **The seed.** Unset, LocalAI draws a random one at every load. Each
   reload was another run, and RAGE's event named it: "Seed 551695963 →
   1045691491". Now on §10's list: `PUT` requires `parameters.seed`
   (not -1), and the six YAMLs pin it.
2. **Fixed defaults.** LocalAI also fills `FlashAttention: "auto"`,
   `MMap: true` and `cache_reuse:256`. These are constants of the build:
   they move only with the image, which is a new run anyway. They are not
   required.
3. **B8.** `/models/reload` never forgets a deleted YAML
   (localai-bugs.md). `DELETE` answers 502 and says so, `GET
   /louise/models` shows the model with `file: null`, and it goes at
   LocalAI's next start. This settles §8's UNVERIFIED point the other way.


### 11.1 A Python backend (15e-2, the same day)

The same target, with `louise-medcpt-rerank` added on OpenVINO's CPU (no
torch: only the node's image has it). The model is
`tools/rage_smoke_reranker.py`: MedCPT's three inputs and a BERT pair
tokenizer, with a logit that counts "whale" in the document segment only.

```
python tools/rage_smoke_reranker.py <dir>/models/weights/medcpt/medcpt-cross-ov
# run as above; PUT deploy/rage/localai-models/medcpt-rerank.yaml with options ["device:cpu"]
# POST /upstream/medcpt-rerank/v1/rerank {"query": ..., "documents": [...]}
```

**What held.**
- LocalAI found the backend in the system path, spawned its `run.sh`, and
  loaded it over gRPC with the stubs generated from the series' own
  `backend.proto`.
- `/v1/rerank` ranked 2 "whale"s, then 1, then none, with TEI's sigmoid
  scores (0.881, 0.731, 0.5). A body naming another model was answered by
  the URL's.
- First answer 2.46 s with the load, then 11 ms. Both carried `Louise-Run`.
- The run: `name` `localai/louise-medcpt-rerank`; `env` with
  `NEO_CACHE_DIR` and `OV_CACHE_DIR`, and no `EnvVars` among the options
  (§5); the weights digest of the directory; device `cpu`.
- OpenVINO wrote its compiled model to `/state/ov-cache`.
- A restart gave a new process (pid 24 → 66) and stated the same run.
- A misspelt option (`devise:cpu`) refused the load, and the caller got
  the backend's reason in LocalAI's error.
- What MADLAD's design rests on, checked with a throwaway backend built
  from `louise_backend.py` that answers its prompt back: a JSON prompt
  with Unicode, paragraph breaks and edge spaces reached `Predict` byte for
  byte through `/v1/completions`, and the reply came back as the
  completion's text, unchanged. A vector declared `FINAL` came back from
  `/v1/embeddings` as sent, not normalised.

**What it found.** LocalAI starts `run.sh` as `./run.sh` from the
backend's directory, so the first run's `args` were
`./venv/bin/python ./backend.py`. That is an argv relative to a directory
the run does not state. `run.sh` now resolves its directory to an
absolute path, and a test starts it the way LocalAI does.

Not run here: DAR-legal-it and MADLAD, whose torch is on PyTorch's index,
which this sandbox cannot reach. Their gRPC methods are tested with
stand-in models (`tests/test_localai_backends.py`). Their libraries are
imported at the image's build, on the node.

## 12. The go/no-go on the node (15c, 2026-10-04)

*Run unattended at the operator's word. The image
`ghcr.io/sourstar/rage@sha256:b203a361…a092d` ran as a throwaway container
(`rage-15c`, port 30499, its own folder `/mnt/ssd5/apps/louise-rage-15c`,
no clock) beside the live RAGE, which was not touched. The scripts were
`gate15c.py`, `nan15c*.py` and `fp15c.py`, run from Louise's own image with
the branch's code. They are throwaway and kept in `/tmp/15c-scripts` on
the node.*

**bge-m3, by wikipedia's own gate** (03 §3.3: its Codex, its 512
calibration texts, its tolerance 0.95), through Louise's client:

| engine | gate | lowest of 512 cosines | tokens/s | batch-invariant |
|---|---|---|---|---|
| live: llama-server b10630 under llama-swap | 16/16, median 1.0000 | 0.999004 | 9,509 | false |
| LocalAI 58830f7 + P1, SYCL f16 backend, 3 passes | 16/16, median 1.0000, each pass | 0.999004–0.999006 | 10,651–10,756 | false |

- On the card: 1,036 MiB at first, 1,149 MiB once warm. The first answer
  took 12.6 s with the load and the weights hash; RAGE stated a run on
  every answer.
- **Throughput (§8.2).** One text per backend call is 12 % FASTER than
  llama-server's batches here. R2 (a batched `Embedding`) is not needed.
- **The fingerprint is not stable on either engine.** Five passes of the
  sixteen fingerprint texts gave five fingerprints on each. 3 or 4 of the
  16 texts vary in their last bits, with an absolute difference of at most
  2.0e-4 on the live engine and 1.6e-4 on LocalAI. It is a property of the
  SYCL encoder, not of LocalAI. The fingerprint stays what 03 §3.3 makes
  it, evidence and never a check the worker repeats. But two statements of
  one run will not carry equal fingerprints.
- **Batch invariance** is false on both engines, in line with the point
  above.

**What it found: LocalAI bug B9.** Two of the first three gate passes
ended in HTTP 500 "json: unsupported value: NaN". The NaN is intermittent:
the same texts, alone or in the same batches, came back finite on every
retry, and 512 texts sent alone gave none. llama-server sends such a vector
as nulls, which Louise's client has always retried. LocalAI cannot encode
it and fails the request. Louise would have recorded an error on the
document, and an error takes the document out of every selection.
**Fixed before going on:** `embeddings._numeric_failure` retries that 500
exactly as a null vector is retried, and never cuts the input
(`tests/test_embed_recipe.py`, two tests, both failing without the fix).
With it, three gate passes out of three.

Over-long inputs: both engines refuse the same ~61 raw calibration texts
above the context. LocalAI's "too large to process" 500 is the one Louise
already shrinks on (`_over_budget`, 2026-09-15).

**The MedCPT reranker on the card** (OpenVINO GPU, `louise-medcpt-rerank`),
against the live one on five PubMed-style pairs:
- the same order;
- largest score difference 2.9e-8;
- 289 MiB on the card;
- 4.7 s for the first answer with the load, 29 ms after;
- a run stated.

**Not run, and why:**
- **The fingerprint under spill (§8.1).** Spilling means overcommitting
  the card, which would push the live models off it.
- **DAR-legal-it (≈6 GB) and MADLAD (≈8 GB) on the card.** About 6 GB was
  free beside the live models.

Both need a window in which the live RAGE gives up its models, or the
cut-over itself.

**What the test did to the live node:** while it held 1.4 GB of the card,
the live RAGE gave up MADLAD (idle) under pressure, by design (rage.md
§19). It comes back on the next translation, at the cost of a load.
Nothing else changed, and the test container is removed.

## 13. Text pairs: `input_pairs` and `louise-medcpt-article-ov` (2026-10-08)

The RAGE half of [`input-forms.md`](input-forms.md). LocalAI's embeddings
endpoint takes strings and hands a backend one string per call, so a
tokenizer text pair (MedCPT's title, abstract) has no way through it.

**The front** (`app/rage/pairs.py`, called by the `/upstream` proxy).
- An embeddings body may give `input_pairs: [[first, second], …]` instead
  of `input`. It is accepted only for a model whose YAML declares the
  option `pairs:true`. That option goes in the load call, so it is in the
  run's engine options and its `run_id`. Any other model: 400, naming why.
  A pair is never flattened.
- Each pair goes on as one marked string, `"\x1eLOUISE-PAIR\x1e" +
  json.dumps([first, second])`. A plain `input` that carries the marker is
  a 400 for every model.
- After the answer: if the run that answered was loaded without
  `pairs:true` (its YAML hand-edited and not reloaded), the answer is a 502
  and no vector goes out. The answer still carries `Louise-Run`.

**The backend**, `deploy/localai/backends/louise-medcpt-article-ov`. It
runs MedCPT's article encoder on OpenVINO and implements `Embedding`. Its
model code is `deploy/medcpt-rerank/server.py`, which the cross-encoder
uses too:
- `make_encoder`: the model's own pair tokenizer, segment ids included,
  cut longest-first to 512 tokens, as NCBI's code;
- `make_cls_infer`: the `[CLS]` row of the last hidden state.

A marked string is decoded into a true pair. A plain string is one text.
Options: `device:xpu|cpu` and `pairs:true`. Model YAML:
`deploy/rage/localai-models/medcpt-article-ov.yaml`.

**The weights.** The IR is built by `deploy/medcpt-rerank/convert_article.py`
from the Hugging Face snapshot already on the node, fetching nothing. It
writes `/mnt/ssd5/apps/medcpt-models/medcpt-article-ov`, which is mounted
as `weights/medcpt/medcpt-article-ov`. The IR outputs the last hidden state
with fp32 weights. The script checks that the backend's tokenizer path
reproduces transformers' encoding exactly, and that the IR on CPU matches
transformers at a cosine of at least 0.99999.

llama.cpp keeps the plain-text spaces. If a backend on any vendor learns
pairs later, it can serve pair spaces by declaring `pairs:true`.
