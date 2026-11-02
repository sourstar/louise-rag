# Nemotron-3-Embed-1B on llama.cpp, f16: the harrier exercise (2026-11-02)

The operator: "Do the same exercise of harrier with Nemotron 1b embedder, I
want it on llama.cpp with 16bit precision, in the worse case is a
validation test for the future."

**Status: planned, measure only.** The tools are merged. The steps below run
on the node. The operator decides on the rows; nothing follows from them on
its own (the operator, 2026-11-02: "No automatic decision, I decide what to
do"). The harrier exercise it repeats is gutenberg-encoder-bakeoff.md.

## The model, as its card states it (not measured here)

`nvidia/Nemotron-3-Embed-1B-BF16`, released 2026-07-16.

- **Licence:** OpenMDW-1.1 (permissive, commercial use), Apache-2.0 for
  the Ministral parts.
- **Architecture:** Ministral-3-3B pruned twice and distilled from
  Nemotron-3-Embed-8B. 1.14 B parameters, 16 layers, hidden size 2,048.
  `model_type` is `ministral3`.
- **How it embeds:**
  - bidirectional attention (`is_causal: false`);
  - mean pooling over every token, the prompt included
    (`1_Pooling/config.json`: `include_prompt: true`);
  - L2-normalised, 2,048 dimensions;
  - a Matryoshka cut to 1,024 or 512 works if the slice is renormalised.
- **Prompts:** `query: ` before a question, `passage: ` before a document
  (`config_sentence_transformers.json`).
- **Context:** 32,768 tokens. NVIDIA evaluated at 4,096; our window is 8,191.
- **Published scores (NDCG@10):**
  - RTEB 72.38 and MMTEB retrieval 71.04;
  - against llama-nemotron-embed-vl-1b-v2's 61.98 and 59.71;
  - none of it measured on our sets.
- **Size:**
  - 2.28 GB of bf16 weights, about the same as an f16 GGUF;
  - a vector of 2,048 floats: 2× bge-m3's and harrier's 1,024, 0.8× Qwen3-4B's 2,560.

## llama.cpp: what supports it, and on which engine

- **Upstream support is 52ffa02**, "model : support Nemotron-3(.5)-Embed"
  (#30877, 2026-10-20). For the TEXT model it is converter-only
  (`conversion/mistral3.py`):
  - the `Ministral3Model` class writes `mistral3.attention.causal = false`;
  - it writes the pooling type from the sentence-transformers config;
  - it writes `add_bos`/`add_eos` false.

  Its C++ changes are in `tools/mtmd`, for 3.5's images. f9599d0
  (10-23) fixes 3.5's pre-tokenizer only.
- **The node's engine** (`vega-llama-cpp-p5`) is LocalAI v4.12.1's llama.cpp,
  c0fd786 (2026-10-17), plus P1–P5. It already has the `mistral3`
  architecture and non-causal attention for embeddings, so it predates only
  the converter.
- **So the plan:**
  1. convert with upstream's converter at a pinned commit ≥ f9599d0 (3f8af15,
     read 2026-11-02);
  2. run the file on the engine the node has;
  3. let the conversion check (step 5) say whether c0fd786's runtime gives
     the model's vectors.

  If it does not, the "validation for the future" case: a new engine from
  upstream ≥ 52ffa02 with P5 carried, gated as any engine.
- **Not the GGUFs on the Hub.** `zenmagnets/…-Q4_K_M-GGUF` was made with
  b10015 in July, before upstream's support, and is 4-bit. Ours is
  converted from NVIDIA's safetensors to f16, as harrier's was.

## The steps (München, on the node)

1. **Weights.** `nvidia/Nemotron-3-Embed-1B-BF16` at a pinned revision. Record
   the revision and the sha256 of `model.safetensors`.
2. **The cast to f16.** `python -m tools.measure.f16_range <snapshot>/model.safetensors`.
   - `overflow` must be 0. A bf16 value past 65,504 becomes inf in f16, and
     the converter says nothing.
   - `to_zero` (values under 2^-24) is reported and read.
3. **Convert.** From upstream llama.cpp at 3f8af15 (its `gguf-py` beside it):
   `python convert_hf_to_gguf.py <snapshot> --outtype f16 --outfile
   weights/llama-embed/nemotron-3-embed-1b-f16.gguf`. Record the sha256.
   Then read the metadata (`gguf-dump`):
   - `mistral3.attention.causal = false`;
   - `mistral3.pooling_type = 1` (mean);
   - `tokenizer.ggml.add_bos_token = false`;
   - `mistral3.embedding_length = 2048`;
   - `mistral3.block_count = 16`.

   A causal file passes everything else and is another model.
4. **Stage** `deploy/vega/stage/models/vega-nemotron-3-embed-1b.yaml`. It is
   harrier's file: the same engine, flags and env (invariant 12's
   `GGML_SYCL_ENABLE_DNN=0`, `GGML_SYCL_FA_ONEDNN=0`), FA on, `parallel:1`,
   ubatch 8,192. The differences are `pooling:mean` and the weights.
   Non-causal attention needs the whole text in one ubatch, which 8,192 covers.
