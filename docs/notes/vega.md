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

Not deployed; nothing on the node. Not wired into `app/embeddings.py` yet.

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

**Open.**
- The package build (stubs from the release's `backend.proto`, LocalAI's
  `grpc_auth.py`/`parent_watch.py`, the `app/` files, the venv) and its
  install beside LocalAI's backends; `metadata.json`'s `digest` as the
  gallery writes it, read on the node (§6).
- Mounting `service.router` and an `Intake` in the louise-rage app.
- Wiring the client: verify the raw body before normalising, NaN first.
- `args` names the program under LocalAI's backends path; relative to the
  backend image's root is the alternative — for the §4 amendment.
