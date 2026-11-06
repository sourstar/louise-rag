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
- **The candidate engine PASSES correctness** (München 17:47–17:51; built
  as louise-llama-cpp:sycl_f16-f96fd5e3f0a4, tar sha256 b5d7ad6a…; P5 applied
  without fuzz at ddf48f8):
  - `st_parity tokens` on all of W1 and P3 (40,931 texts, 8,140,608
    tokens): 0 texts and 0 tokens differ from NVIDIA's tokenizer;
  - `compare` on the 48: worst 0.999999 (documents), 1.0 (queries);
  - `compare` on the 71 that failed on the live engine: worst 0.999999,
    none under 0.999;
  - so on the candidate nemotron is NVIDIA's model on every checked text.
- **The candidate's speed is the live engine's** (17:51–17:58, the same 5,000
  W1 chunks, no other job):

  | arm | chunks/s | card busy | byte-equal to the live engine |
  |---|---|---|---|
  | c1 | 46.80 | 86.7 % | 4,987/5,000 |
  | c4 | 55.38 | 98.7 % | 4,987/5,000 |
  | c8 | 55.24 | 98.7 % | 4,987/5,000 |
  | c4, SYCL graphs on | 55.05 | 98.7 % | 4,987/5,000 |

  - **Speed:** +0.5 to +0.7 % against the live engine, within run-to-run
    noise. The upstream SYCL host-wait changes buy nothing measurable once
    the card is saturated, and graph replay gives the same bytes at the same
    speed.
  - **Bytes:** ddf48f8's kernels give c0fd786's bytes. Only the 13 texts the
    Tekken fix re-tokenizes differ; the worst is O'Donnell, at 0.954569 to
    the live engine's vector.
  - **Within the candidate,** every arm is bit-identical to its c4.
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
- **The candidate engine is REPRODUCIBLE, exact** (invariant 12; München
  21:01:54–21:34:55, #241's wrapper, exit 0):
  - `vega_repro_check --model vega-nemotron-3-embed-1b-up --loads 3
    --repeats 2 --window 8191`, exact mode;
  - 528 of 528 texts (16 spread, 512 swept) bit-identical over 3,168
    vectors: 3 fresh loads × 2 shuffled passes.
- **So the candidate passes every correctness step:**
  - tokens: 0 texts differ on W1, P3 and gutenberg;
  - compare: ≥ 0.999999 on the 48 and on the 71;
  - speed and scores: the live engine's;
  - invariant 12: exact.

  **Operator, 2026-11-02: A.** It becomes the node's ONE llama.cpp engine,
  for every model, after that night's re-gates:
  - the repo's series moved to its base (LocalAI `16acc89`), with P3 and P4
    (MADLAD) rebased and checked on the CPU at `ddf48f8`
    (docs/notes/localai-bugs.md, "Patches we carry");
  - on the node: the sycl_f16 build from the series; every llama.cpp model
    moved to it; each space gated on it; MADLAD's byte compare against the
    old engine; then the old backends retired;
  - the CPU variant moves with #241's rebuild, after the CPU reference
    completes (RULE 1: the reference is not redone).

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

## The candidate engine's scores (München, 17:58–18:12, no other job)

W1 and P3 were re-embedded as `nemotron-up` (vector only, the questions as
typed). The scores are identical to the live engine's on every question,
on both sets:
- W1: MRR@10 0.8184, 0 questions better or worse;
- P3: MRR@10 0.9720, 0 questions better or worse;
- the 1,024 cuts: also identical to the live engine's.

So the 0.17 % tokenizer fault moved no question's rank: the candidate's gain
is correctness, not rows.

Speed at one request in flight, no CPU twin:
- W1: 41.4 chunks/s;
- P3: 68.3 chunks/s, against bge-m3's 86.4.

The afternoon's 24.1 and 33.4 were the CPU twin's doing, not the engine's
(B shows the two engines equal).

## Gutenberg on the live engine's tokenizer (München 18:36–18:43, CPU only)

`st_parity tokens` on the pool's 54,111 exact strings (53,621 documents and
490 queries, 44.5 M tokens):
- **1,475 texts (2.73 %) tokenize off NVIDIA's,** 16× W1/P3's rate, but only
  8,503 tokens (0.019 %), ~5.8 a text;
