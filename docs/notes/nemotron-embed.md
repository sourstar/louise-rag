# Nemotron-3-Embed-1B on llama.cpp, f16: the harrier exercise (2026-11-02)

The operator: "Do the same exercise of harrier with Nemotron 1b embedder, I
want it on llama.cpp with 16bit precision, in the worse case is a
validation test for the future."

**Status: steps 1–3 and the reference done (below); the card steps
next.** The tools are merged. The steps below run
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
   - FIRST `tokens` over every text of the sets: the engine's token ids
     against the snapshot's tokenizer.json. Each text that differs is added
     to the texts below (added 2026-11-02, after the 48-text sample missed
     71 that differed);
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

## Done (München, 2026-11-02, on the dev PC)

Steps 1–3 and the reference half of step 5 ran in one python:3.12-slim
container. The record is in nemotron-provenance.json and f16_range.json
under /data/bakeoff/nemotron/.

- **Weights:**
  - revision c0c9fea93ea424587517f2c59e20db9f1d6bf615;
  - model.safetensors 2,281,852,472 bytes, sha256
    f959c3b04e66b42de280bfb97c140cb7e0bfe25e3ecb0b4464c68a8436b2d04f;
  - every LFS file checked against the Hub's sha256.
- **The cast to f16:**
  - 146 tensors, 1,140,918,272 values, max |x| 3.72;
  - **overflow 0**, non-finite 0;
  - to_zero 4,055 (0.0004 %), subnormal 4,093,908 (0.36 %).
- **The GGUF:**
  - upstream llama.cpp 3f8af15a76ac8675812e856da45454bafc90f12c,
    `--outtype f16`;
  - 2,289,841,280 bytes, sha256
    09596cedbdf2a0d164b37083794632be55dcb2aa2abcf02fb995dfdccbecbf2c.
- **Its metadata:**
  - `mistral3.attention.causal = false`, `pooling_type = 1` (mean);
  - `add_bos_token = false`, `add_eos_token = false`;
  - 2,048 dims, 16 blocks;
  - YaRN (original context 16,384, trained context 262,144);
  - pre-tokenizer `pixtral`.

  As step 3 asks.
- **The reference:** 48 texts (32 W1 chunks, 8 en and 8 it questions), the
  card's prompts, fp32 on the CPU. The venv:
  - torch 2.8.0+cpu, transformers 5.5.0, sentence-transformers 5.4.1;
  - tokenizers 0.22.2, safetensors 0.8.0, numpy 2.3.5.

  transformers warned of the unknown `apply_yarn_scaling` key, which the
  card says to expect. If the two engines' YaRN differ, the long texts will
  show it first.
- **Next:** 5b on the card, then 6–8, after the night's pipeline steps.

## Language coverage (München, 2026-11-02 16:31, a read-only count)

NVIDIA evaluated the model in 34 languages: English, Arabic, Assamese,
Bengali, Bulgarian, Chinese, Danish, Dutch, Finnish, French, German, Hindi,
Hinglish, Indonesian, Italian, Japanese, Korean, Malay, Marathi, Nepali,
Norwegian, Persian, Portuguese, Romanian, Russian, Spanish, Swahili,
Swedish, Tamil, Telugu, Thai, Ukrainian, Urdu, Vietnamese.

- **wikipedia:** 44,266,030 documents selected for embedding.
  - Outside the 34 is Turkish only: 546,237 documents (1.23 %), 0.6 % of the
    selected tokens.
- **gutenberg:** 79,760 books.
  - Outside the 34: 1,474 books (1.85 %), 1.33 % of the tokens.
  - Mostly Hungarian 654, Greek 216, Esperanto 124, Latin 108 and Catalan 97.
- **wikidata:** English texts, nothing outside.

Outside the 34 the quality is unknown, not absent: the base model is
multilingual. A Turkish known-item set would measure it if it ever
matters.

## Wikidata W1, vector only (München, 2026-11-02 16:05–16:20)

