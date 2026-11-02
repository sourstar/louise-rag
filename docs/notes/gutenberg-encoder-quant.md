# gutenberg: is Qwen3-4B at 4 bits on bge-m3's level? A measurement

*Planned 2026-11-02; measure only. The operator: "Measure only, I am
wondering if the quantization to 4 bits of qwen3 is putting it at the same
level of bge at 16 bits". Results go in "Result" below when the run is done.*

## The question, and the three rows that answer it

| row | encoder, weights | document vectors | queries |
|---|---|---|---|
| **Q4** | Qwen3-Embedding-4B, Q4_K_M (gutenberg's own) | the vectors gutenberg holds (RULE 1: nothing recomputed), checked against 64 re-embedded twins at cosine ≥ 0.99 | gutenberg's served encoder and instruction |
| **F16** | Qwen3-Embedding-4B, f16 (`vega-qwen3-embedding-4b-f16`, the Q4 YAML with the f16 file: the deployed flags and env) | embedded | gutenberg's instruction (`--query-like gutenberg`) |
| **bge** | bge-m3, F16 (`vega-bge-m3`, as wikidata-bge serves it) | embedded | as wikidata-bge serves them |

- Q4 against bge answers the operator's question.
- Q4 against F16 is what the 4 bits cost Qwen.
- F16 against bge is the two models with no quantization in between.

## The set

- **Queries:** gutenberg's own, judged by the WORK they name. These are the
  451 descriptive queries in nine languages and the 39 title queries of
  [one-language-per-work.md](one-language-per-work.md)
  (`tools/measure/one_language_queries.tsv`, `one_language_titles.tsv`). The
  works and their books come from `one_language_works.py`'s `works.json`.
- **Pool:** `tools/encoder_bakeoff.py pool` reads gutenberg's vector table,
  read-only. It takes:
  - every target work's books: the first 4 chunks and an even spread up to
    40 a book;
  - 20,000 chunks of other books, drawn by seed 0.

  No encoder chose a distractor, so no encoder's own confusions are favoured.
  A chunk of any book of the named work is relevant.
- **Measures:** MRR@10 (the rank of the first chunk of the right work), and
  hit@10 and hit@40. hit@40 is what reaches the reranker. Paired
  randomization tests come from the tool (Smucker et al. 2007).
- **What it cannot say:**
  - A pool is easier than all 10.48 M chunks. Its numbers rank encoders
    against each other; they are not the search's own figures.
  - It measures the vector leg alone. Word search, fusion and the reranker
    come after it on gutenberg.

## Cost

- **Card time:** a pool of ~30–40 k chunks at ~750 tokens is ~25–30 M tokens.
  - bge-m3: about 1 h.
  - Qwen f16: about 3–5 h.
  - Q4: minutes (the queries and 64 checks).
  - Day and night periods only.
- **Disk:** the f16 weights, 8.05 GB (`Qwen/Qwen3-Embedding-4B-GGUF`), and
  ~0.4 GB of vectors a Qwen row.

## Result

*Pending.*
