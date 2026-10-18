# Upstream reports, ready to file (2026-10-18)

One file per recipient, each section one issue. The operator files them.
Nothing here has been filed yet.

**Checked against each tracker on 2026-10-18; none is already known.** The
rule (operator): skip an issue upstream already knows, unless its cause is
unknown there and we have the fix. Searched intel/torch-xpu-ops,
pytorch/pytorch, wmde/WikidataTextifier, philippesaade-wmde and
exowanderer/WikidataTextEmbedding, both Hugging Face datasets' discussions,
ggml-org/llama.cpp and mudler/LocalAI. The near hits, and why they differ:
- LocalAI #12480: a backend that dies on its own stays listed. B3 is a live
  backend left untracked after a failed eviction.
- LocalAI #8562: llama.cpp's auto-fit and `gpu_layers`, not B11.
- Issues that quote B10's or B13's log lines inside other bugs.
- llama.cpp #27771 and #27769: other SYCL wrong answers (bf16 matmul, flash
  attention), on Xe-LPG.

| file | to | issues |
|---|---|---|
| [intel-torch-xpu-ops.md](intel-torch-xpu-ops.md) | intel/torch-xpu-ops | fp32 `torch.softmax` wrong for row lengths 513–768, … (B70, torch 2.14.0/2.14.1+xpu) |
| [wmde-wikidata-textifier.md](wmde-wikidata-textifier.md) | wmde/WikidataTextifier | random alias order; formatter failures silently drop values |
| [wmde-wikidata-embedding-project.md](wmde-wikidata-embedding-project.md) | philippesaade-wmde (WikidataTextEmbedding and the HF datasets) | stale vectors kept as current (~14 %); text depends on a live wiki page; the corpus drops entity data; the card's counts |
| [ggml-llama-cpp-sycl.md](ggml-llama-cpp-sycl.md) | ggml-org/llama.cpp | SYCL embeddings non-reproducible through oneDNN's GEMM |
| [mudler-localai.md](mudler-localai.md) | mudler/LocalAI | B1–B14 of localai-bugs.md, condensed |

Not included, because there is no reproducer yet:
- jina-v3's batched `encode` failing in `torch.unique` on the B70; a plain
  `torch.unique` reproduces nothing.
- llama-server corrupting long texts when one request carries several
  (STATUS, `EMBED_BATCH=1`, 2026-08).
