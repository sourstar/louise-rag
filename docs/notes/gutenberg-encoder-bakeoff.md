# gutenberg's encoder against harrier-oss-v1-0.6b: a measurement

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

## The set

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

- **Card time:** about 1 h for the harrier corpus side (~40 M tokens, a
  0.6 B model, about bge-m3's speed), in day or night periods outside the
  step-1 windows.
- **Disk:** about 1.2 GB of GGUF and 2.4 GB of safetensors while converting,
  plus ~0.2 GB of vectors.

## Result

*Pending.*
