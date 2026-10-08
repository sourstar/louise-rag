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
`localai/<metadata name>`, `build` the metadata's `version` (else `uri`),
`variant` null, **`image` the backend's OCI digest from `metadata.json` —
PENDING the 09 §9.3 amendment (§4)**; no digest, the wrapper refuses to
start. `args` `[<real run.sh, absolute>, "--addr", "127.0.0.1:${PORT}"]`;
`env` the load's `EnvVars`; `options` RAGE's canonical form of the
ModelOptions received — a field the wrapper's `backend.proto` cannot name
refuses the load; `device` read from the child's DRM clients (`vega.json`
`cards`); `driver` as RAGE, the runtime from the LocalAI image's dpkg status.

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
"vega-…"`; where receipts are read is node identity, `VEGA_URL` (07 §7.8,
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
| 2, eviction | as configured, LocalAI evicts nothing (below) | **not proved, open** |
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
