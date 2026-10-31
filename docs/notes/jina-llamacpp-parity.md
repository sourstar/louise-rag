# jina-v3 on llama.cpp against the torch reference (2026-10-31)

The operator asked for "a proposal to match in llama.cpp the pytorch case
100%", for any future jina-style encoder (one base model, one LoRA per task,
chosen per call). wikidata no longer needs it: it moved to bge-m3
(wikidata-family-slices.md §10). This note keeps the measurement and the
plan, so that the next such case starts from them.

**What "100 %" can mean.** Every choice can match torch: the tokens, the
weights with the task's adapter and its scale, the instruction, the pooling
and the Matryoshka cut. The last bits of rounding cannot match, because two
programs add in different orders. That is why 03 §3.1 keeps a tolerance
between engines.

## 1. What the torch reference does

Source: `jinaai/xlm-roberta-flash-implementation`, `modeling_lora.py`, read
2026-10-31. The weights are stored in bf16 (`config.json`
`torch_dtype`). transformers 4.55 loads them as fp32, as the
`louise-python` jina-v3 task does: `deploy/jina-embeddings-v3/server.py`
sets no dtype.

1. `encode(task=…)` prepends the task's instruction to the TEXT
   (`config.task_instructions`, e.g. "Represent the document for
   retrieval: "), then tokenizes instruction and text as one string.
2. In every Linear and Embedding layer, the forward pass forms
   W' = W + (α/r)·B·A for the task (α = 1, r = 4: scale 0.25), in fp32,
   then multiplies. The adapter is merged at run time; nothing on disk is
   merged.
3. Mean pooling over all tokens, the specials and the instruction
   included. Then the first 512 dimensions (`truncate_dim`), renormalised
   by the task.

In this route the adapter is an engine option (`adapter:retrieval.passage`),
outside `space_id`. No collection may be created on it until the adapter is
part of the weights digest (wikidata-family-slices.md §10).

## 2. What was measured (München, 2026-10-31, /root/measure/wd-ab-score-3way.json)

The arm: llama.cpp c0fd786, f16 GGUF, each task's LoRA merged by
`llama-export-lora` (the tensors equal second-state's f16). The instruction
and the cut to 512 were done by the tool. llama.cpp used its own tokenizer.
21,348 wikidata chunks and 400 queries.

| | jina-v3 torch | jina-v3 llama.cpp f16 |
|---|---|---|
| MRR@10 | 0.510 | 0.509 |
| cosine to torch, chunk by chunk | — | median 0.999987, p01 0.99995, worst 0.99944 |
| chunks/s (B70, contended) | ~5.3 | 26.1 |
| bit-exact to itself | yes (`LOUISE_TORCH_DETERMINISTIC=1`) | 4 of 4 takes (3 fresh loads × 2 orders, both sides; FA on, `GGML_SYCL_FA_ONEDNN=0`) |

The f16 rounding is where it was estimated (1 − cos ≈ 1e-5). Search quality
is the same, and the run is five times faster.

## 3. What a production jina-style space on llama.cpp needs

| step | today (the A/B's arm) | needed | identity |
|---|---|---|---|
| adapter and scale | merged into the GGUF | as is; or base + adapter file (09 §9.3, #140) | weights digest |
| instruction | added by the measuring tool | the VeGa wrapper adds it to the text before tokenization, read from a key written into the merged GGUF | inside the weights file |
| tokens | llama.cpp's copy of jina's Unigram tokenizer (vocabulary, scores and charsmap converted from `tokenizer.json`) | first COUNT: the A/B and calibration texts tokenized both ways. 0 differences: keep it. Otherwise jina's ids through P2's token-ids path, or an upstream fix | `tokenizer.json` as a second file if the wrapper tokenizes |
| Matryoshka cut | done by the measuring tool | the wrapper keeps the first `dim` and renormalises, BEFORE the receipt | `dim` in the space |
| activation | to check: llama.cpp's graph may use tanh-GELU where torch uses exact (erf) GELU | patch to `ggml_gelu_erf` if so, and upstream | engine |
| precision | f16 | f16 is enough by §2. f32 (weights equal torch's W', expected 1 − cos < 1e-7) costs twice the memory (~2.3 GB a side) | weights digest |

Still to explain: the worst chunk at 0.99944. The first suspects are the
tokenizer count (whitespace runs: jina's `tokenizer.json` splits at any
whitespace) and a chunk long enough for the rounding to accumulate.

Build cost when a case arrives: about one day for the wrapper (instruction,
cut, tests), plus the token count and the worst-chunk check (minutes), plus
the GELU check (half a day if a patch is needed).
