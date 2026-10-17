# LocalAI backends: one per engine (2026-10-17)

*Operator, 2026-10-17: at most one LocalAI backend per engine — llama-cpp,
OpenVINO, PyTorch, with 16/32-bit variants and nothing more. Upstream first;
a model that needs an upstream fix stays on OUR backend, ideally one backend
for all of them, ideally upstream's plus our patches.*

## Measured on the node (B70, LocalAI v4.11.0)

Every probe ran in a throwaway container from `local-ai:v4.11.0-gpu-intel` on
the card, as uid 568 with bytecode writing off, the backends mounted
read-only (no installed tree changed). Scripts and outputs:
`/mnt/tank/apps/localai-probe/` (5.5 GB, mostly the probe venv).

| model | stock upstream | combined venv (below) vs today's backend |
|---|---|---|
| DAR-legal-it | **fails.** `intel-transformers` (transformers 5.18.0, sentence-transformers 6.1.0) as upstream loads it: `please install xformers`. With sdpa and the two GTE switches passed: `GteModel has no attribute get_extended_attention_mask` — its remote code does not run on transformers 5. | cosine ≥ 0.9999999999998 on 4 texts (to 4,000 tokens), max abs diff 6.9e-8; the longest bit-identical |
| MADLAD | its generic Seq2Seq path calls `Predict` once per prompt: no batch | transformers 4.46.3 → 4.55.4: 41 of 42 translations byte-identical; the one that differs (it→ar) was `1-1-1-…` before and Arabic after. 4.05 s → 3.18 s |
| medcpt-article | no text-pair input (segment ids) | tokenizers 0.22.2 → 0.21.4: token ids of 2,000 real PubMed pairs (pubmed26n0837) identical; 64 vectors on OpenVINO GPU bit-identical |
| medcpt-rerank | its IR is not something `rerankers` loads | same code and tokenizer library as the article encoder |

**The combined venv:** torch 2.14.0+xpu, transformers 4.55.4,
sentence-transformers 5.1.0, openvino 2026.3.1, tokenizers 0.21.4, einops,
accelerate, sentencepiece. `pip check`: no broken requirements.

## Consequence

All four need what upstream lacks, so all four go on ONE backend of ours,
with that one venv. Upstream's `transformers` backend is not a useful base:
none of the four runs through its code paths, and its transformers 5 is the
one DAR-legal-it cannot load on. The base is what the four already share,
`deploy/localai/backends/louise_backend.py`; the model's YAML names its task.

The backend's tree replaces four engines with one, so each pinned model's
run is new: DAR-legal-it (normattiva) and medcpt-article-ov (pubmed) pass the
calibration gate again. By the figures above, no re-embedding (RULE 1).

## What the stock backends would need (for upstream)

What `intel-transformers` / `intel-rerankers` (v4.11.0's, as installed on the
node) would need to serve these four, and whether each change could move a
model they already serve. "Gated" = behind a new option; a model whose YAML
does not set it loads as today.

| # | patch | for | other models |
|---|---|---|---|
| U1 | `SentenceTransformer`: pass `device` (XPU/CUDA from the load call), `model_kwargs` (`attn_implementation`), `config_kwargs`, and `max_seq_length` from `ContextSize` | DAR-legal-it | none if gated. **Ungated, `max_seq_length` changes vectors** of every ST model whose texts exceed its card's length |
| U2 | DAR-legal-it's `GteModel` code calls `get_extended_attention_mask`, gone in transformers 5 | DAR-legal-it | NOT a backend patch: the code is the model's (`trust_remote_code`, inside its weights digest). Upstream would need transformers 4.x for it, which breaks every model that needs 5. **This alone keeps DAR-legal-it on a backend of ours** |
| U3 | `Embedding` prints every text in full to stderr ("Calculated embeddings for: …") | all | none; a whole article per line in LocalAI's log |
| U4 | OpenVINO: optimum-intel 1.15.0 imports `openvino.runtime`, which openvino 2026.4.1 removed | every OV model | **the stock OV path is broken for every model** (checked: `ModuleNotFoundError`); the fix (optimum-intel ≥ 2.x, as `intel-rerankers` has) moves every OV model to a new optimum |
| U5 | OpenVINO: `if "CPU" or "NPU" in device_map` is always true (localai-bugs B1), so the GPU-only settings never apply | every OV model on GPU | **yes**: fixed, every OV model on a GPU gets the GPU settings, and its numbers can move |
| U6 | OpenVINO: `export=True` at every load (B2), so an IR on disk is re-exported and the compile cache never hits | every OV model | load time only; the result is the same graph |
| U7 | OpenVINO feature extraction: a text pair (segment ids) and [CLS] pooling | medcpt-article | none if gated (`pooling:cls`); the pair needs a field LocalAI's API does not have, so our marker (`app/rage/pairs.py`), which upstream is unlikely to take |
| U8 | Seq2Seq `Predict`: a batch in one prompt | MADLAD | none if gated; but the format is Louise's (app/translate.py), not LocalAI's |
| U9 | `rerankers`: pass the device (none is passed) | medcpt-rerank (on its HF weights, not our IR) | **yes**: every reranker moves to the GPU when one is present — memory and placement for all of them |

U3, U4 and U6 are plain upstream fixes worth filing (localai-bugs.md). U1,
U7, U8 are gated features. U2 is not fixable in a backend; U5 and U9 move
models other people run.

## Importing it in LocalAI's GUI

`deploy/localai/backends/louise-python/Dockerfile` builds the backend as an
image whose root is the backend directory (run.sh first), as LocalAI's own
backend images are. LocalAI installs it, under the name the install gives,
from:
- **Backends → From URL** with the image's registry name, or
  `ocifile:///<path>` to a `docker save` tar its container can read
  (`pkg/downloader`: `ocifile://` is extracted as an OCI image);
- **the catalog**, once `LOCALAI_BACKEND_GALLERIES` names
  `deploy/localai/gallery/index.yaml` (a meta entry picking the variant by
  GPU, as upstream's).
No venv: the packages are in the portable CPython's prefix, so the backend
does not depend on the directory it is unpacked into (a venv's pyvenv.cfg
does; upstream rewrites it at every start, which a hashed tree cannot have).

Open: the AMD peer (WSL2, next week) needs this backend's ROCm build; OpenVINO
has no AMD GPU, so MedCPT runs on its CPU there.
