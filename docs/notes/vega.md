# VeGa, the Vector Gate: receipts in place of a front

*Operator's proposal and decisions, 2026-10-08. Supersedes the front of
[`rage-over-localai.md`](rage-over-localai.md) once it is proved on the node
(§6); until then RAGE runs unchanged.*

## 1. The proposal

1. RAGE is renamed **VeGa, the Vector Gate**: its job is to make every
   vector's provenance provable, nothing else.
2. VeGa sits **between LocalAI and its backends**, as a gRPC pass-through:
   LocalAI's `backend-xyz` is also installed as `vega-backend-xyz`, the same
   backend behind VeGa's wrapper.
3. Ordinary backends work as usual (LocalAGI's model); `vega-` backends serve
   Louise.
4. So **LocalAI is the only one handling VRAM** on the card. Today two
   lifecycle managers share it — RAGE's (a patched LocalAI inside its own
   image) and the LocalAI app's — which is the case rage.md §19 and
   [`gpu-card-overcommit`] exist to survive.

Operator, same day: Louise makes **two requests, the embedding and the
receipt, and accepts the vectors only if she gets both**; and **query
encoding at question time requires a receipt too** ("Of course").

## 2. What it looks like

```
Louise ──POST /v1/embeddings (model vega-xyz, tagged inputs)──► LocalAI app
   │                                                               │ gRPC Embedding, one text per call
   │                                                               ▼
   │                                                     vega wrapper (in LocalAI's container)
   │                                                       │ strips the tag, forwards
   │                                                       ▼
   │                                                     real backend (llama.cpp SYCL, OpenVINO, …)
   │                                                       │ vector
   │                                       receipt ◄───────┘ (sent BEFORE the vector goes back)
   │                                          │ unix socket on a dataset both containers mount
   └──GET receipts?ids=… ─────────────────► VeGa (receipts + run descriptions, read-only HTTP)
```

- **The tag.** Every input Louise sends to a `vega-` model is one marked
  string: a marker, then canonical JSON with a one-time id, the input form
  (text or pair, [`input-forms.md`](input-forms.md)) and its content. The
  pair marker of input-forms phase 3 becomes this one, for every form.
- **The wrapper** refuses an input without a tag, strips it, forwards the
  bare text or pair to the real backend, and writes the receipt: the id,
  the run, the digest of the input it received and the digest of the
  vector it returns (f32 little-endian). Then it returns the vector.
- **Louise** recomputes the vector digest from what LocalAI returned, reads
  the receipts of the batch from VeGa, and writes nothing unless every id
  has a receipt, the digests match, and the run is validated (03 §3.3). No
  receipt, or VeGa unreachable, is a deferral, never an error on a document.
- **What needs no receipt:** reranking, translation, chat — they write no
  vector — go straight to LocalAI, `vega-` or not.

## 3. Why it is stronger than the front

- LocalAI is out of the trust path: it routes and schedules, and the two
  digests prove it changed neither an input nor a vector. A request it
  routed to a non-`vega` backend has no receipt and is refused.