The operator narrowed the test: "a simple Nemotron test, without reranking
… if it saturates the GPU, the throughput and if it fits a simple benchmark
comparison like @10 @40 @50 @100", and "No dedicated translation model".
So the questions are embedded as typed, with no translation and no
reranker. The conversion check had passed first: 48 texts, worst cosine
0.999999 (documents) and 1.0 (queries), on the node's c0fd786 engine.

The set: 400 questions over 21,348 chunks of 19,998 entities.

| encoder | MRR@10 | recall@1 | @10 | @40 | @50 | @100 |
|---|---|---|---|---|---|---|
| bge-m3 F16 (wikidata's) | 0.6496 | 0.575 | 0.795 | 0.900 | 0.9175 | 0.960 |
| harrier-0.6b f16 | 0.7951 | 0.7525 | 0.880 | 0.9425 | 0.960 | 0.9825 |
| **nemotron f16** | **0.8184** | **0.7625** | **0.920** | **0.9625** | **0.970** | **0.9875** |
| nemotron, cut to 1,024 | 0.8098 | 0.750 | 0.920 | 0.9625 | 0.9675 | 0.985 |

- **By question language (MRR@10):**
  - en: bge 0.636, harrier 0.777, nemotron 0.776, the cut 0.777;
  - it: bge 0.663, harrier 0.813, **nemotron 0.861**, the cut 0.843.
- **Paired, MRR@10:**
  - nemotron − bge-m3 = +0.169, p = 0.0001 (127 questions better, 20 worse);
  - **nemotron − harrier = +0.023, p = 0.074** (53 better, 34 worse): not
    significant;
  - the cut − harrier = +0.015, p = 0.246;
  - full − cut = +0.009, p = 0.165: the cut to 1,024 costs nothing
    measurable.
- **Speed:** 24.1 chunks/s at batch 16, one request in flight (wikidata's
  recipe), with the card 59 % busy. At the same shape harrier did 43.3 and
  bge-m3 57.7 (HARRIER-OPT), so nemotron runs at 0.56× harrier and 0.42×
  bge-m3. One request in flight does not fill the card.

### By question language (200 questions each)

| encoder | en MRR@10 | en @10 | en @100 | it MRR@10 | it @10 | it @100 |
|---|---|---|---|---|---|---|
| bge-m3 | 0.636 | 0.790 | 0.975 | 0.663 | 0.800 | 0.945 |
| harrier | 0.777 | 0.870 | 0.990 | 0.813 | 0.890 | 0.975 |
| nemotron | 0.776 | 0.890 | 0.985 | **0.861** | **0.950** | 0.990 |
| nemotron, cut to 1,024 | 0.777 | 0.890 | 0.985 | 0.843 | 0.950 | 0.985 |

- **English:**
  - nemotron and harrier tie (−0.001, p = 0.96);
  - both beat bge-m3 by 0.14 (p = 0.0001).
- **Italian:**
  - nemotron beats harrier by **0.047, p = 0.020** (better on 32 questions,
    worse on 11);
  - the cut keeps +0.029 over harrier (p = 0.12);
  - the cut costs 0.018 against full nemotron (p = 0.028).
- **Where the lead comes from:** nemotron's whole lead over harrier on W1 is
  Italian questions about English texts, the cross-language case.

### Speed with requests in flight (W1's first 5,000 chunks, batch 16, VeGa, the card to itself)

| in flight | nemotron chunks/s | card busy | harrier | bge-m3 |
|---|---|---|---|---|
| 1 | 26.74 | 56.3 % | 43.29 | 57.71 |
| 2 | 34.25 | 76.7 % | 50.28 | — |
| 4 | 39.50 | 85.2 % | 51.57 | 72.49 |
| 8 | 39.27 | 85.1 % | — | — |

- **Every arm is exact:** 5,000 of 5,000 vectors bit-identical to W1's.
- **At 4 in flight:** nemotron runs at 0.77× harrier and 0.54× bge-m3.
- **CORRECTED: the gap to harrier is mostly not nemotron's** (the
  operator's choice B, München 17:04–17:10).
  - **The cause:** every nemotron speed row above ran beside gutenberg's
    CPU reference (the CPU twin, ~700 % CPU inside LocalAI, on 12 cores).
    harrier's and bge-m3's HARRIER-OPT rows ran while it was paused. The
    host is CPU-saturated: the backend's main thread waits to be scheduled
    between submissions, and the card idles meanwhile.
  - **Profiled pair, both beside the twin** (c4, the same 5,000 chunks):
    harrier 36.38 chunks/s at 82.2 % busy, nemotron 33.60 at 78.2 %, both
    bit-identical. **Nemotron is 0.92× harrier like for like**, not 0.77×.
    Profiling itself costs ~15 %.
  - **The CPU's time, the same for both:**
    - 80 % of samples are ggml's CPU threadpool workers (`threads: 6`)
      busy-polling while the card computes, ~200 % CPU spent waiting;
    - the main thread's own time is mostly the kernel (syscalls, scheduling);
    - no host thread at 100 %, so a feeding thread is not the ceiling.
  - **Tokenization:** 11.3 ms a text (nemotron, `pixtral`) against 10.1
    (harrier). 1.2 ms against 25–37 ms of card time a chunk: not the ceiling.
  - **What follows:**
    - card measurements only where no CPU twin runs (the scheduling-log rule,
      broken today by the rerun's timing);
    - `threads: 1` for fully offloaded embedding models, which removes ~2
      cores of spinning (its bit-identity to be checked);
    - the candidate engine below.
- **The clean baseline, no CPU twin** (München 17:2x, live engine, the same
  5,000 W1 chunks, batch 16, through encoder_fn, nothing attached):

  | arm | chunks/s | card busy | backend CPU | bit-identical |
  |---|---|---|---|---|
  | harrier c4 | 58.82 | 99.2 % | 339 % | 5,000/5,000 |
  | nemotron c1 | 46.48 | 86.8 % | 304 % | 5,000/5,000 |
  | nemotron c4 | 55.03 | 98.6 % | 344 % | 5,000/5,000 |
  | nemotron c8 | 54.98 | 98.3 % | 340 % | 5,000/5,000 |

  - **Nemotron saturates the card** at 4 in flight, and runs at **0.94×
    harrier**. 8 in flight adds nothing; 1 → 4 adds 18 %.
  - **What the CPU twin cost it:** 43 % at 1 in flight (26.74 → 46.48) and 28 %
    at 4.
  - The rows above it, measured beside the CPU twin, are superseded for speed.
  - These are not comparable with HARRIER-OPT's 51.57: that was the full
    21,348-chunk W1, and this is its first 5,000.
- **Tokenizer correctness on our engine** (all of W1 and P3, the exact
  strings, against `tokenizers` on the snapshot's tokenizer.json):
  - 71 of 40,931 texts differ, all chunks, no question: W1 203 of 5.82 M
    tokens, P3 137 of 2.32 M;
  - the cases are contractions ("O'Donnell", "O'Reilly") and combining
    marks in Devanagari and Gujarati;
  - the cause: c0fd786 maps the `pixtral` pre-tokenizer to LLAMA3's rules.
    Upstream e197cef (10-22) maps it to Tekken's, and d8d9297 (10-20) adds
    `\p{M}` to the Tekken lookaheads;
  - **their cosines to a fresh sentence-transformers reference: the
    conversion check FAILS on them** (17:19):
    - 4 of the 71 are under 0.99 (worst 0.9546), and 27 are between 0.99
      and 0.999;
    - the four are W1 entities whose NAME is the differing part: "Joseph
      O'Donnell", two Hindi names and a Thai title;
    - the 48-text check had passed only because its sample held none of
      these.
  - **So on the live engine nemotron is not NVIDIA's model** on 0.17 % of
    these texts: apostrophe forms and combining marks. The W1 and P3 tables
    carry that at this rate. It cannot move them much, but nemotron is not
    adoptable on c0fd786. The candidate engine (Tekken pre-tokenizer at
    ddf48f8) is checked on the same 71.
  - **The check itself is fixed:** `st_parity tokens` tokenizes EVERY text
    of the sets (step 0, no card). Each text that differs joins the
    reference and the compare. A sample finds what is common, and a
    tokenizer fault is rare by construction.
- **The candidate engine:**
  - LocalAI master (16acc89, 2026-11-02) pins llama.cpp ddf48f8 (10-31),
    which carries both tokenizer fixes, the fattn-mkl host-wait removal
    (0a9ef00), SYCL graph record/replay (7377bad, opt-in) and the SYCL
    ordering and FA fixes (8115b4c, 36a0e2f);
  - `deploy/localai/llama-cpp/build.sh` builds it beside the live engine with
    `SERIES_FILE`; the live series and its tag are unchanged.
- **A first run is superseded.** The node's scratch tool left out the
  `passage: ` prefix, so its bit-identity column compared different inputs.
  Arms embed through `wikidata_encoder_ab.encoder_fn` from now on.

## Wikipedia P3, vector only (München, 2026-11-02 16:38–16:48)

The set: 5,000 articles, 18,783 chunks, 400 questions (200 en, 200 it).

| encoder | MRR@10 | recall@1 | @10 | @40 | @50 | @100 |
|---|---|---|---|---|---|---|
| bge-m3 (wikipedia's) | 0.9277 | 0.900 | 0.9775 | 0.9875 | 0.990 | 0.9975 |
| harrier | 0.8650 | 0.820 | 0.950 | 0.9825 | 0.985 | 0.995 |
| **nemotron** | **0.9720** | **0.9575** | **0.995** | 1.000 | 1.000 | 1.000 |
| nemotron, cut to 1,024 | 0.9727 | 0.960 | 0.995 | 0.9975 | 0.9975 | 1.000 |

- **All 400 questions:**
  - nemotron − bge-m3 = +0.044, p = 0.0001 (32 questions better, 1 worse);
  - nemotron − harrier = +0.107, p = 0.0001;
  - the cut costs nothing (−0.0007, p = 0.86).
- **English questions:**
  - nemotron 0.972, harrier 0.944, bge-m3 0.917;
  - nemotron beats bge-m3 by 0.055 (p = 0.0001) and harrier by 0.028
    (p = 0.008).
- **Italian questions:**
  - nemotron 0.972, bge-m3 0.938, harrier 0.786;
  - nemotron beats bge-m3 by 0.034 (p = 0.002);
  - harrier's cross-language weakness is not nemotron's.
- **By the article's language (MRR@10):**
  - en articles: bge 0.949, harrier 0.796, nemotron 0.979;
  - it articles: bge 0.907, harrier 0.933, nemotron 0.965.
- **Speed on P3's longer chunks, one request in flight:** bge-m3 86.4,
  harrier 34.0, nemotron 33.4 chunks/s. Level with harrier here, 0.39×
  bge-m3.

**Reading, the two general collections that ran:**
- wikidata: nemotron ≈ harrier ≫ bge-m3 (ahead of harrier on Italian only);
- wikipedia: nemotron > bge-m3 > harrier.

It is the only model of the three that is best or tied-best on both.
Gutenberg was not run (the operator narrowed the test). What it costs is
speed: 0.39–0.54× bge-m3 at one in flight, about half at saturation on
wikidata's chunks.

## What the operator gets

Per collection:
- MRR@10, recall@1 and @10, each with the paired test against the
  incumbent and harrier;
- the whole-search rows;
- vector size and chunks/s.

The rule harrier was held to (gutenberg-encoder-bakeoff.md, "The verdict
rule") is reported beside the rows. The decision is the operator's.