5. **The conversion check** (`tools/measure/st_parity.py`). Before any row
   counts:
   - `pick` 32 W1 chunks spread over their lengths and 16 questions, en and it;
   - `reference` in a THROWAWAY venv: torch CPU,
     `transformers==5.5.0`, `sentence-transformers==5.4.1`, the card's versions.
     fp32 on the CPU, the card's prompts. Never the app image (invariant 7);
   - `compare --encoder nemotron`: the GGUF through VeGa and the production
     client, exactly as the rows embed it.

   **PASS: every text ≥ 0.99 on both sides** (harrier's bar). A text under
   0.999 is read before the rows count: a BOS the tokenizer does not add
   stays above 0.99 under mean pooling.
6. **Invariant 12.** `python -m tools.vega_repro_check --url <LocalAI> --model
   vega-nemotron-3-embed-1b --window 8191`: fresh loads, shuffled passes,
   the sweep.
7. **The rows**, on the sets harrier ran:

   | collection | the set | commands |
   |---|---|---|
   | wikidata | W1 (21,348 chunks, 400 questions) | `wikidata_encoder_ab embed OUT --encoder nemotron`, then `--encoder nemotron-m1024` (the 1,024 cut, no second embedding), then `score` |
   | wikipedia | P3 (18,783 chunks, 400 questions) | the same, on P3's directory |
   | gutenberg | the pool (490 queries judged by work) | `encoder_bakeoff embed --name nemotron --url … --model vega-nemotron-3-embed-1b --api vega --query-prefix "query: " --doc-prefix "passage: "`, then its score and report |

   `nemotron-m1024` gives the row at bge-m3's and harrier's vector size, so
   disk is compared like for like.
8. **The whole search.** The rerank step on each set, with nemotron added to
   `--encoders` (`bge,harrier,nemotron,nemotron-m1024` on wikidata and
   wikipedia). The reranker's cached pairs are reused, so only new
   candidates are scored.
9. **Speed.**
   - chunks/s from each row's timing;
   - the production-shape arms (VeGa, batch 16, 1/2/4 requests in
     flight), as HARRIER-OPT, only where its rows make it a candidate.

**Order on the node:**
- steps 1–3 and the reference (step 5's second part) are CPU and disk, any time;
- the card steps (5's compare, 6–9) run after tonight's pipeline steps, as
  harrier's did: acquire-only, embedding suspended;
- disk: ~2.3 GB snapshot, ~2.3 GB GGUF, ~1 GB venv.

## What the operator gets

Per collection:
- MRR@10, recall@1 and @10, each with the paired test against the
  incumbent and harrier;
- the whole-search rows;
- vector size and chunks/s.

The rule harrier was held to (gutenberg-encoder-bakeoff.md, "The verdict
rule") is reported beside the rows. The decision is the operator's.