- **all at an apostrophe.** LLAMA3's case-insensitive contraction rule
  ('s|'t|'re|'ve|'m|'ll|'d) splits where Tekken does not:
  - a quotation opened by an apostrophe: "'Mine" → ' | M | ine, against
    NVIDIA's ' | Mine;
  - archaic -'st verbs: "bad'st" → bad | 's | t, against bad | ' | st;
  - plain modern contractions (don't, it's) tokenize the same.
- **So the gutenberg row runs on the candidate engine,** where the count is
  expected to be 0.

## Gutenberg, vector only (München 18:36–19:23, candidate engine, no other job)

The pool: 53,621 documents, 490 queries judged by work. nemotron-up
embedded from scratch, its prefixes, 2,048 dims, 4 in flight, one text a
request.

| model | hit@1 | hit@10 | MRR@10 | hit@40 | hit@50 | hit@100 |
|---|---|---|---|---|---|---|
| Qwen3-4B Q4 (stored, gutenberg's) | 0.7918 | 0.9776 | 0.8630 | 0.9939 | 0.9959 | 0.9980 |
| **nemotron-up** | **0.8204** | 0.9612 | **0.8682** | 0.9918 | 0.9939 | **1.0000** |
| harrier (gutenberg's instruction) | 0.7429 | 0.9041 | 0.7989 | 0.9469 | 0.9531 | 0.9633 |
| bge-m3 F16 | 0.6939 | 0.9041 | 0.7618 | 0.9653 | 0.9714 | 0.9755 |

- **Paired MRR@10:**
  - nemotron − Qwen3-4B = +0.005, p = 0.68: level;
  - nemotron − harrier = +0.069, p = 0.0002;
  - nemotron − bge-m3 = +0.106, p = 0.0001.
- **Against Qwen3-4B:** better at hit@1 (+0.029) and at depth 100, worse at
  hit@10 (−0.016). None of these is tested.
- **Speed:** 19.1 texts/s with the card 94.8 % busy, against gutenberg's
  Qwen3-4B at 4.4 texts/s in production (batch 16): ~4.3× faster.

**Across the three general collections,** nemotron is the best or tied-best
on each:
- wikidata: level with harrier, ahead on Italian;
- wikipedia: ahead of bge-m3 and harrier;
- gutenberg: level with Qwen3-4B.

No other candidate is. The vector is 2,048 dims, or 1,024 cut for almost
nothing. The re-embed is coarse: gutenberg ~5–6 card-days, wikidata ~7–9,
wikipedia smart ~15, so ~4 weeks of card, ~1.5 months with the quiet hours.

## The residual ~5 % idle on long texts (München 19:31–19:53)

The gutenberg pool's first 5,000 texts (~846 tokens each), candidate engine,
exact mode, nothing else running:

| batch × in flight | texts/s | card busy | byte-equal to batch 1 |
|---|---|---|---|
| 1 × 4 | 19.06 | 95.0 % | 5,000/5,000 |
| 1 × 8 | 19.06 | 95.3 % | 5,000/5,000 |
| 16 × 4 (production's) | 19.08 | 95.5 % | 5,000/5,000 |
| 16 × 8 | 19.06 | 95.4 % | 5,000/5,000 |

- **Not per-request overhead:** batch and requests in flight change nothing.
- **Batch-invariant:** at f16 on long texts, every arm gives batch 1's bytes.
- **The cause** (perf on the backend): at `parallel:1` llama.cpp runs one
  sequence at a time, synchronously. The host's work for each sequence
  falls between the card's bursts: graph build and submit, the KQ mask
  (n² entries), input copies, the f16 output's conversion, and the sync.
  That is ~2.4 ms of a ~52 ms text.
  - The main thread reads 98 %, but mostly spinning in `sched_yield` (Level
    Zero's host sync).
  - The CPU is not the limit: 345 % of 12 cores.
  - On W1's short chunks the same costs are smaller, hence 98.6–99 %.
- **What would remove it,** none measured:
  - overlapping the next sequence's host work with the card's current one,
    an engine change. llama.cpp's own way, `parallel` > 1, batches
    sequences together and moves bytes, so VeGa refuses it;
  - the KQ mask built on the card.
  - Either buys at most ~4.5 % on long texts and ~1 % on short ones.
- **Decided (operator, 2026-11-02): left as it is.** "A, bit exactness is at
  best a 5% to pay, it is worth the price."

## Both modes are part of a model's validation (operator, 2026-11-02)

"About the validation of a model, both modes need to be tested. A model
shall be able to produce bit exact results if needed. Now that we have proved
that bit exactness is achievable, give me the speed mode numbers."

- **The exact mode:** the vendor's reproducibility mode,
  `GGML_SYCL_ENABLE_DNN=0` on Intel SYCL. It must reproduce its own bytes,
  so that a collection asking `exactness = "exact"` can have them.
  Nemotron does, on both engines (every arm above).
- **The speed mode:** the vendor's fast path (oneDNN's matmuls,
  `GGML_SYCL_ENABLE_DNN=1`; `GGML_SYCL_FA_ONEDNN` stays 0 after its NaN on
  this card). It is measured for chunks/s, for self-reproduction within
  invariant 12's near-exact bound (0.99999, no NaN), and for parity with the
  fp32 reference.
- **Measured raw for now.** VeGa's wrapper refuses a SYCL run without
  `GGML_SYCL_ENABLE_DNN=0` (its DETERMINISM map, invariant 12's
  enforcement). Serving the speed mode through VeGa needs the operator's
  change to that rule first.

## The speed mode (München 18:12–18:2x; candidate engine, raw, no other job)

The same 5,000 W1 chunks, batch 16, `GGML_SYCL_FA_ONEDNN=0` in both modes.

| arm | chunks/s | card busy | against | byte-identical | worst cosine | NaN |
|---|---|---|---|---|---|---|
| exact c1 | 46.20 | 85.3 % | exact c4 | 5,000/5,000 | — | 0 |
| exact c4 | 55.27 | 98.9 % | — | — | — | 0 |
| exact c8 | 55.22 | 98.8 % | exact c4 | 5,000/5,000 | — | 0 |
| speed c1 | 47.53 | 85.4 % | its c4 | 133/5,000 | 0.9999992 | 0 |
| speed c4 | 56.83 | 98.7 % | exact c4 | 121/5,000 | 0.9999983 | 0 |
| speed c8 | 57.00 | 98.8 % | its c4 | 133/5,000 | 0.9999925 | 0 |
| speed c4, fresh load 1 | 56.85 | 98.7 % | its first c4 | 133/5,000 | 0.9999930 | 0 |
| speed c4, fresh load 2 | 57.07 | 98.8 % | its first c4 | 133/5,000 | 0.9999979 | 0 |

The speed mode is oneDNN's matmuls: `GGML_SYCL_ENABLE_DNN=1`.

- **Speed:** +2.8 to +3.3 % against the exact mode, at every concurrency.
- **The speed mode is near-exact, not exact:**
  - only 133 of 5,000 vectors give back their bytes;
  - the worst self-cosine over orders, concurrency and two fresh loads is
    0.9999925, within invariant 12's 0.99999;
  - no NaN in any run.
- **Parity with the fp32 references:** worst 0.999999 on the 48 and on the 71,
  the same as the exact mode, with none under 0.999.
- **Raw against VeGa:** 3,082 of 5,000 vectors are byte-equal. That is the
  read path, not the engine: VeGa reads base64 float32 and normalises, the
  raw tool reads JSON floats.
- **Reading:** bit exactness is achievable, since the exact mode is
  byte-identical at 1, 4 and 8 in flight. The speed mode buys ~3 % for
  byte-level reproducibility, within near-exact's bound.

## Production (operator, 2026-11-02)

"Wipe wikidata vectors, use Nemotron instead, rule 1. Suspend any gutenberg
and wikipedia further embedding, when wikidata is over make a gutenberg-nemo
with Nemotron and embed it. Once done the old gutenberg can be backed up in
tank and retired. Then do the same with wikipedia, smart embedding only."

**The full 2,048 dimensions are kept** (operator, 2026-11-03: "keep the full
vectors of Nemotron, it will be interesting to verify how much the quality
drops halfening them once we have a full overview"). The 1,024 cut is
measured afterwards, on every Nemotron collection at once. Rows so far: W1
+0.009 for the full vectors (p = 0.165), wikipedia P3 −0.0007 (p = 0.86).

- **The document prefix is the space's.** Every row above embedded documents
  with `"passage: "`, as the card says; the spec had a query instruction only.
  Codex version 11 adds `space.document.instruction`, inside `space_id`,
  prepended to every document input by the one client that sends documents
  (`embeddings._sent`): chunks, the calibration set at `create`, the gate,
  the re-check, probes. The operator's A, by default, the same night.
- **2,048 dims, native.** The 1,024 cut measured no worse (W1 full − cut
  +0.009, p = 0.165; P3 equal) but needs a truncation the spec does not
  have; a 1,024 space can be derived later from the stored 2,048 vectors
  without the card (09 §9.3 `derivation`, `truncate`).
- **Order:** wikidata re-created (RULE 1, records and chunks carried, the
  1.44 M bge vectors wiped at the re-creation: 3.3 GB); 43.27 M chunks,
  ~9 card-days at W1's c4 rate, ~190 GB at 2,048 f16. Then gutenberg-nemo,
  the old gutenberg backed up to tank and retired; then wikipedia-nemo, smart
  embedding only, the same way.

## What the operator gets

Per collection:
- MRR@10, recall@1 and @10, each with the paired test against the
  incumbent and harrier;
- the whole-search rows;
- vector size and chunks/s.

The rule harrier was held to (gutenberg-encoder-bakeoff.md, "The verdict
rule") is reported beside the rows. The decision is the operator's.

## The chat model: Nemotron in place of Qwen3-8B? (operator, 2026-11-03) — EVALUATING

"Consider replacing the chat model with nemo equivalent, look for comparisons
already available"; then "Evaluate both A and B".

The chat model's jobs in Louise:
- Ask's answers, streamed;
- Ask's tool loop (`app/ask/chat.py`: `louise_search`, `louise_lookup`,
  `louise_traverse`);
- document summaries, map then reduce (`app/summarize.py`).

| | model | Artificial Analysis Intelligence Index | size on the card | languages (card) |
|---|---|---|---|---|
| now | Qwen3-8B | 8 reasoning, 5 non-reasoning (Louise runs it non-reasoning) | ~5–9 GB | 119 |
| **A** | NVIDIA-Nemotron-3.5-Lightning-30B-A3B (OpenMDW-1.1) | 24 | ~18 GB at Q4 | **6**: en, es, fr, de, it, ja |
| **B** | NVIDIA-Nemotron-3.5-Bolt-E4B-4GB (OpenMDW-1.1, Gemma-4-E4B base) | not published yet | 4 GB | 34 |

- The index numbers come from search-result snippets; artificialanalysis.ai
  is not reachable from the agent session to read them first-hand.
- No card compares with Qwen3-8B. Bolt-E4B's card (NVIDIA's harness, thinking
  on) against Qwen3.5-4B: grounding GaRAGe 42.6 against 24.7, RAGTruth 77.6
  against 75.6; tool calling BFCL-v3 68.5 against 71.0 and Tau2 55.1 against
  75.1.

**The evaluation:** `tools/measure/chat_eval.py`.
- Ask's own loop for each model, on the 451 labelled queries in 18
  languages (one-language-per-work.md).
- Scored without an LLM judge: tool calls, failed calls, loops that hit the
  bound, the answer in the query's language (the node's own deterministic
  guess), and time.
- Split into Lightning's six languages and the others.
- A blind side-by-side sample for the operator to read, with the key kept
  in a separate file.
- Qwen3-8B is the reference row.
- First, the engine checks: A's nemotron_h on SYCL, B's gemma4, the memory
  beside wikidata's embedding, tokens/s. Run at night.

**B first, now; A for some uses** (operator, 2026-11-03: "Evaluate B now, in
case it fits we switch it right away as chat and we validate option A as an
alternative for some use cases (i.e. serving chat while working on
wiki-gutenberg)").
- Installed FROM LOCALAI'S MODEL GALLERY (operator, the same day: "If it is
  available use localai to get the model. Do not reinvent the wheel"):
  - entry `nemotron-3.5-bolt-e4b-q4`: unsloth's UD-Q4_K_XL and its vision
    projector, pinned by sha256, plus the entry's tool-call handling;
  - our overrides carry only the chat model's engine settings
    (`deploy/vega/stage/gallery/louise-chat-nemotron-bolt-e4b.json`, the
    `/models/apply` body);
  - `deploy/vega/stage/models/louise-chat-nemotron-bolt-e4b.yaml` is what
    LocalAI writes from it.
- **Both precisions** (operator, the same day: "Localai allows downloading
  alternatives with a different precision, check. Evaluate both 8bit and 4bit
  builds").
  - An apply names its build in `variant`. The entry's own name selects its
    base build (LocalAI `core/gallery` `SelectVariant`).
  - Without one, LocalAI installs the largest build the host can run: the
    first install, unnamed, got UD-Q8_K_XL.
  - So B is two models, `louise-chat-nemotron-bolt-e4b-q8` and `-q4`, each
    with its own apply body and YAML.
  - Each must meet the five conditions on its own. If both do, the better
    one is chosen; where the two cannot be told apart (tool errors and
    bound hits equal, the language share within the test's noise), the
    smaller, q4, is chosen, as the encoder rule breaks ties by cost.
- A model the gallery holds is installed from it from now on. Of the Nemotron
  candidates, it holds Lightning-30B-A3B (q4, q8, nvfp4) and Bolt-E4B (q4,
  q8), not Rerank-8B or Bolt-74B-A9B, which stay Hub downloads.
- "It fits" means all of these, against Qwen3-8B on `chat_eval` (10 queries
  per language, the same run):
  1. it loads on the card's SYCL engine, and no thinking reaches the
     answer's content;
  2. it fits in memory beside wikidata's embedding and the reranker;
  3. no more failed tool calls and no more loops that hit the bound than
     Qwen3-8B;
  4. the answer in the query's language at least as often, overall and in
     it/en/fr/de;
  5. median time per answer no worse.
- Then `CHAT_MODEL=louise-chat-nemotron-bolt-e4b` on web and worker. Qwen3-8B
  stays installed as the rollback, and the blind sample goes to the operator
  afterwards.
- A, Lightning-30B-A3B, is validated as the chat model for the hours it is
  loaded anyway for wiki-gutenberg's jobs, with the same chat_eval run.


### Qwen3.5-9B as well (operator, 2026-11-03: "Evaluate also Qwen3.5-9B")

| | Qwen3-8B (now) | Qwen3.5-9B |
|---|---|---|
| languages (card) | 119 | 201 |
| architecture | dense transformer | hybrid: 8 × (3 Gated DeltaNet + 1 gated attention) |
| thinking | off by `/no_think` and `reasoning_effort: none` | ON by default; no `/no_think`; off by the template's `enable_thinking` |
| weights | Q4_K_M (Ollama's copy) | gallery `qwen3.5-9b`: Q4_K_M; Q8_0 from the Hub |
| card numbers (its thinking mode) | — | IFEval 91.5, MMLU-Pro 82.5, MultiChallenge 54.5 |

- **Installed from LocalAI's gallery**, entry `qwen3.5-9b`, named as the `variant`.
  The 8-bit build is not in the gallery: the same entry with Q8_0 as an added
  file. Its uri is pinned to the Hub's commit and its sha256 is written in at the
  install (staged as placeholders, because the Hub API is refused here).
- **No vision projector.** Ask sends text, and the entry's projector is F32,
  1.8 GB.
- **Order, by the operator's rule for Bolt:** q8 first, on the same stack and
  tags as the others (#270, #272, #273). q4 runs only if q8 is promising.
- **Engine checks before the benchmark:**
  - the DeltaNet layers' ops on SYCL. If one falls back to the CPU, generation
    tok/s shows it;
  - flash attention at head dim 256;
  - no thinking in the content, and no reasoning tokens spent (time per answer);
  - three greedy prompts for garbage.

### The bar, and the Qwen versions (operator, 2026-11-03)

"Evaluate also the other Qwen versions. As long as it answers correctly for
english, italian, german, french and spanish I am good with it. Other users
can use another chat model to fit their needs."

**The bar is the operator's five:** en, it, de, fr, es (`chat_eval` `--langs`,
scored as "the operator's five").
- Correct means: the answer is in the question's language, the searches run
  without tool errors, nothing fails or hits the round bound, and the blind
  sample reads well.
- The other 13 languages are reported, but do not decide.
- The chat model is this node's choice. Another node names its own.

**Candidates.** Every one runs on our stack: #270, #272, #273, #275, #278.
- On the bench since the morning: Qwen3-8B (live), Bolt-E4B q4 and q8,
  Qwen3.8-9B (Empero), Qwen3.5-9B q4, q4 + DFlash and q8.
- Qwen's other versions, installed for the summary job anyway:
  - Qwen3.6-35B-A3B (MoE, 3 B active);
  - Qwen3.8-27B (dense).
- Left out:
  - Qwen3.6-27B: its successor 3.8-27B beats it on every row of Qwen's own card;
  - Qwen3.5-27B and Qwen3.5-35B-A3B: superseded by 3.6 and 3.8;
  - Qwen3.8-Flash-Next: 125 B, does not fit the card;
  - Qwen3.8-4B and 2B: Empero distills, like the 9B.

**The run:** `--langs en,it,de,fr,es --per-lang 10`, 50 questions a model. Among
those that meet the bar, the faster one wins on warm seconds per answer. VRAM
beside the embedder and the reranker is recorded too.

**The operator's requirements (the same day):** "I need it fast, not too big,
precise in articulating the answer and able to use tools. Check the qwen 3.5
and 3.8 models I downloaded in localai."

| requirement | how it is measured |
|---|---|
| fast | warm median seconds per answer, and generated tok/s |
| not too big | VRAM loaded at a 16k context, beside the embedder and the reranker |
| precise | `chat_eval` (#280): cited sources per answer; the share of cited uids that a tool really returned ("grounded"); answers citing nothing. Plus the blind sample |
| able to use tools | calls per answer, tool errors, answers given with no tool call, round-bound hits |

The models the operator downloaded run the "five" tag first.

**The operator's three, the most important (the same day):** "how effectively
the model conveys the query to Louise, how effectively it summarizes the
results, and the amount of citations from the results." Measured by
`chat_eval score --works works.json` (#284). Every question is labelled with
its work, and `works.json` lists the work's gutenberg books.

| the operator's measure | the number |
|---|---|
| conveys the query | the model's own searches returned the work (share), and the work's best rank over them (MRR). The reference is `run --direct`: the question searched as typed, no model. A model that beats it conveys the query better than the reader's own words |
| summarises the results | the answer cites the work it was asked about (share); "found but left out": its searches found the work and the answer did not cite it |
| citations | cited per answer; grounded (each cited uid was returned by a tool); citing nothing though results came back |

**The fourth (the same day):** "is it adding some additional information or
distorting an information from the collection, overlapping basically its own
knowledge?"
- **Method:** entity-level factual consistency (Nan et al., EACL 2021), simplified
  (#285). Every number and name the answer states is looked up in what its tools
  showed the model (`chat.compact`) and in the question. One found in neither was
  added, or distorted.
- **Numbers** are counted apart, since they hold in every language.
- **Names** are noisy in German, which capitalises every noun, and wherever the
  answer translates a name.
- **The blind sample** lists each answer's flagged items under it, for the
  reader to judge.
- **The full method** would add spaCy NER, a new dependency; asked for if
  the names column decides.

### Only Alibaba's Qwen, and the official 2–4B candidates (operator, 2026-11-03)

"Delete all the qwen not from Alibaba": Empero's distills (Qwen3.8-9B, 4B, 2B,
35B-A3B), JonathanColetti's uncensored 27B, UkisAI's Swift 27B and z-lab's
DFlash drafter leave the stage. Qwen's own weights in a third party's GGUF
(unsloth, bartowski) stay: same weights, a conversion. LocalAGI, the one user
of the Empero 2B, now runs `louise-chat-qwen3-8b` (deploy/truenas-localagi.yaml).

"Search around for official chat models at about 4b and 2b". Each one is
published by its maker and is in LocalAI's gallery, except Phi-4-mini.
Languages and licence are from the Hub card (2026-11-03):

| model (gallery entry) | params | the five | tools | licence |
|---|---|---|---|---|
| Qwen3.5-4B (`qwen_qwen3.5-4b`, bartowski GGUF) | 4.7 B | yes (201 langs) | yes | Apache-2.0 |
| Gemma 4 E4B (`gemma-4-e4b-it`, ggml-org; `-qat-q4_0`, Google's own) | 4 B effective, 8.0 B stored | yes (140+) | yes | Apache-2.0 |
| Granite 4.2 3B (`granite-4.2-3b-q4/q8`, IBM's own GGUF) | 3.7 B | yes (12) | yes (`tool-calling` tag) | Apache-2.0 |
| Ministral 3 3B (`mistralai_ministral-3-3b-instruct-2512-multimodal`, unsloth) | 3.8 B | yes (11) | yes | Apache-2.0 |
| Phi-4-mini (not in the gallery) | 3.8 B | yes (23) | yes | MIT |
| SmolLM3-3B (`huggingfacetb_smollm3-3b`) | 3.1 B | de not in the card's tags | yes | Apache-2.0 |
| Llama 3.2 3B (`llama-3.2-3b-instruct:q8_0`) | 3.2 B | yes (8) | yes | Llama 3.2 licence (use policy, 700 M MAU) |
| LFM2.5-2.6B (`lfm2.5-2.6b-q8`, Liquid's own) | 2.7 B | yes (16) | yes | LFM Open License (revenue cap) |
| Qwen3.5-2B (`qwen_qwen3.5-2b`) | 2.3 B | yes | yes | Apache-2.0 |
| Gemma 4 E2B (`gemma-4-e2b-it`, Q8) | 2 B effective, 5.1 B stored | yes | yes | Apache-2.0 |

On the bench, Q8 first as for the 8–9 B tier: Qwen3.5-4B, Gemma 4 E4B, Granite
4.2 3B, Ministral 3 3B; then Qwen3.5-2B and Gemma 4 E2B. Left out: Phi-4-mini
(no gallery entry; added if the four fail), SmolLM3 (German unstated), Llama
3.2 (2024, a restricted licence), LFM2.5 (a revenue-capped licence). The one
small model measured so far, Empero's 2B, never called a tool (five2): tool
use is the first cut at this size.

### The interface first (operator, 2026-11-03)

"Before fix the way any model interfaces with louise. Since then stop this
painfully useless test." The benchmark was stopped at five3 (qwen3-8b
40/50).

**What the models sent** (München, five3, the 39 qwen3-8b answers with
their calls):
- One `louise_search` per question, the question copied VERBATIM, no
  second search when the work was missing.
- Found or not depended only on how the question searches as typed: the
  test measured the question, not the model.
- Stray arguments: `"limit": 10`, `"space": "gutenberg"`.
- The Empero 2B never called the tool (five2).

**What changed** (app/ask/chat.py; 05 §5.4 amended):
1. ~~Rewrite-Retrieve-Read~~ (Ma et al., EMNLP 2023): asking the model
   for the searches the question implies. Walked and REMOVED the same day
   (below, "The walk").
2. Louise searches first. The question runs as the Ask tab runs it,
   with translations, before the model's first round.
3. Numbered sources `[n]`, as on the Ask tab (the ALCE form). A record
   keeps its number every time it comes back. The answer carries
   `sources`, `cited` and `cited_unknown` (a number no search showed).
4. The model sees only `query` and `collections` (`id`; `id`, `relation`
   and `depth`). Translation is always on. An argument the model invents
   is dropped and named in the turn (`dropped`).

The tools stay, so a model may search again. The MCP declarations are
unchanged; their description now says to search again by title and
author.

**chat_eval:** a new column, "the question as typed found the work",
beside "every search found it". The rows record whose search each was
(`hits_by`), the rewrites and `rewrite_s`. `cited` comes from the
answer's own resolution.

**Qwen3.5-4B and 2B** (operator, the same day) are already on the list
(#289). Qwen publishes no GGUF of them, so the staged files are
bartowski's conversions of Qwen's own weights.

**The bench resumes on this:**
1. Tag `iface`, `--redo`, on the same 50 questions: qwen3-8b and
   qwen3.5-9b-q8, with the `direct` row as the reference.
2. Then the six 2–4B models.

### The walk (operator: "Ask a question, check the result, do not run before walking")

München, 16:39, 95043ec, qwen3-8b, gutenberg. The work is WQ977055,
Le Collier de la Reine. The gold is gutenberg:18199, 18200 and 20122.

| | EN, the question verbatim | IT, the question verbatim |
|---|---|---|
| the model's rewrites | "The Diamond Necklace", "Le Collier de diamants": WRONG titles | "La Tour de Nesle": another Dumas work |
| searches (4, side by side) | 22.3 / 20.0 / 13.3 / 23.1 s | 21.0 / 18.0 / 15.7 / 10.3 s |
| the work among 23–25 sources | no | the Finnish edition (54229), not in the gold |
| answer | "The Diamond Necklace", 1785, Cardinal de Rohan: from MEMORY, citing unrelated sources | names [20], the Finnish edition; invents nothing |

**The skill walked by hand**, with an agent that knows the title:
- The question verbatim missed it; the Finnish edition came at rank 8.
- "Le Collier de la Reine Dumas" found 18199, 18200 and 20122 at ranks
  1–3.

**Read:**
- A rewrite is only as good as the model's knowledge.
- Qwen3-8B guessed wrong twice, and the wrong title became the answer.
- The searches side by side cost ~23 s of wall time before the model.

So the rewrite step is out:
- a model that knows a title still searches for it with the tool, one
  search at a time;
- the system prompt now says, when no source is what was asked, to say
  so and never to supply it from memory.

The gold in works.json misses the translations, so "found" undercounts.

### The measure walked, and the reranker's slots (München, 2026-11-03)

**citation_support on walk 2's answers** (#295, #296):
- First pass: 2 correct flags, 4 false positives ("Luigi XVI" from the
  question, a possessive), and 1 miss (the invented title, in an uncited
  sentence).
- After #296 it matches the reading:
  - EN: the uncited title sentence flagged; both cited sentences
    supported.
  - IT: the [2] and [1] sentences flagged; [7], [9] and [10] supported.

**Reranker (louise-rerank-bge):**
- b1: 4–5 calls per answer, one per language variant of the question;
  5.3–5.9 s per answer in all; 0 duplicate pairs of 2,145.
- b2, the same 20 bodies:

  | slots | pairs/s | median s (40 docs) | card busy | against p1 |
  |---|---|---|---|---|
  | p1 | 16.0 | 2.50 | 93.8 % | — |
  | p4 | 13.5 | 2.96 | 94.0 % | 0/20 byte-equal, max \|Δ\| 3.9e-2, top-10 changed 1/20 |
  | p8 | 13.3 | 3.01 | 94.6 % | 0/20 byte-equal, max \|Δ\| 3.9e-2, top-10 changed 0/20 |

  One slot already saturates the card. More slots are slower and not
  exact, so p1 stays. Replicas would meet the same saturation and were
  not run. What is left to try is less work per answer: fewer language
  variants, fewer candidates, or shorter texts. Each changes the results,
  so each is measured on quality before speed (pipeline_eval).

**The operator's two Qwen3.5 files** (4B and 2B, Q4_K_M, from the LocalAI
catalogue) match the gallery's sha256: bartowski's quantizations of Qwen's
weights.

**Run started 17:14:** tag `iface`, the five languages, 10 questions
each, gutenberg, `--search-cache`. Order: direct-iface (records the
searches), qwen3-8b, qwen3.5-9b-q8, qwen3.5-4b-q4, qwen3.5-2b-q4.

### The blind read of the iface run (2026-11-03)

A reader agent saw only the letters A–D, shuffled per question, then the
key was applied. 20 questions × 4 models:

| model | right work | partly | wrong | not named | raw tool call | wrong language | inventions | answers with none | mean quality (1–5) | best of the 4 |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen3-8b | 14 | 2 | 1 | 3 | 0 | 1 | 13 | 8 | **3.40** | 9 |
| qwen3.5-9b-q8 | 12 | 2 | 1 | 5 | 0 | 0 | **12** | **11** | 3.35 | **10** |
| qwen3.5-4b-q4 | **15** | 0 | 2 | 3 | 0 | 0 | 22 | 6 | 3.15 | 8 |
| qwen3.5-2b-q4 | 1 | 1 | 1 | 0 | **17** | 1 | 6 | 1 | 1.20 | 0 |

**Read:**
- Qwen3-8B and Qwen3.5-9B are close. The 8B is faster (4.5 s against
  7.8 s).
- The 4B finds the work most often and invents the most.
- The 2B's answer was a raw `<tool_call>` in 17 of 20 questions: past
  the round bound, with the tools withheld, it wrote the call as text.
  Fixed in app/ask/chat.py: a tool call written as text is never the
  answer; a reply that is only one is asked once more to answer now.

**The model failures that remain**, the same in every model:
- refusing when a snippet does not state the plot word for word;
- anchoring on a wrong top-ranked source and inventing a fit (Q8: *Le
  médecin malgré lui* for *All's Well*);
- mixing up people across works;
- invented translated titles.

**The entity checker is not a faithfulness measure.** Its precision, by
the reader:
- about 2 % in German, where every noun is capitalised;
- about 5 % elsewhere (headings, translated names, Gutenberg ids).

It also missed most of the serious errors, because those use words the
sources contain ("traduit par Maquet", "Isla … 1922", an invented Diderot
title). Its columns stay in chat_eval as counts, but they decide nothing.
What decides is the blind read, and next a trained checker
(LettuceDetect, which has it/de/fr/es models).

### Sampling, Q8 first (operator, 2026-11-06)

**The operator's decision:** "Chat model for rag retrieval: qwen3 8b,
qwen3.5 9b and command r7b are the only candidates, qwen3.6 35b is too big
and will be user evaluated for wiki-gutenberg summaries. For the chat
models benchmarking and optimizations start with the Q8 quantization,
let's try to optimize the results by changing their parameters." The
operator's table, found online for Qwen3.5-9B: temperature 0.1, top_p
0.8, top_k 20, min_p 0, repetition 1.0, presence 0, frequency 0, context
16,384, output 2,048, parallel 1, thinking off, KV cache Q8_0.

**What the makers say** (read 2026-11-06):

| model | source | values |
|---|---|---|
| Qwen3-8B | card, non-thinking | temperature 0.7, top_p 0.8, top_k 20, min_p 0; presence 0–2 "to reduce endless repetitions", which "may occasionally result in language mixing" |
| Qwen3.5-9B | card, instruct mode, general tasks | 0.7, 0.8, 20, 0, presence 1.5, repetition 1.0 (reasoning tasks: 1.0, 1.0, 40, 0, presence 2.0) |
| Command R7B | Cohere's chat API defaults (known; docs.cohere.com unreachable from here, the card is gated) | temperature 0.3, p 0.75, k 0 (off) |

No source sets a temperature for answers grounded in given documents.
The one controlled study found (TempPerturb-Eval, arXiv 2512.01183, LREC
2026) measured that higher temperatures make answers more sensitive to
noisy retrieved passages. It does not name a best value. The operator's
0.1 is that direction taken almost to greedy, and Qwen warns that greedy
decoding brings "endless repetitions". The 35B looped at 0.2. So 0.1 is
measured as an arm, with loops counted, not adopted on faith.

**What LocalAI does with the values** (its code read 2026-11-06 at master:
core/http/middleware/request.go, core/backend/options.go,
backend/cpp/llama-cpp/grpc-server.cpp, core/config/generic_defaults.go,
inference_defaults.json):
- **An unset value is not the card's.** It takes the generic default:
  temperature 0.9, top_p 0.95, top_k 40, min_p 0. Name-matched families
  take theirs: `qwen3` gets 0.6/0.95/20, Qwen3's THINKING values;
  `qwen3.5` gets the card's instruct values; there is no Cohere entry.
- **A request's penalty of 0 means "unset".** Presence, frequency and
  repeat penalties are plain floats merged on `!= 0`. An arm that sends 0
  silently runs the model file's value.
- **No `repeat_last_n` in the file means no penalty at all.** LocalAI sends
  the file's `repeat_last_n` as given, 0 when absent. In llama.cpp,
  `repeat_last_n` 0 disables every penalty ("0 = disabled"). So the
  presence_penalty 1.5 in the 9B's and the 35B's files most likely never
  applied. The probe below checks this on the node's own version.
- **The penalty window is not the card's.** vLLM, where Qwen measured
  1.5, counts the tokens generated so far. llama.cpp counts the last
  `repeat_last_n` tokens, the prompt's tail included. At 64, a citation
  "[2]" repeated within a sentence or two is penalised too.

**What changes**, in the three Q8 files and the benchmark:
- every sampling value is stated in each file (the card's), with neutral
  penalties and `repeat_last_n` 64;
- `louise-chat-qwen3-8b-q8` is staged: Qwen's own Q8_0 (Qwen/Qwen3-8B-GGUF).
  The live file is Q4_K_M (Ollama's qwen3:8b);
- `chat_eval run --sampling k=v,…` sends an arm's values with every
  request. The run is the arm: its name and settings.json say which, and
  a penalty of 0 is refused;
- loops are counted: a span repeated back to back three times.

Already as the operator's table: 16k context, parallel 1, thinking off.
Ask's answer cap stays at 1,500 tokens rather than 2,048, so a loop
costs less. The KV cache: F16 for the sweep, one Q8_0 arm on each
winner, unless the operator chooses Q8_0 from the start (put to the
operator, 2026-11-06).

**The operator, 2026-11-06:** "Go for the first [KV cache F16 for the
sweep, a Q8_0 arm on each winner], we are testing. Before lowering the
quantization level experiment different settings at Q8; when you find the
optimal one you lower the quantization to observe the performance
decrease. Be absolutely sure that localai is applying your settings
otherwise the test is void. In case it does not, check online for open
bugs before planning anything, then report to me."

**One more pitfall, from the code of the family defaults**
(core/config/inference_defaults.go). They fill a penalty that is 0, and
they match by name: `louise-chat-qwen3.5-9b-q8` contains "qwen3.5". So
the 9B file's presence 0 runs at 1.5. Now that `repeat_last_n` is 64,
that penalty is no longer disabled.

**THE PROBE** (tools/measure/sampling_probe.py, `chat_eval probe`) checks
every value by its effect, on the node, through the body Ask sends. A run
of a model whose probe has not passed is refused.

**Searched online for open bugs, 2026-11-06:**
- None is open on a penalty of 0 read as unset, `repeat_last_n` 0, or a
  family default filling a 0.
- Closed:
  - LocalAI #11230, "Gallery install writes sampling parameters where the
    config loader cannot read them" (2026-09-03);
  - #1051, request penalties discarded (2023).
- Merged: #12732, "bump inference defaults from unsloth" (2026-10-16).
- llama.cpp #25388: `--repeat-last-n -1` silently disables the penalty,
  so -1 is never used here.

**The order:**
0. **On the node: what reaches the engine.**
   - Install the Q8 and record its VRAM beside the search models (the 35B
     evicted them).
   - At temperature 0, a prompt that repeats a word: send presence 2.0
     to `louise-chat-qwen3-8b` (no `repeat_last_n`) and to the Q8 file
     (64). Identical answers then differing answers confirm the pitfall.
1. **The arms**, all at Q8 with a KV cache of F16, seed 0, on the 50
   recorded searches (en it de fr es × 10). Each arm starts with a pilot
   that is read.

   | model | arms |
   |---|---|
   | qwen3-8b-q8 | t07 (the file = the card), t04, t01 (the operator's) |
   | qwen3.5-9b-q8 | t07p15 (the card exactly), t07, t04, t01 |
   | command-r7b-q8 | t03 (the file = Cohere's), t01, t07, after the patched engine |

   An arm at 0.4 or above also runs with seeds 1 and 2, so that a
   difference smaller than one seed's spread is not read as a result.
2. **The read.** The table first: cites the work, left out, supported,
   uncited claims, language, looped, time. Then per model the blind read
   of its arms on the same 30 questions. Then the three winners blind
   against each other.
3. **Q4 last.** Each winner's Q4 at its winning sampling is kept only if
   the blind read finds no loss.

**Not built, written down:** DRY ("Don't Repeat Yourself", llama.cpp's
n-gram repetition sampler) is the standard answer to loops. It does not
penalise a single token repeated, such as a citation. LocalAI's request
schema has no DRY field. It is worth adding only if the low-temperature
arms loop and presence 1.5 costs quality.

**The Q8 pilots, read** (2026-11-06, 15 questions each, en/it/de/fr/es × 3, timed beside the CPU twin so their seconds are not used; branch measure/chat-pilots-q8-sampling, 221d4fdd):
- **Mechanically clean, all seven:** no request failed, no loop, no tool failure, every answer in the reader's language, every citation grounded.
- **Qwen3-8B at 0.1, 0.4 and 0.7:** the same answers in substance, and the same errors at every temperature:
  - *Le médecin malgré lui* named for *All's Well*;
  - invented details: "Athens' invasion of Corcyra" at 0.1, "nine years after Salamis" at 0.4, "Caliban, Prospero's illegitimate son" at 0.1;
  - at 0.7, a raw `gutenberg:` id written into two answers.
- **Qwen3.5-9B copy (presence 0.0001):**
  - Answers are twice as long as the 8B's.
  - It is better when the work is not found: at every temperature, #7 says "the collections do not show…" and names the closest source, as the system prompt asks.
  - **It thinks aloud at 0.4 and 0.7, with or without presence 1.5, but not at 0.1.** On #1 it named invented titles and retracted them in place: "*La Dame de Monsoreau*? No, that's incorrect… *La Reine Margot*? No, again incorrect". The reader sees invented titles, and that answer ran to 4.5–5.7 k characters and 32–40 s.
  - `chat_eval` now counts this as "thinks aloud". The column is read from the stored answer, so it covers rows of any age; on the pilots it flags exactly these three answers.
- **Decision:** a pilot-ok for all seven. The whole runs go on.