- The check sits in Louise, the writer, not in a header she must trust.
- **The LocalAI patch goes.** RAGE needed P1 (`deploy/localai/patches/`) to
  learn the options a model was loaded with; the wrapper receives
  `LoadModel(ModelOptions)` itself. It is also the backend's parent, so it
  knows its exe, argv and environment without `/proc` matching, and it can
  hash the weights DURING the load (rage-over-localai §8.3's cost goes).
- VeGa leaves the data path: vectors travel LocalAI → Louise once.

## 4. What changes in the specification

To write when §6 passes, before the code ships:

- **03 §3.3**: the platform states the run of every answer by a receipt
  bound to the input and the vector, read by the writer before the write;
  the runner is outside the trust path.
- **09 §9.12**: the tag, the receipt and the receipts route replace
  `Louise-Run` (kept as long as RAGE runs). Wire format — operator's
  proposal, so approved in principle; the bytes get their own review.
- **09 §9.3, `engine.image`**: the backends come from LocalAI's gallery as
  OCI images (`/mnt/tank/apps/localai/backends`), not from one node image.
  The engine names the backend image's digest. Every engine changes, so
  every space is re-gated once — a gate run, not a re-embedding (RULE 1).
  **Operator, 2026-10-08 (after §10's findings): no pinning.** LocalAI
  installs and upgrades backends as it likes, `latest-` tags included; the
  wrapper hashes the backend's INSTALLED files at every load (never the
  gallery's `metadata.json` digest, which is the registry's word, not the
  bytes'), and the engine's driver is the one the backend actually loads
  (its bundled Level Zero, not the LocalAI image's dpkg). An upgrade is
  therefore a new run, gated before its first vector (03 §3.3). The Codex
  cannot be updated and keeps the engine it was created with forever; later
  engines are admitted by their own calibration statements beside it. So
  for each Codex the backend it names is ARCHIVED on tank (as Codex platform
  images are), for a verifier to reproduce the reference vectors after
  upstream drops that version.

**Applied 2026-10-08**, before the code follows: 01 vocabulary (engine,
VeGa, receipt); 02 §2.4 (the engine, receipts, the archive) and §2.7
(`space_from` of a backend engine); 03 §3.3 (no pinning, VeGa and the
receipt, embedding and searching); 09 §9.3 (`version` 6, the `image` and
`args` rows, *a backend engine*, the tree digest, the archive), §9.12
(the run identity by receipt or `Louise-Run`, five bullets with
`driver`, the tag, digests, receipt, routes, intake, reading the
answer; the statement's version 4), §9.18 (two rows moved, one added).
Decided in writing it, for the operator's review:
- **Versions** move for an object carrying a backend engine alone:
  `louise-codex` 6, `louise-calibration-run` 4; everything else is written
  at 5 and 3, so no byte already written moves (RULE 1). No key was added;
  a backend engine is told by `${BACKEND}/` in `args[0]`.
- **`args`** is `["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"]`,
  relative as the options are to `${MODELS}`. The wrapper still writes the
  absolute path (§9): it must follow, and the codex validator must admit
  `${BACKEND}` only there and only from version 6.
- **Still open: `build`'s fallback bytes.** §9 says the tree digest's first
  12 hex digits; `vega_wrapper.short_build` writes `sha256:` + 12. 09 §9.3
  marks it unfixed until one is chosen.

## 5. Facts, read on the node 2026-10-08

- The LocalAI app runs `quay.io/go-skynet/local-ai:v4.11.0-gpu-intel` with
  `/dev/dri`; its backends are on tank and include
  `intel-sycl-f16-llama-cpp`. RAGE's LocalAI is built from 58830f7, which
  is v4.11.0 too: one gRPC protocol for both.
- The weights sit on ssd5 under RAGE's mounts; the LocalAI app mounts
  `/mnt/ssd5/apps/localai/models`.

## 6. To prove before anything is retired

1. **f32 through JSON.** Go writes a float32 as the shortest decimal that
   reads back to it, so the digest should survive LocalAI. Prove it on the
   real LocalAI with real vectors. *Before the node (§9): exhaustive over
   every float32, two values and negative zero needed handling, and are
   handled; the proof on the real LocalAI is still to do.*
2. **LocalAI's eviction on xe.** Its LRU, `pinned` and active-backend limit
   must keep the encoders on the card under LocalAGI's load. RAGE's governor
   reads the card's free memory; LocalAI may not. If it falls short the fix
   goes upstream or into LocalAI's config, never a second scheduler.
   *On the node (§11): proved for the loads tried, by the memory reclaimer
   and `pinned`, not by `vram_budget`. Still open: LocalAI cannot see RAGE's
   memory, and it checks nothing before a load.*
3. **The fingerprint through the wrapper** equals the fingerprint direct
   (03 §3.3): the wrapper forwards bytes, so it must.
4. **Throughput**: one extra small request per batch; measure tokens/s.

## 7. The node

- **Never delete a TrueNAS app** (operator, 2026-10-05). An app cannot be
  renamed. **Operator, 2026-10-08: keep the `louise-rage` app name for now**;
  it hosts VeGa, slimmed to the receipts service.
- The LocalAI app gains the `vega-` backends and one host-path mount (the
  receipts socket), and the model YAMLs and weight mounts of the models
  Louise uses.

## 8. Order of work

1. Input-forms phases 1–2 land (the single embedding entry point is where
   the tag and the receipt check go).
2. The wrapper (a generic gRPC proxy that intercepts `LoadModel` and
   `Embedding` and forwards everything else as opaque bytes), the receipts
   service, Louise's client; tests against a fake LocalAI.
3. §6 on the node, with a `vega-` llama.cpp beside the live RAGE.
4. The specification (§4), then the cut-over, then the pubmed migration of
   input-forms.md.

## 9. What was built (2026-10-08, §8 step 2)

Not deployed; nothing on the node. Wired into `app/embeddings.py` on
2026-10-08 (below, **Louise's path**).

| file | what |
|---|---|
| `app/vega/tag.py` | the tag and the two digests — ONE module, carried into the wrapper's package |
| `app/vega/service.py`, `__main__.py` | the receipts service: intake on a unix socket, read-only HTTP |
| `app/vega/client.py` | Louise's side: `tag_batch`, `verify_response` → vectors and run ids, or `Refusal` / `Deferral` |
| `deploy/vega/` | the wrapper (`vega_wrapper.py`), `run.sh`, `vega.example.json`, `requirements.txt` |
| `app/rage/runs.py` | `driver_string(runtime_lines=…)`: the wrapper reads the runtime from dpkg's status file |
| `tools/vega_f32_json.go` | the float32-through-JSON check, exhaustive and golden |
| `tests/vega/` | 94 tests; the 15 gRPC ones need grpcio, protobuf and grpcio-tools |

**The tag.** `"\x1eVEGA/1\x1e"` + canonical JSON (09 §9.1) of
`{"form":"text","id":<32 hex>,"text":…}` or `{"form":"pair","id":…,"pair":[a,b]}`;
anything else, a non-canonical byte included, is refused. `input_digest` =
`H` of that object without `id`; `vector_digest` = `H` of the vector as
little-endian f32. The phase-3 pair marker stays as the BACKEND's form of a
pair (`louise-medcpt-article-ov` reads it): replacing it in RAGE's front, the
backend and their tests is not trivial while RAGE runs.

**The intake.** One canonical JSON line each way: `{"kind":"run","run_id","run"}`
(accepted only if `run_id` = `H(run)`), `{"kind":"receipt","id","run_id",
"input_digest","vector_digest"}`; answers `{"ok":true}` or `{"ok":false,
"code":"unknown-run"|"conflict"|"invalid","error"}`. The wrapper tells each
connection its run before the first receipt. An id is one-time: the same
receipt again is acknowledged, another one refused. **HTTP:** `GET
/vega/receipts?ids=…` (missing ones absent), `GET /vega/runs/<run_id>`.

**Bounds.** Receipts: 600 s, at most 100,000 (≈65 MB), 1,024 ids a read,
1 MiB a line. Wrapper: the receipt acknowledged within 2 s or the call fails
UNAVAILABLE (`VeGa unavailable`, which `client.is_deferral` reads); the
weights hash 1,800 s; a stop's grace 30 s.

**The run** is RAGE's identity computed by the backend's parent: `name`
`localai/<metadata name>` (else the directory's), `variant` null. **`image`
is the backend's installed files, hashed at every load (`tree_digest`; §4,
no pinning) — PENDING the 09 §9.3 amendment.** It is `H` of the canonical
JSON (09 §9.1) of `{<path>: <entry>}` over every entry below the backend's
directory, walked without following links, except the top-level
`metadata.json` (LocalAI's install record: a reinstall of the same bytes is
the same engine). `<path>` is relative, `/`-separated, UTF-8. A file is
`{"type":"file","exec":<any execute bit set>,"size":…,"sha256":"<hex>"}`, a
link `{"type":"link","target":<its readlink string>}`, a directory
`{"type":"dir"}`; anything else, or a name that is not UTF-8, states no run.
Owners, times and the other mode bits are not in it. Written `sha256:<hex>`,
as a weights directory is: 09 §9.3's validator, which a calibration
statement passes, reads no other form. A file is read again only when its
(dev, inode, size, mtime_ns, ctime_ns) at its physical path changed. Cost,
read on the node: the SYCL backend's 1,059,780,025 bytes (45 files, 32
links) hash in 1.02 s warm, alongside the load; cold not measured.
`metadata.json`'s `uri` and `digest` are logged at each load, never identity
(§10, finding 3). `build` is the metadata's declared `version`, else the
tree digest's first 12 hex digits, without `sha256:`; never the `uri`'s
moving tag. `args` `["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"]`
(09 §9.3, a backend engine; the child is started with the real absolute
path); `env` the load's
`EnvVars`; `options` RAGE's canonical form of the ModelOptions received — a
field the wrapper's `backend.proto` cannot name refuses the load; `device`
read from the child's DRM clients (`vega.json` `cards`). **`driver`** is the
kernel and module as RAGE reads them, then the driver library the backend
loads (§10, finding 4): `ZE_ENABLE_ALT_DRIVERS` from the backend processes'
own `/proc/<pid>/environ` (the environment the wrapper gave, when none can be
read), else the one its `lib/` carries (`libze_intel_gpu.so.*`), each as
`<file name> sha256:<hex>` of the file a link names. With neither, the
runtime is read from the LocalAI image's dpkg status, as before. An upgrade
is therefore a new run, gated before its first vector.

**Found.**
- **f32 through JSON (§6.1), exhaustive** over all 4,278,190,080 finite
  float32 with go1.26.8: ±7.038531e-26 come back as their even neighbour
  when Python reads Go's JSON; `tag.f32_bytes` repairs exactly those two.
  Go writes negative zero `-0`, which Python's `json` reads as the integer
  0: `client.parse_response` keeps it, so the body is read there, never with
  `response.json()`. `encoding_format: "base64"` reads no decimal at all.
- LocalAI's Go-side pooling (a per-token layout) computes a vector no
  receipt names: the wrapper refuses such an answer.

**Louise's path (2026-10-08).** An encoder opts in with `api = "vega"` in
its collection's `[encoder]` (it holds for `query_url` too) and `model =
"vega-…"` (since the same day `embed_model` and `query_model`, 07 §7.8); where receipts are read is node identity, `VEGA_URL` (07 §7.8,
amended). RAGE's `api = "openai"` + `Louise-Run` path is unchanged.
`embeddings._request_vouched`, under the one entry point, so chunks,
queries, the gate, probes and calibration all take it: every input tagged
(a pair as the tagged pair, never `input_pairs`), `encoding_format:
"base64"`, a JSON body read by `parse_response`, NaN first (handed back
unverified to the NaN retry), then the batch's receipts read after the
dispatch gate is left. The receipts' run replaces `Louise-Run` and meets
the same check before a write or a query (two runs in a batch: each held
to it); `run_check.stated_run` reads the run's description from
`<VEGA_URL>/vega/runs/<run_id>`. `ReceiptDeferred` (VeGa away, `VEGA_URL`
unset, a receipt missing, the wrapper's `VeGa unavailable`) is a
`ConnectionError`, so the worker defers as for an unreachable encoder and a
search falls back to its text leg; `ReceiptRefused` (a digest that does
not match, a malformed receipt or body) is a `RunRefused`: no write, the
document deferred untouched, the verdict forgotten so the next look pauses
the embedding with the reason, and a search withholds its vector leg and
says why. `hold embedder calibrate` takes `api = "vega"`. Tests:
`tests/vega/test_embedding_path.py`.

**Open.**
- The package build (stubs from the release's `backend.proto`, LocalAI's
  `grpc_auth.py`/`parent_watch.py`, the `app/` files, the venv) and its
  install beside LocalAI's backends; `metadata.json`'s `digest` as the
  gallery writes it, read on the node (§6). Built by hand for the proof
  and the digest read: §10.
- Mounting `service.router` and an `Intake` in the louise-rage app.
- An asymmetric space under `vega`: the query side takes its model from
  `query_url`'s `/upstream/<model>` (`embeddings._side`), which LocalAI's
  URL does not carry; naming it needs a key (`query_model`?) — a schema
  addition, not made.
- `args` names the program under LocalAI's backends path; relative to the
  backend image's root is the alternative — for the §4 amendment.

## 10. The node proof (2026-10-08)

*§6 on the node, beside the live RAGE, which was only read. Scripts in
`deploy/vega/proof/` (not committed): `build_package.sh` builds the package,
`proof.py` and `proof_direct.py` measure, `gpu_snapshot.py` reads the card as
`app/gpu_probe` does. Texts: 2,048 docstrings of Python's standard library
(295,514 characters; 81,754 tokens by LocalAI's tokenizer, 40 a text), in
batches of 32. Nothing came from a Louise collection.*

**What was built.** `vega-llama-cpp` in LocalAI's backends path, in front of
`intel-sycl-f16-llama-cpp`. It holds the wrapper and the `app/` files it
carries, and stubs from the v4.11.0 `backend.proto` (grpcio-tools 1.84.0).
`grpc_auth.py`, `model_identity.py` and `parent_watch.py` come from the
v4.11.0 tag and are byte-identical to `intel-rerankers`' copies. The runtime
is a portable CPython 3.12.11 (python-build-standalone 20250818, sha256
`98229938…f053`) with a venv symlinked into it, as LocalAI's
`_makeVenvPortable` does, holding grpcio 1.84.0 and protobuf 7.36.1
(`intel-rerankers` carries 7.36.2). The receipts socket is in the package's
`run/`, which both containers see, so no mount was added. The receipts
service ran as `vega-proof` (`python -m app.vega` on the pinned
python:3.12-slim). The real backend's `metadata.json`, verbatim:

```
{"alias": "llama-cpp", "name": "intel-sycl-f16-llama-cpp",
 "gallery_url": "https://index.localai.io/backends",
 "installed_at": "2026-10-05T19:14:46Z",
 "uri": "quay.io/go-skynet/local-ai-backends:latest-gpu-intel-sycl-f16-llama-cpp",
 "digest": "sha256:eb0c909e0e405c6a8111472f829e060955431fa47be97afd25adb178d538cd68"}
```

**Results.**

| §6 | measured | verdict |
|---|---|---|
| 1, base64 | LocalAI v4.11.0 honours `encoding_format: "base64"`: 4,096 bytes = 1,024 f32 | yes |
| 1, f32 through LocalAI | 2,048 texts × (base64, JSON) = 4,096 vectors: 4,096 vector digests match, 0 mismatch, 0 receipt missing; `client.verify` 128/128 batches; no `-0` in the JSON bodies | passed |
| 3, wrapper vs direct, one engine (`vega-bge-m3` vs `vega-bge-m3-direct`: same LocalAI, backend, options, weights) | 1,975/2,048 bit-identical, cosine min 0.99999, max abs diff 7.5e-4. The engine against itself (the base64 pass vs the JSON pass of `vega-bge-m3`): 1,975/2,048, 6.5e-4 | passed: the wrapper changes no byte; the rest is the engine's own run-to-run drift (rage-over-localai §12) |
| 3, vs RAGE's `bge-m3` | 1,975/2,048 bit-identical, cosine min 0.9999956, median 1.0, max abs diff 2.8e-4 | equivalent; the runs differ (below) |
| 4, throughput | direct 29.4 texts/s; behind the wrapper 28.3, with the receipts read 28.2 (−4 %); one receipts read costs 1.5 ms per batch; RAGE 27.6 texts/s = 1,100 tokens/s, `vega-bge-m3` 1,069 (base64) and 1,047 (JSON) tokens/s | passed. The absolute tokens/s is far below §12's 10,700: short texts, one gRPC call each |
| 2, eviction | as configured, LocalAI evicts nothing (below) | **not proved, open** (→ §11) |
| refusals | untagged → 500 `InvalidArgument … without a VeGa tag`; a non-canonical tag → 500 `InvalidArgument … not in canonical form`; neither left a receipt. For every one of 6,145 answers, the receipt was there at the first read after the HTTP answer | passed |

**The two runs** (`GET /vega/runs/sha256:798e5bc8…` vs RAGE's
`sha256:40005906…`) have the same weights (`sha256:daec91ff…`) and device.
They differ in:
- `name`: `localai/intel-sycl-f16-llama-cpp` vs `localai/llama-cpp`;
- `build`: the metadata has no `version`, so it is the `uri`, a moving
  `latest-` tag;
- `image`: `eb0c909e…`, the backend's, vs RAGE's `e64893cf…`;
- `args`: `/backends/intel-sycl-f16-llama-cpp/run.sh` vs the `ld.so` argv;
- `env`'s path;
- `driver`: the LocalAI image's `26.05.37020.3` vs RAGE's `26.18.38308.1`;
- the model path;
- `options`: RAGE's LocalAI build adds `cache_reuse:256`, the app's v4.11.0
  does not.

**Eviction (§6.2).** LocalAI's runtime settings:
- `watchdog_enabled`, `watchdog_idle_enabled` and `watchdog_busy_enabled`
  are false;
- `max_active_backends` is 0;
- `single_backend` is false;
- `size_aware_eviction` is false;
- `vram_budget` is empty;
- `memory_reclaimer_enabled` is false (threshold 0.95);
- `force_eviction_when_busy` is false.

No environment variable sets any of them.

The card has 32,656 MiB. The driver's `used`, step by step:

| state | used | added |
|---|---|---|
| before (RAGE's five processes) | 12,131 MiB | |
| with `vega-bge-m3` | 13,268 MiB | its child: 1,132 MiB |
| with LocalAGI's `qwen3.8-2b-q4`, loaded for the test | 20,299 MiB | 7,031 MiB |
| with `vega-bge-m3-direct` | 21,417 MiB | |

RAGE's processes did not move: 9,419 / 1,104 / 1,082 / 336 / 167 MiB on the
card throughout. LocalAI's `/system` estimates (`size_vram` 1.19 GB and
7.37 GB) are close to the driver's figures. So LocalAI keeps everything
because it has no limit. What it does under pressure was not tried, since
that would need its settings changed. Three of RAGE's processes were already
flagged pushed-out (host > card) before anything here was loaded.

**Found.**
1. **A wrapper defect, fixed.** LocalAI sets no deadline on `LoadModel` or
   `Embedding`. grpcio reports that as 9.22e18 s remaining, and the wrapper
   passed it on as a timeout, which is a deadline already past. Every load
   failed `DEADLINE_EXCEEDED` at once. The tests' fake LocalAI set 30 s
   everywhere and hid it. The fix:
   - `vega_wrapper.remaining()` now forwards None;
   - the fake sends no deadline, as v4.11.0 does;
   - two tests are added.

   `tests/vega` gives 120 passed with grpcio installed; without the fix, 10
   gRPC tests fail.
2. **LocalAI registers backends only at its start or after a gallery
   install** (`core/gallery/backends.go`, `RegisterBackends`). A hand-placed
   directory is listed by `GET /backends`, but loading it gives "backend not
   found" until a restart. The LocalAI container was restarted once (20:08,
   nothing loaded; the same 19 models after).
3. **`digest` is not computed from what was installed.** After the download,
   the gallery asks the registry for the digest of the `uri`'s TAG
   (`lookupImageDigest(uri.OCIReference())`). The download may come from a
   mirror or the `master-` fallback, and the tag may move between the two.
   So `image` names what the tag pointed to at install time. That is
   plausibly the bytes on disk, but not provably. For the §4 amendment.
4. **`driver` names a driver the backend does not load.** The wrapper reads
   the LocalAI image's dpkg (`libze-intel-gpu1 26.05.37020.3`). The SYCL
   backend carries its own Level Zero driver
   (`lib/libze_intel_gpu.so.1.15.39122`), and its `run.sh` points
   `ZE_ENABLE_ALT_DRIVERS` at it. The backend's digest covers that copy, but
   the driver string is still wrong. A decision for the §4 amendment.
5. **LocalAI's `/system` names the wrapper's pid** (36 MB RSS), not the child
   that holds the card. Labelling the card by `/system` pids (rage-over-
   localai §7) has to follow descendants, as `vega_wrapper.family` does.

**Left on the node.** Running: `vega-proof` (127.0.0.1:30439 → 8090, with
its run store inside the container), and `vega-bge-m3`, loaded (1,137 MiB on
the card). `vega-bge-m3-direct` and LocalAGI's model were unloaded after the
test. To remove later:

| path | size |
|---|---|
| `/mnt/tank/apps/localai/backends/vega-llama-cpp/` (package, `python/`, `venv/`, `run/receipts.sock`, `neo-cache/`) | 349 MB |
| `/mnt/ssd5/apps/localai/models/vega-weights/bge-m3-FP16.gguf` | 1,157,671,200 B |
| `/mnt/ssd5/apps/localai/models/vega-bge-m3.yaml`, `vega-bge-m3-direct.yaml` | 813 B, 869 B |
| container `vega-proof`; image `vega-proof:local` | 152 MB |
| image `python@sha256:54c85f3c…` (pulled, untagged) | 126 MB |
| host `/tmp/vega-src/`, `/tmp/vega-src.tgz`, `/tmp/vega-proof-ctx/`, `/tmp/vega-snap.sh`, `/tmp/vega-models-before.txt` | < 1 MB |

To remove them:
1. `POST /backend/shutdown {"model":"vega-bge-m3"}`.
2. Remove the YAMLs, then `POST /models/reload` (B8: LocalAI forgets them at
   its next start).
3. `docker rm -f vega-proof`, then `docker rmi vega-proof:local`.
4. Remove the directories.

## 11. Eviction on the node (2026-10-08)

*§6.2 on the LocalAI app (v4.11.0-gpu-intel), beside the live RAGE, which
was only read. Scripts in `deploy/vega/proof/` (not committed):
- `evict_snap.py` reads the card from the host, as `app/gpu_probe` does: the
  DRM fdinfo of every process, one record per client, plus xe's TTM managers
  in debugfs;
- `evict_load.py` uses one model and polls `/system` and the card every
  100 ms;
- `evict_digests.py` computes item 5's receipts and digests;
- `evict_spill.py` checks for spill.

Settings went through `POST /api/settings`, which persists them in
`runtime_settings.json`. `app.update` was not used.*

**What LocalAI v4.11.0 has** (read in its source at the tag, 58830f7):
- **`max_active_backends`** counts models. It evicts before a load.
- **The memory reclaimer** evicts after the fact. Every `watchdog_interval`
  (500 ms) it compares a usage figure with `memory_reclaimer_threshold`. If
  the figure is above, it evicts one model:
  - not a `pinned` model;
  - not a busy one, unless `force_eviction_when_busy`;
  - the least recently used one, or the largest weight file first with
    `size_aware_eviction` (LRU breaks ties).
- **`vram_budget`** only changes the reclaimer's figures.

On Intel, LocalAI finds the card through clinfo (total 31,023 MiB). It sums
`drm-total-vram*` over the fdinfo of the processes in its own PID namespace.
So it sees only its own models, never RAGE's.

**`vram_budget` caps figures, not allocations.** It caps both the reported
total and the reported free memory at the budget S. In this setup:
- used = max(0, own − (T − S)), where own is LocalAI's own memory and
  T = 31,023 MiB;
- the reclaimer fires when own > T − (1 − t)·S, where t is the threshold.

Two readings with LocalAI holding 1,137 MiB:
- S = 18 GiB reports used 0 of 18,432 MiB;
- S = 30,000 MiB reports used 113 MiB.

So a smaller budget makes eviction later, never sooner. At S = 18 GiB and
t = 0.95, LocalAI would evict only above 30,101 MiB of its own: the card
would be overcommitted first. A budget of 8 GiB or more changes no load
option either: the parallel-slot tier is 4 slots from 8 to 32 GiB. **So the
budget is left empty.** It would mean something only if LocalAI could see
everything on the card.

**The settings chosen**, and the arithmetic:
- Card C = 32,656 MiB. RAGE R = 12,110 MiB: its five processes, unchanged in
  every reading. Margin M = 2,048 MiB, for RAGE loading a small idle model,
  compute-buffer growth, and the driver's 26 MiB not attributed to any
  process.
- At peak LocalAI may hold A = C − R − M = **18,498 MiB**.
- The reclaimer acts after a load has allocated. So the peak is at most the
  trigger X plus the largest model L, here LocalAGI's `qwen3.8-2b-q4` at
  7,032 MiB. That gives X ≤ A − L = 11,466 MiB, so t ≤ 11,466 / 31,023 =
  0.3696.

| setting | before | now | why |
|---|---|---|---|
| `memory_reclaimer_enabled` | false | **true** | the only byte-aware eviction in v4.11.0 |
| `memory_reclaimer_threshold` | 0.95 | **0.36** | X = 11,168 MiB; the bound X + L = 18,200 MiB ≤ A |
| `size_aware_eviction` | false | **true** | one eviction frees the most, and a small utility model (granite, 211 MiB) stays |
| `vram_budget` | "" | "" | caps figures only (above) |
| `max_active_backends` | 0 | 0 | counts models, not bytes; models here are 0.2–7 GB |
| `force_eviction_when_busy` | false | false | never kill a model in the middle of a request |
| `vega-bge-m3.yaml` | — | **`"pinned": true`** | the encoder is never evicted |

The pin was set with `PUT /models/toggle-pinned/vega-bge-m3/pin`. That call
rewrites the YAML, so it was then rewritten by hand with its comments
(`deploy/vega/proof/vega-bge-m3.yaml`). LocalAI was restarted once at the
end. The settings read back the same after the restart, its watchdog started
at boot, and the same 20 models were listed.

**Results.** These are the card readings from the driver (debugfs
`vram0_mm`) at 20 points, plus the lowest free memory polled during each
load.

| § | measured | verdict |
|---|---|---|
| 1, eviction order (t = 0.24, LRU, nothing pinned) | `vega-bge-m3`, `granite`, `filler-a` and `filler-b` were loaded (6,666 MiB). Loading `filler-c` evicted `vega-bge-m3`, `granite` and `filler-a` at +3.55, +4.28 and +4.99 s, in LRU order. That is one per 500 ms tick, while `filler-c` was still loading (its load took 44 s), until own < X. Card free was 11,586 MiB at the lowest | **passed**, with the reclaimer |
| 1, size-aware (t = 0.25/0.36, sizes known) | granite was used first, then `filler-b`, then `filler-c`. Loading `filler-a` evicted `filler-b`: the largest file, the oldest of the equal ones. Granite stayed, although it was older | passed |
| 1, timing | Eviction: the model is gone from `/system` < 0.1 s after the tick, and its card memory is freed at once. Reload: `vega-bge-m3` 7.9–10.0 s (the first batch, load included); a 2B chat model 44–46 s | — |
| 2, `pinned` | `vega-bge-m3` (pid 590775) stayed resident from 20:56 to 21:08, through 7 evictions in which it was the least recently used model | **passed** |
| 3, no overcommit, no spill | The card's peak was 24,879 of 32,656 MiB (lowest free 7,777, while `qwen3.8-2b-q4` loaded). LocalAI's peak was 12,769 MiB, under A. At every reading xe's `gtt_mm` usage equals the sum of all processes' `drm-resident-gtt` + `-system`, to the MiB (10,850–12,124 MiB). No process ever lost `vram0` while gaining host memory: the only drops are −25 MiB in a just-loaded model, with its host memory unchanged. RAGE's five processes were identical in all 20 readings | **passed** for these loads |
| 4, LocalAGI's model | `qwen3.8-2b-q4` loaded at 7,032 MiB with t = 0.36. Its load evicted one filler, and it answered "5" to "What is 2+3?" (22+31 tokens). It was evicted later as LRU, and came back in 46 s | **passed** |
| 5, vectors after eviction/reload | 256 texts, base64, all 8 batches receipt-verified, the same run `sha256:798e5bc8…` throughout. The engine against itself on one load: 246/256 bit-identical, max abs diff 1.7e-4. After an explicit reload: 246/256, 1.5e-4. After the reclaimer's eviction and reload: 246/256, 1.3e-4. After the LocalAI restart: 246/256, 1.2e-4. Cosine ≥ 0.9999989 everywhere | **passed**: within the engine's own variation. `pinned` is not a load option, so the run does not change |

**How spill is read.** xe places a buffer object in VRAM, in TT (`gtt`) or
in system memory. Under pressure it moves VRAM objects to TT without telling
anyone, and fdinfo counts each object where it is now. So host memory is not
spill in itself. A fresh `vega-bge-m3` already holds 564–910 MiB in `gtt`
(SYCL host allocations), and RAGE's processes 114–5,126 MiB, which is why
§10 saw three of them "pushed out" before anything was loaded. Spill is a
process whose `vram0` falls while its `gtt` rises, with no unload. The
card-wide check is debugfs `gtt_mm` usage against the sum of the processes'
host figures.

**Found in LocalAI** (for upstream):
1. **`size_aware_eviction` set at runtime is saved and shown, but not
   applied.** It has no `restartRequired` and no live setter
   (`runtime_settings_registry.go` L208), so it waits for the next watchdog
   restart. It was applied here by posting it together with the threshold.
2. **A watchdog restart forgets model sizes.** `WatchDogState`
   (`watchdog.go` L361) has no `modelSizes`. After any restart-requiring
   settings change, the models already loaded count as size 0, so
   size-aware eviction takes them LAST. Seen: it evicted granite (211 MiB)
   first, because granite's was the only size known. Reloading the models,
   or restarting LocalAI, repairs it. **After any settings change, restart
   LocalAI.**
3. **Models loaded while no watchdog exists are not tracked when one is
   started.** `RestartWatchdog` restores from a nil watchdog. This was read
   in the source, not observed: `vega-bge-m3` was reloaded before the tests.
4. **The reclaimer evicts while a load is still in progress.** The loading
   process grows past X and older models go before it answers. This is
   harmless, but it is why the bound is X + L and not X.

**Still open (§6.2).** LocalAI does not see RAGE. If RAGE grows by more than
M, LocalAI will not react. RAGE does see LocalAI (rage.md §19) and gives up
its own idle models. And there is no check before a load: a single model
bigger than the headroom overcommits the card whatever the settings. Two
models in this LocalAI are that big: `swift-qwen3.8-27b` (18.0 GB of
weights) and `flux.1-krea-dev-ggml` (12.7 GB). None was loaded. Busy models
are never evicted, so a card full of busy models is not protected either.
The fixes are upstream: a fit check before a load, and a usage figure that
counts every process on the card.

**Created, and what is left.**
- `vega-filler-a/-b/-c.yaml` (501 B each) were test models: LocalAGI's
  Qwen3.8-2B-Q8_0 weights, read only, context 8192, `parallel:1`, about
  2,650 MiB on the card each. They were removed before the restart, so
  LocalAI has forgotten them.
- The throwaway containers `vega-evict-dg` (from `vega-proof:local`, `--rm`)
  are gone.
- `granite-embedding-107m-multilingual` and `qwen3.8-2b-q4` were unloaded.
  Loaded now: `vega-bge-m3` only (1,136 MiB), as at the start.
- The `runtime_settings.json` backup was deleted: it holds the keys. To roll
  back, POST `memory_reclaimer_enabled: false`,
  `memory_reclaimer_threshold: 0.95` and `size_aware_eviction: false`, then
  restart LocalAI.
- To remove later: host `/tmp/vega-evict/` (6.8 MB). It holds the scripts,
  `snaps.jsonl` (the 20 readings), the digests in `out/`, and the original
  `vega-bge-m3.yaml.bak` (813 B).

## 12. Staging in the LocalAI app (2026-10-08/09)

*Switch-over step 3, unattended. Every model Louise uses is staged in the
LocalAI app beside the live RAGE (read only); no Louise collection file was
changed. Scripts in `deploy/vega/stage/` (not committed); the packages are
built from commit 89418c9, whose staged files are 4148d4b's bytes.*

**Installed** (`/mnt/tank/apps/localai/backends`, owner 568:0; apparent size):

| backend | what | size |
|---|---|---|
| `vega-llama-cpp` | rebuilt from HEAD (`build_vega_package.sh`), in front of `intel-sycl-f16-llama-cpp`; `run/` kept (the receipts socket, bind-mounted into `vega-proof`) | 365 MB |
| `vega-louise-medcpt-article-ov` | the same wrapper in front of `louise-medcpt-article-ov`; the same socket | 359 MB |
| `louise-medcpt-article-ov`, `louise-medcpt-rerank` | `build_louise_backend.sh`: Dockerfile.localai's `louise-backends` layout, portable CPython 3.12.11, OpenVINO 2026.3.1 | 663 MB each |
| `louise-madlad` | the same, with torch 2.14.0+xpu in its own venv and `lib → venv/lib` | 7.8 GB (3.5 GB on disk) |

Built on the node in throwaway `python:3.12-slim` containers: 6 s per
wrapper, 10–19 s per OpenVINO backend, 156 s for MADLAD. The app's image
has no system torch, so each backend carries its own Python, as LocalAI's
own python backends do. Everything is compiled at build time. Python then
writes nothing into a backend's tree at run time, which a hashed tree
requires. In the app's container, as uid 568, torch sees `xpu` (the B70)
and OpenVINO sees `GPU`.

**The article encoder's IR.** `convert_article.py` wrote
`/mnt/ssd5/apps/medcpt-models/medcpt-article-ov` (435,570,817 B of fp32
weights, 417 MB on disk) in 48–56 s. It used torch 2.14.1+cpu and
transformers 5.15.1 from the HF cache. The check pairs were this staging's
64 pairs. Encodings were identical for all 64 pairs.
- **A defect in its check, fixed:** the first run failed at a worst cosine
  of 0.999950 < 0.99999. OpenVINO's CPU plugin infers in bf16 by default on
  the node's Ryzen 7600 (`avx512_bf16`).
- **The fix:** the IR is now held in f32 (`INFERENCE_PRECISION_HINT`), and
  its worst cosine is 0.9999999. The plugin's default precision is printed
  beside it, not held to the bound. The edit to `convert_article.py` is not
  committed.

**Mounts.** One `app.update localai` (`mounts.py`) added six read-only host
paths without ACLs (uid 568 already reads them through their inherited
NFSv4 entry `user:apps`). They are RAGE's own: `bge-m3-models`,
`louise-rage/models/chat`, `granite-models`, `llama-embed`, `madlad-models`
and `medcpt-models`, mounted at `/models/weights/{bge-m3,chat,granite,
llama-embed,madlad,medcpt}`. So a model path is RAGE's, byte for byte.

Checked against the dataset's 01:00 snapshot, leaf by leaf, the only other
change in the user config is `ix_context.scale_version`, which TrueNAS
rewrites at every update. The app is RUNNING, with the same 20 models plus
the 9 new ones. The reclaimer settings read back unchanged (enabled, 0.36,
size-aware, no budget).

**The YAMLs** (`/mnt/ssd5/apps/localai/models`, sources in
`deploy/vega/stage/models/`). Every load option is copied from RAGE's YAML.
Only three things differ: the backend, the env (`NEO_CACHE_DIR` and
`OV_CACHE_DIR` under `/data/louise-cache`), and `pinned`.
- `vega-bge-m3`: updated to the mounted weights, NOT pinned (wikipedia has
  `embed = "off"`).
- `vega-qwen3-embedding-4b`: **pinned** (gutenberg, `embed = "all"`).
- `vega-granite`: **pinned** (deposit-granite, `embed = "all"`, and the
  node's default encoder; 211 MiB).
- `vega-medcpt-query`, `vega-medcpt-article` (the GGUF of pubmed's current
  space) and `vega-medcpt-article-ov` (the pair encoder, `pairs:true`): not
  pinned (pubmed has `embed = "off"`).
- `louise-rerank-bge` (`intel-sycl-f16-llama-cpp`), `louise-rerank-medcpt`,
  `louise-translate-madlad` and `louise-chat-qwen3-8b`: ordinary models,
  with no VeGa.

The rule: pin an encoder that Louise embeds with continuously.

**Pin through the API, never by editing the YAML.** `PUT
/models/toggle-pinned/<name>/pin` reaches the watchdog at once;
`"pinned": true` written into the YAML plus `POST /models/reload` does
not, until LocalAI restarts. Observed 2026-10-10: `vega-medcpt-article-ov`,
pinned by an edit and a reload at 11:49, was evicted twice at 11:54, once
in the middle of its load, while a translation model filled the card to
80%. Pubmed's pair gate failed with "connection refused". The API call
rewrites the YAML without its comments, so put the commented file back
afterwards (`pinned` reads the same).

**Verified** (`verify.py`, in `vega-proof`). Each model was loaded once,
asked, compared, and shut down. The inputs were 64 standard-library
docstrings (64 pairs for the pair encoder), cut to 1,000 characters for
the 512-token models. Every vector came back with base64 and was checked
against its receipt (64/64 for each encoder).

| model | first call, load included | vs | cosine min / median | bit-identical | max abs diff |
|---|---|---|---|---|---|
| `vega-granite` | 9.2 s | RAGE `granite-embedding-107m-multilingual` | 1.0 / 1.0 | 64/64 | 0 |
| `vega-bge-m3` | 11.9 s | RAGE `bge-m3` | 0.9999971 / 1.0 | 47/64 | 2.8e-4 |
| `vega-medcpt-query` | 6.3 s | RAGE `medcpt-query` | 0.9999405 / 1.0 | 50/64 | 1.4e-3 |
| `vega-medcpt-article` | 6.3 s | RAGE `medcpt-article` | 0.9999999 / 1.0 | 50/64 | 6.6e-5 |
| `vega-medcpt-article-ov` (pairs, GPU) | 7.3 s | transformers fp32 CPU (RAGE has no such model) | 0.9999963 / 0.9999985 | 0/64 | 5.7e-3 |
| `vega-qwen3-embedding-4b` | 33.7 s | RAGE `qwen3-embedding-4b` | 0.9999991 / 0.9999997 | 3/64 | 1.2e-4 |

The other four models:
- `louise-rerank-bge`: the same order as RAGE's on 9 documents, scores
  within 1.05e-3; 8.1 s with the load.
- `louise-rerank-medcpt`: the same order, identical scores; 4.7 s.
- `louise-translate-madlad`: 4 sentences (it, de, fr, ja), identical to
  RAGE's; peak 5,658 MB; 25.0 s with the load.
- `louise-chat-qwen3-8b`: "Rome"; 9.4 s with the load.

During these loads the card stayed at 18.7–19.1 of 32.6 GB used (RAGE's).
At the end nothing is loaded and LocalAI is RUNNING.

**The runs** (`GET /vega/runs/<id>`).
- Every llama.cpp encoder:
  - `name` `localai/intel-sycl-f16-llama-cpp`;
  - `image` (the tree digest) `sha256:f5c07d2bed1d1b4efc633d798fc93fc2e5764e30dd6d2757eadf9f8dbcad4112`;
  - `build` `f5c07d2bed1d` (the metadata has no `version`);
  - `args` `["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"]`;
  - `driver` `kernel 6.18.52-production+truenas; xe; libze_intel_gpu.so.1.15.39122 sha256:58d0b217…`,
    the backend's bundled Level Zero driver (§10, finding 4, now applied).
- `vega-medcpt-article-ov`:
  - `name` `localai/louise-medcpt-article-ov`;
  - `image` `sha256:8065a7fc670d3a43…`, `build` `8065a7fc670d`;
  - `driver` from the app image's dpkg: `libze-intel-gpu1
    26.05.37020.3-1~24.04~ppa3; intel-opencl-icd 26.05.37020.3-1~24.04~ppa3;
    libze1 1.27.0-1~24.04~ppa2`. OpenVINO carries no driver.
- `options` hold no `cache_reuse`. The app's LocalAI does not add it, while
  RAGE's build adds `cache_reuse:256` (§10). It was recorded, not copied
  into the YAMLs.
- `vega-granite` and `vega-medcpt-article-ov` were loaded a second time.
  Each stated the same run, so neither backend's tree moved between loads.

**Not staged.** DAR-legal-it (not in step 3's brief; no live collection
uses it) and DNABERT-2 (genome collections deleted).

**DAR-legal-it, staged 2026-10-12 (operator).** `louise-dar-legal-it`
(`build_louise_backend.sh` from 3ecc108: torch 2.14.0+xpu, transformers
4.55.4, sentence-transformers 5.1.0 in its own venv; 3.6 GB) and the wrapper
`vega-louise-dar-legal-it` (166 MB, the shared receipts socket); model
`vega-dar-legal-it` (not pinned, context 8192, device xpu); weights
`/mnt/ssd5/apps/models/dar-legal-it` (820 MB, revision de7867e4, under the
one weights mount). LocalAI restarted once to register the backend (lesson
2 above); the busy watchdog and the five pins read back the same. First
load 21.7 s; run `sha256:85b9fa1a…`; three texts receipt-verified and equal
to the CPU reference (cosine 1.000000).

**Open: the backend's tree digest moved once with no file changed.** The
first loads (22:25–22:32 CEST) stated run 85b9fa1a… with engine.image
`355a0208…`; from about 23:00 every load states 651c4ec0… with image
`7b5d557e…`, which `tree_digest` recomputes today. VeGa's two run records
differ in `image`/`build` only, and nothing under `louise-dar-legal-it/`
has an mtime or a ctime after 22:24:30 — no write, no chmod, no deletion.
Not explained yet. Found beside it and fixed (24b9faf): the WRAPPER build
left its stdlib uncompiled, so the wrapper's tree took .pyc files at its
first loads — a real defect, but the wrapper is not what `image` hashes.
Since the rebuild two fresh loads state the same run and nothing is written
in either tree. A drift is safe by construction (an unvalidated run's
answers are refused before the write and the gate validates the new run),
so the Normattiva recipe names the engine served now; watch its next loads.
Probed 23:30: none of these reproduces `355a0208…` from today's tree
(44,710 entries) — without any `__pycache__`, without the top-level one,
without the `lib` link, without any one top-level entry. VeGa keeps a run's
digest, not the per-file list it was computed from, so a past drift cannot
be diffed. Today's list is kept for the next one:
`/root/dar-legal-it-tree-7b5d557e.json` on the node (8.7 MB, sha256
59ee8039…). Worth adding: the wrapper writing that list beside each new run
it states, so a drift names its byte the day it happens.

**Left on the node, ours.**
- Host `/tmp/vega-stage/` (99 MB, tmpfs): sources, the portable Python,
  `out/` with the reports, `pairs.json` and the reference vectors. The pip
  cache (2.6 GB) was removed.
- `/tmp/vega-stage-src.tgz`; `vega-proof:/tmp/stage/`.
- `/mnt/tank/apps/localai/data/louise-cache/` (neo 9 MB, ov 391 MB).
- The six mount points under `/mnt/ssd5/apps/localai/models/weights/`.
- Now unused: the proof's `vega-weights/bge-m3-FP16.gguf` (1.16 GB; still
  named by `vega-bge-m3-direct.yaml`) and `vega-llama-cpp/neo-cache/`.

## 13. The switch-over (2026-10-09)

Done by hand, step by step, after the operator's "Proceed with 1".

- **Receipts service, permanent.** A `vega` service in the `louise-rage`
  app (the app keeps its name until after the migration, operator):
  image `vega:bb9bf61` (the Louise image of that commit, tagged), socket
  `/mnt/tank/apps/localai/backends/vega-llama-cpp/run`, state
  `/mnt/ssd5/apps/louise-rage/state/vega`, port 30439. `vega-proof` removed.
- **Louise env:** `VEGA_URL=http://192.168.178.10:30439`;
  `CHAT_URL`/`TRANSLATE_URL` = LocalAI with `CHAT_MODEL=louise-chat-qwen3-8b`,
  `TRANSLATE_MODEL=louise-translate-madlad` (new key, 36ae15e); `RAGE_URL`
  removed. `[deposit]` in platform.toml: LocalAI, `api = "vega"`,
  `vega-granite` (3c27212); `platform.toml.pre-vega` kept.
- **Collections** (`deploy/vega/switch/switch_collection.py`; previous files
  in `config/collections.pre-vega/`): deposit-granite and the two granite
  minis → `vega-granite`; gutenberg-mini-certified, gutenberg-744ba8430fed,
  gutenberg → `vega-qwen3-embedding-4b`; wikipedia → `vega-bge-m3`; pubmed →
  `vega-medcpt-article` / `vega-medcpt-query` (its current GGUF space);
  rerankers → `louise-rerank-bge` / `louise-rerank-medcpt`. louise-stop (a
  test collection) deleted, operator.
- **Checks:** the granite run gated (16/16, median 1.000000); gutenberg's
  backend gate on the VeGa endpoint: "baseline reproduced"; searches with
  vector hits through receipts on gutenberg-744ba8430fed and pubmed (pubmed
  reranked by MedCPT on LocalAI); wikipedia answers exactly as it did under
  RAGE (text hits only: its vector leg and reranker exceed their budgets —
  an older problem, open); chat "Rome", translation correct.
- **Second peer** (dev PC): new code, its four files migrated and switched,
  env to LocalAI/VeGa; a search answers with vector hits (238 ms dense).
- **RAGE retired:** the `rage` service removed from `louise-rage`
  (definition kept in `/mnt/ssd5/apps/louise-rage/rage-service-retired-2026-10-09.json`,
  mode 600); nothing points at :30437.
- **Reclaimer:** with RAGE gone LocalAI holds everything on the card (9,521
  MiB used = vega-qwen3 9,363 + vega-granite 134). C = 32,656, margin 2,048,
  largest model ≈ 9,500 (vega-qwen3) → trigger X ≤ 21,108 MiB → threshold
  **0.68** of the 31,023 MiB LocalAI sees; LocalAI restarted (§11 lesson).

**Incidents, all fixed before going on:**
1. The switch script first wrote the encoder `url` WITH `/v1/embeddings`,
   which the client appends: LocalAI answered 404 and the worker wrote
   `embed: RuntimeError: embed server HTTP 404` on **4,295 gutenberg
   documents** (reverted within minutes). Fixed: the url is the server;
   and an endpoint answer that is not about the input never writes a
   document error any more — wrong endpoint pauses with its reason, busy
   defers (295eb1e). The 4,295 errors wait for the operator's "clear errors
   & retry".
2. The script read a `[[sources]]` table after `[encoder]` as part of it
   (louise-stop's source url rewritten); fixed in it and in
   align_model_names.py (3078c83).
3. The backend gate's 30-minute pass was keyed on the collection name, so
   RAGE's pass covered VeGa's vectors until it expired; keyed on the
   endpoint now (89b8ebf).
4. New deposit collections and `hold collection create` named the model
   from the encoder LABEL when the URL named none; both now take explicit
   `api`/`embed_model`/`query_model` and refuse otherwise (3c27212, d7a4997).
