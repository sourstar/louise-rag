# harrier-oss-v1-0.6b against our encoders: gutenberg, wikidata, wikipedia

*Extended 2026-11-02 (the operator): "Harrier may be the model of reference
for all our non specialistic collections (gutenberg, wikipedia, wikidata,
gutenberg-graph), compare the score also with bge-m3 and
wikipedia/wikidata". The plan is in "Three collections" below. gutenberg-graph
does not exist yet: it takes the reference model when it is created.*

*Planned 2026-11-02; measure only. The question changed twice the same
night:*
1. *"I am wondering if the quantization to 4 bits of qwen3 is putting it at
   the same level of bge at 16 bits" (three rows, then two);*
2. *"Cancel this test, I want to compare the current Gutenberg embedder with
   harrier-oss-v1-0.6b".*

*The bge-m3 row was stopped at 512 of 53,621 chunks; its shard is kept, not
scored. Results go in "Result" below.*

## The two rows

| row | encoder | document vectors | queries |
|---|---|---|---|
| **Q4** (done 2026-11-02 03:31) | Qwen3-Embedding-4B, Q4_K_M: gutenberg's own | the vectors gutenberg holds (RULE 1). 64 re-embedded twins: worst cosine 0.998926, median 1.000000 | gutenberg's served encoder and instruction |
| **harrier** | microsoft/harrier-oss-v1-0.6b, converted by us to an f16 GGUF from Microsoft's own safetensors (`vega-harrier-oss-v1-0.6b`: gutenberg's YAML, flags and env, with this file) | embedded | gutenberg's instruction (`--query-like gutenberg`): both models take `Instruct: <task>\nQuery: ` on queries and nothing on documents, so the same task text asks both |

**harrier-oss-v1-0.6b, as its card states it (published, not measured
here):**
- Microsoft, MIT licence.
- Qwen3 architecture, 0.6 B parameters, 28 layers.
- Last-token pooling, L2-normalised, 1,024 dimensions, 32,768 tokens.
- Multilingual MTEB v2: 69.0, distilled from larger embedding models.
- Weights: bf16 safetensors, 1.19 GB.

**Before its row counts:**
- **Conversion check.** The GGUF on the card must reproduce the model as
  sentence-transformers runs it on the CPU: 32 pool chunks and 16 instructed
  queries, every cosine ≥ 0.99. Last-token pooling reads the wrong token if
  the end-of-text token is not appended as the model's tokenizer does. That
  fault passes every other check and makes the row wrong.
- **Invariant 12.** `tools/vega_repro_check.py` on the model, once.

## Three collections

| collection | its encoder today | the set | rows |
|---|---|---|---|
| gutenberg | Qwen3-4B Q4_K_M | the pool below: 490 queries judged by work | Q4 (stored, done); harrier with gutenberg's instruction AND with its own retrieval prompt (the query side only, the documents shared); bge-m3 F16 (the stopped row resumed, as wikidata serves it) |
| wikidata | bge-m3 F16 | the known-item set of 2026-10-31 (wikidata-family-slices.md §10): 400 questions, en and it, over 21,348 chunks of 20,000 entities | bge-m3 (its vectors from 10-31, kept); harrier (`wikidata_encoder_ab.py embed --encoder harrier`) |
| wikipedia | bge-m3 F16 | a NEW known-item set the same way (`sample-table`): 5,000 articles, en and it, their first 4 chunks from its own vector table, 400 questions by the chat model without the title (`queries --prompt wikipedia`) | bge-m3, harrier |

- **harrier's queries** take its card's retrieval prompt (`web_search_query`:
  "Instruct: Given a web search query, retrieve relevant passages that
  answer the query\nQuery: "). bge-m3 takes none, as served. On gutenberg,
  harrier also gets gutenberg's own instruction, for a like-for-like row
  against Qwen.
- **The verdict** for each collection is MRR@10 with a paired randomization
  test, every pair. Speed (chunks a second) and vector size (1,024 for
  both harrier and bge-m3, 2,560 for Qwen3-4B) are stated beside it.
- **Normal embedding stays suspended** for the whole evaluation (the
  operator: "Suspend normal embedding during harrier evaluation").

## The gutenberg set

- **Queries:** gutenberg's own, judged by the WORK they name. These are the
  451 descriptive queries in nine languages and the 39 title queries of
  [one-language-per-work.md](one-language-per-work.md), with `works.json`
  regenerated 2026-11-02 from the 2026-10-09 records (79,562 books, 6,735
  works).
- **Pool:** `tools/encoder_bakeoff.py pool`, seed 0, with 53,621 chunks:
  - 33,621 from the 955 target books (32 books have no rows);
  - 20,000 random chunks of other books.

  All 490 queries have a relevant chunk.
- **Measures:**
  - MRR@10: the rank of the first chunk of the right work.
  - hit@10, and hit@40, which is what reaches the reranker.
  - Paired randomization tests.
- **What it cannot say:**
  - A pool is easier than all 10.48 M chunks. The rows compare the two
    encoders with each other.
  - It measures the vector leg alone, before word search, fusion and the
    reranker.

## Cost

- **Card time, the card to itself:**
  - gutenberg: harrier and bge-m3, about 1 h each (~40 M tokens each);
  - wikidata: harrier, about 15 min;
  - wikipedia: the questions by the chat model, then bge-m3 and harrier, about
    30 min.
  - Day and night periods only.
- **Disk:** about 1.2 GB of GGUF and 2.4 GB of safetensors while converting,
  plus ~0.2 GB of vectors.

## Result

*Pending.*
