# RAGE over LocalAI: the protocol, the guarantee, the runner

*2026-10-02. **Proposal, not accepted.** The operator: "RAGE grants the
compliance of the vectors only, while LocalAI will manage pre-build,
preload, caching and eviction policies. RAGE addresses a fundamental RAG
need that no platform addresses today." And: "Louise implements a new
protocol to create, derive, share and contribute collections."*

*Nothing in docs/01–09 changes until the operator approves §6. §5 is a
wire-format change, so it is asked first (CLAUDE.md).*

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

**Gap 1 is the only blocker.** Without it RAGE can state no run for any
model LocalAI serves. Louise would then search and write nothing (03 §3.3),
so the node could not embed at all. Nobody upstream has asked for it: a
search on 2026-10-02 found no issue. The fix is small. LocalAI already
builds these options and hashes them for its distributed registry. The
single-node path needs to keep them on the loaded model and list them,
with their hash, in `/system`'s `loaded_models`.

**Your choice:** wait for LocalAI to merge it, or carry the patch in our
image until they do.

## 5. The engine object for a LocalAI run — a wire-format decision

The engine object (09 §9.3) has no field for options sent over gRPC. Its
`args` is "the argument vector the engine was started with", and a LocalAI
backend is started with `--addr` alone. Everything that shapes its
numbers arrives in `LoadModel`: context size, batch, pooling, GPU layers,
flash attention, threads, seed.

- **A (recommended): add `options` to the engine.** Type: object or null,
  required. It holds the load options the engine received over its own
  protocol, as canonical JSON (§9.1): proto field names, with non-integer
  values as the shortest decimal string that reads back to the same f32.
  The weights path is written `${WEIGHTS}`. Any other path is refused, so a
  run that loads a second file (a projector, a LoRA, a draft model) cannot
  be stated yet. `options` is `null` for an engine started by `args` alone.
  - What it bumps: `louise-codex` 3 → 4 and `louise-calibration-run`
    2 → 3, the two objects that carry the engine whole. The bundle header,
    the attestation and the digest carry only `engine_id`, so their
    schemas stay.
  - What it leaves alone: Codexes already written stay at version 3, and
    their engines stay reproducible from the images they name.
  - What it costs: LocalAI serving the same weights is another engine, so
    the gate admits it like any new build (03 §3.3). Every space on the
    node is re-gated once at cut-over. rage.md §9 already accepted that
    cost for a rebuild.
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

## 6. The amendment, for approval

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
  > `null` for an engine started by its argument vector alone.

- "The platform checks that the process it launched matches them" becomes
  "The platform checks that the process serving the answer is the one it
  read them for — by pid and start time, before and after the answer".

**09 §9.3** gains A's `options` row. **09 §9.18** moves the two versions.
**rage.md** §2 and §4 are rewritten when this is accepted, not before.

## 7. What RAGE keeps, and what goes

| today (`app/rage/`) | becomes |
|---|---|
| `front.py`: the proxy and `Louise-Run` | kept. It proxies to LocalAI on loopback and refuses a body whose `model` is not the one in the URL. URLs stay `/upstream/<model>/…` (07 §7.8) |
| `runs.py`, `procfs.py`, `store.py`: the identity, `/proc`, the runs | kept. `match` reads `/system` and `/proc` in place of llama-swap's argv |
| `clock.py`: `Louise-Level` holds the card's clock | kept. The front is the only process that reads the header, and the header changes no answer |
| `model.py`: `rage.toml` | shrinks to what the identity needs: card names, `pci`, the device per model. The lifecycle keys (`memory`, `priority`, `preload`, `park`, `cmd`) move to LocalAI's model YAML |
| `admission.py`, `placement.py`, `swap.py`, `swapyaml.py` | retired at cut-over, through [`legacy-to-delete.md`](legacy-to-delete.md). Until then they are the reference for the upstream proposals (rage-prior-art §7) |
| `deploy/rage/servers/` and the three servers | become LocalAI Python backends: DNABERT-2 → `Embedding`, MedCPT → `Rerank`, MADLAD → `Predict`. Louise's translator client moves from `/translate` to the completion shape, a change in the app |

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
   key set. The front's `ADMIN_TOKEN_FILE` guards admin requests. That no
   key opens `/system` is UNVERIFIED. Publishing LocalAI's own UI would
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

0. **You:** approve the split, §5's choice and §6's text.
1. **Upstream:** gap 1 as an issue and a PR, plus
   [`localai-bugs.md`](localai-bugs.md). Later, gap 3 and rage-prior-art
   §7 (ledger, priority, refuse instead of overcommit).
2. **On the node:** §8.1 and §8.2. This is where the go/no-go is decided.
   If it fails, today's RAGE keeps running unchanged.
3. **The front over LocalAI.** Tests first: `test_runs` and `test_front`
   run against a fake `/system` and a fake `/proc`.
4. **The three backends.**
5. **Cut-over, as 13f:** record today's vectors first, `node pause`, swap
   the image, re-gate every space, `node resume`. To back out, deploy
   today's image by its digest.
6. **Retirements**, through `legacy-to-delete.md`.

This becomes a migration step after 13f, numbered when it is accepted.
