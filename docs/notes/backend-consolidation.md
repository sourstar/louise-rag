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

Open: the AMD peer (WSL2, next week) needs this backend's ROCm build; OpenVINO
has no AMD GPU, so MedCPT runs on its CPU there.
