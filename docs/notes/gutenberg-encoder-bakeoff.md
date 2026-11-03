# harrier-oss-v1-0.6b against our encoders: gutenberg, wikidata, wikipedia

*Nemotron-3-Embed-1B repeats this exercise on the same sets (operator,
2026-11-02): [nemotron-embed.md](nemotron-embed.md).*

*Extended 2026-11-02 (the operator): "Harrier may be the model of reference
for all our non specialistic collections (gutenberg, wikipedia, wikidata,
wiki-gutenberg), compare the score also with bge-m3 and
wikipedia/wikidata". The plan is in "Three collections" below. wiki-gutenberg
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

## The verdict rule, fixed before the data (2026-11-02)

The operator: "If the numbers justify the new model over qwen3 and/or bge,
suspend the losing models' optimizations, work on the optimization of the
candidate model instead, then give me some numbers about the time to
re-embed with it." What "justify" means, per collection, written before any
score is read:

| incumbent | harrier is chosen when | why |
|---|---|---|
| **Qwen3-4B Q4_K_M** (gutenberg) | it is **not significantly worse** on MRR@10: harrier ≥ Qwen, or Qwen's lead has p ≥ 0.05 (paired randomization test) | harrier is ~7× smaller, its vectors 2.5× smaller (1,024 against 2,560), exact on this card, and its card time is several times less. Equal quality is worth it |
| **bge-m3 F16** (wikipedia, wikidata) | it is **significantly better** on MRR@10: harrier > bge-m3 with p < 0.05; **or**, when harrier is chosen for gutenberg, it is **not significantly worse** (amended 2026-11-02, below) | the same size class and the same 1,024 dimensions: a re-embed of tens of millions of chunks is paid for by quality alone, unless it also leaves ONE model for every general collection |

**Amended the same day, still before any score** (the operator: "Another
reason to adopt harrier over bge is, in case it wins over qwen3, to have only
one model loaded for generalistic collections, that is particularly important
with a small VRAM amount"). Where harrier replaces Qwen3-4B on gutenberg,
keeping bge-m3 for wikipedia and wikidata would hold two general encoders on
the card. harrier for all three holds one. The gains:
- one model's VRAM;
- one query embedding per question across the three collections;
- no swapping on a peer whose card holds one at a time.

So there harrier needs only to be not significantly worse than bge-m3. The
cost it accepts is the re-embed time, which comes with the numbers below.

hit@10 / recall@10 and the per-language rows are reported beside it, but
they do not decide.

**Meanwhile** (the operator: "Suspend wikipedia and wikidata embedding until
we have the numbers about the candidate model"), wikipedia and wikidata
embed nothing after the evaluation until the verdict. Their `embed` is set
to `off`, the previous value recorded and restored by whichever verdict
keeps the incumbent.

**After a verdict for harrier:**
- the losing models' optimisation work stops: lever 3 (wikipedia's bge-m3
  at 4 lanes), the bge-m3 lane tuning for wikidata, and gutenberg's Qwen3
  CPU reference, gate and near-exact work, as the case may be;
- the work moves to harrier: its LocalAI slots (`parallel`), batch and
  ubatch, and the collection's concurrency, each setting gated
  (reproducibility over loads, orders and batches, as invariant 12 asks)
  and timed on the collection's own chunks (`louise dev embedder-profile`);
- the re-embed time per collection = its chunks (wikipedia: the smart
  selection's count) ÷ the best gated chunks/s, over the hours the schedule
  leaves for embedding.

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

### gutenberg (2026-11-02 ~10:10): Qwen3-4B Q4_K_M stays

53,621 texts, 490 queries judged by work. Measured by München; harrier's
document vectors are G1's (parallel:1, its repro check exact).

| model | hit@1 | hit@10 | hit@40 | MRR@10 |
|---|---|---|---|---|
| Qwen3-4B Q4_K_M (stored vectors, gutenberg's instruction) | 0.7918 | 0.9776 | 0.9939 | **0.8630** |
| harrier-0.6b f16, gutenberg's instruction | 0.7367 | 0.9061 | 0.9449 | 0.7934 |
| harrier-0.6b f16, its own retrieval prompt | 0.7429 | 0.9041 | 0.9469 | 0.7989 |
| bge-m3 F16, as wikidata serves it (no query prefix; G3, 11:08) | 0.6939 | 0.9041 | 0.9653 | 0.7618 |

- **Qwen3 − harrier, MRR@10:**
  - +0.070 [+0.037, +0.103], p = 0.0001 (104 queries better, 57 worse);
  - against harrier's own prompt +0.064, p = 0.0001.
- **Qwen3 − bge-m3:** +0.101 [+0.069, +0.133], p = 0.0001.
- **harrier − bge-m3** (informative here; the rule's verdicts on bge-m3 are
  wikidata's and wikipedia's):
  - with its own prompt +0.037 [+0.002, +0.072], p = 0.040 (102 better, 71
    worse, 317 tied);
  - with gutenberg's instruction +0.032 [−0.003, +0.066], p = 0.075.
  - Two prompts were tried, so the better p is corrected for it (Holm):
    0.080, not significant. harrier leads bge-m3 here by ~0.03–0.04, not
    proven.
- **Verdict by the rule:** harrier is significantly worse, so Qwen3 stays on
  gutenberg. The "one model" clause does not apply. On wikipedia and
  wikidata, harrier must be significantly better than bge-m3.
- **Speed:** see "Speed, and what limits it" below. The 7.97 texts/s first
  reported here was an average over two windows, one of them beside a CPU
  job, sent one request at a time. With the card truly to itself harrier runs
  12.84 texts/s.
- The pool is easier than the whole collection, so these are not the
  search's own figures. A gap of 0.07 MRR@10 is well outside the noise.

### wikidata (2026-11-02 ~11:50): harrier wins against bge-m3

The 10-31 known-item set: 19,998 entities, 21,348 chunks, 400 queries (200
en, 200 it). Measured by München (W1); harrier through VeGa with its own
retrieval prompt, one text a request.

| encoder | MRR@10 | recall@1 | recall@10 |
|---|---|---|---|
| **harrier-0.6b f16** | **0.7951** | **0.7525** | **0.8800** |
| bge-m3 F16 (wikidata's encoder) | 0.6496 | 0.5750 | 0.7950 |
| jina-v3 torch / GGUF | 0.5100 / 0.5087 | 0.4200 / 0.4175 | 0.6925 |

- **harrier − bge-m3, MRR@10:** +0.146, p = 0.0001 (114 queries better, 22
  worse). **By the rule, harrier wins wikidata.**
- **By family (bge-m3 → harrier):**
  - person 0.743 → 0.934 (89 queries);
  - place 0.373 → 0.742 (50);
  - heritage 0.384 → 0.534 (80);
  - built 0.788 → 0.885 (43);
  - organization 0.917 → 0.954 (54);
  - work 0.651 → 0.718 (49);
  - scholarly 0.808 → 0.870 (18);
  - property 0.844 → 0.859 (16);
  - event 1.0 → 1.0 (1).
- **By language:** en 0.636 → 0.777; it 0.663 → 0.813.
- **Speed:** harrier 25.43 chunks/s at batch 1, one request in flight.
  bge-m3 was not timed on this set.
- **What follows (the operator's rule):** bge-m3's optimisations for wikidata
  stop; harrier's are measured (batches and requests in flight, through VeGa,
  since wikidata cuts by tokens); then the time to re-embed wikidata with it.

- **Chunks under harrier's tokenizer:** 1.0057× bge-m3's (München: 200,211
  records, stratified over the 9 embedded families, cut by
  `chunk_wikidata_text` at 1,024 tokens with each tokenizer; per family 0.945
  for property to 1.0159 for place). So 43.27 M chunks become ~43.52 M.
- **The operator chose to keep wikidata's bge-m3 embedding off** until the
  re-embed decision (2026-11-02, "Choice A"); the reason is written in
  wikidata.toml.

#### Which query the vector leg embeds (operator, 2026-11-02)

"Being the summary of wikidata in english only, does it make sense to
translate the query always to english for bge?"
- **Where the translation goes today:** wikidata's texts are English. The
  word search and the reranker already get the plan's English translation.
  Only the vector leg embeds the question as typed.
- **W1 says nothing either way.** bge-m3 scored Italian questions 0.663 and
  English 0.636, on different entities.
- **An English query does not close the gap to harrier by itself.** harrier
  leads on the English questions too (0.777 against 0.636), where no
  translation is involved.
- **Measured on the same questions** in the whole-search run (`pipeline_eval`):
  - `bge translated-only` is the proposal: the question in the set's
    language alone, so the Italian question by its English translation;
  - `bge per-language` keeps the typed question and adds the translation,
    fused, which hedges against a mistranslated name;
  - each is compared against `bge`, alone, fused with BM25 and reranked,
    and the same for harrier.
- **Its cost in production:** none for the translation, which the plan
  already makes, and one more query embedding.

#### The re-embed time (München, 2026-11-02 13:46–14:20, the card alone)

W1's 21,348 chunks through VeGa at batch 16. "In flight" counts the requests
really in flight (`EMBED_CONCURRENCY`); card busy is read from the PMU.

| arm | chunks/s | card busy | bit-identical to |
|---|---|---|---|
| harrier, 1 in flight | 43.29 | 87.4 % | W1's batch-1 vectors, 21,348 / 21,348 |
| harrier, 2 in flight | 50.28 | 96.6 % | the same, 21,348 / 21,348 |
| harrier, 4 in flight | **51.57** | 99.1 % | the same, 21,348 / 21,348 |
| bge-m3, 1 in flight | 57.71 | 84.2 % | the 10-31 production vectors, 21,348 / 21,348 |
| bge-m3, 4 in flight | **72.49** | 98.3 % | the same, 21,348 / 21,348 |

- **Every arm is exact.** harrier's batch 16 gives batch 1's bytes.
- **The card saturates at 2–4 in flight.** More slots add nothing at 99 %,
  and a `parallel:4` VeGa model cannot be stated (VeGa's contract).
- **harrier ÷ bge-m3, both saturating:** 51.57 / 72.49 = **0.711** on
  wikidata's ~100-token chunks.
- **Re-embed, card time alone:**
  - production's bge-m3 rate is 91.5 chunks/s (scheduling-log.md, lever 2
    AFTER), so harrier there would be ≈ 65.1;
  - 43.27 M × 1.0057 ≈ 43.52 M chunks;
  - 43.52 M ÷ 65.1 ≈ 669,000 s ≈ **186 h (7.7 days)**;
  - bge-m3 holds 1,440,632 vectors (3.3 %, the Lance table's row count,
    14:2x), so ~41.83 M are left: ≈ **127 h** at 91.5. harrier costs
    ≈ 59 h more, and drops what bge-m3 holds.
  The schedule's hours and the sharing with wikipedia and gutenberg come on
  top, for either model.
- **The decision (operator, 2026-11-02): the operator decides on the
  whole-search score, nothing automatic.** "Option C" first, then: "No
  automatic decision, I decide what to do." wikidata stays off until the
  operator says otherwise. München's `louise-wikidata-decide` was disarmed,
  or kept as report only.
- **The row reported first**, fixed before the data: the encoder fused with
  BM25, reranked over the top 100, the query translated (production's
  search: "5 results reranked over 50 results, more likely over 100").
  - Depth 50, the typed query and the translated-only legs are reported
    beside it.
  - The pair, as `pipeline_eval` names it: `bge + BM25 + rerank@100
    [translated]  vs  harrier + BM25 + rerank@100 [translated]`.
  - Its `mrr@10` is bge − harrier; a negative value with p@10 < 0.05 means
    harrier is significantly better.
- **Still owed before the decision:** wikidata's whole-search score
  (`wikidata_encoder_ab.py rerank`: BM25 fused, reranked at 10/40/50/100,
  translated and typed). It says whether the +0.146 survives the full
  search pipeline, or whether the reranker closes the gap. The operator
  asked that exact question.

### wikipedia (2026-11-02 ~12:40): bge-m3 stays

5,000 articles (2,500 en, 2,500 it), 18,783 chunks, 400 questions written by
the chat model (P2, the question's language alternating). Measured by
München (P3).

| encoder | MRR@10 | recall@1 | recall@10 | chunks/s |
|---|---|---|---|---|
| **bge-m3 F16** (wikipedia's recipe, batch 16, one request in flight) | **0.9277** | 0.900 | 0.9775 | 86.4 |
| harrier-0.6b f16 (VeGa, batch 1) | 0.8650 | 0.820 | 0.950 | 34.0 |

- **bge-m3 − harrier, MRR@10:** +0.063, p = 0.0001 (60 questions better, 23
  worse). **By the rule, bge-m3 stays on wikipedia.**
- **Where harrier loses:** Italian questions (0.938 → 0.786), mostly about
  English articles: across languages. It gains on English questions (0.917 →
  0.944) and Italian articles (0.907 → 0.933), and loses on English articles
  (0.949 → 0.796).
- wikipedia's `embed` is back to "all" at EVAL-OVER, and lever 3 is armed.

### The three verdicts

| collection | reference encoder | margin (MRR@10) |
|---|---|---|
| gutenberg | Qwen3-Embedding-4B Q4_K_M | +0.064 over harrier, +0.101 over bge-m3 |
| wikidata | **harrier-0.6b** (if re-embedded) | +0.146 over bge-m3 |
| wikipedia | bge-m3 | +0.063 over harrier |

No one model wins the general collections. A node that searches all three
holds three query encoders (about 2.5, 1.2 and 3.0 GB on the card), where
keeping bge-m3 on wikidata would mean two.

## Published benchmarks, against ours (checked 2026-11-02)

The operator: "I have read benchmarks in which harrier was scoring much better
than qwen3, please check".

| model | MTEB v2 multilingual, mean | MTEB retrieval |
|---|---|---|
| harrier-oss-v1-0.6b | 69.0 | 70.8 \* |
| Qwen3-Embedding-0.6B | 64.3 | 64.6 |
| **Qwen3-Embedding-4B (gutenberg's)** | **69.45** | **69.6** |
| harrier-oss-v1-27b | 74.3 | 78.3 \* |

\* A third-party aggregator; not checked against MTEB's own leaderboard. The
other figures are the model cards' own.

- **Where "much better" comes from.** Microsoft's table compares harrier-0.6b
  with Qwen3-Embedding-0.6B, the same size: +4.7. The other large gaps are
  harrier-27b's. Against the 4B that gutenberg runs, harrier-0.6b is level
  overall and ~1 point ahead on retrieval.
- **Why ours differs.** MTEB's retrieval tasks are mostly short questions over
  short passages. gutenberg's set finds a work from a description over long
  literary passages (903 tokens on average), and there the 4B leads even at
  4 bits (MRR@10 0.863 against 0.799, p = 0.0001).
- **Not the instruction.** harrier scored 0.793 and 0.799 with two
  instructions, against a gap of 0.07. Its MTEB numbers use a prompt per task
  (`mteb_v2_eval_prompts.json` in its repository), and none of them is
  book-finding.
- **wikidata and wikipedia** are closer to MTEB's shape; their rows above
  decide those two collections.

Sources:
- the [harrier-oss-v1-0.6b model card](https://huggingface.co/microsoft/harrier-oss-v1-0.6b) and its `mteb_v2_eval_prompts.json`;
- the [Qwen3-Embedding-4B model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B);
- the aggregators' [MTEB retrieval](https://www.madebyagents.com/benchmarks/mteb-retrieval) and [MTEB overall](https://www.madebyagents.com/benchmarks/mteb-overall) pages;
- [The Decoder](https://the-decoder.com/microsofts-bing-team-open-sources-harrier-embedding-model/) on the release;
- Microsoft's own post (blogs.bing.com) could not be read from the agent's network.

## Speed, and what limits it (2026-11-02)

harrier f16 on the B70 (`parallel:1`, threads 6, ubatch 8192, flash attention
on, `GGML_SYCL_ENABLE_DNN=0`), gutenberg's pool texts, ONE request in flight
(the tools' `EMBED_CONCURRENCY` is 1: the "concurrency 2/4/8" first written
for these rows never reached the server).

| run | beside it | texts/s | tokens/s | tokens a text |
|---|---|---|---|---|
| G1 through VeGa, 08:02–09:00 | gutenberg's CPU twin (4.2–5.4 cores) | 5.63 | 5,152 | 914 |
| G1 through VeGa, 09:14–09:52 | nothing (the twin docker-paused) | **12.84** | **10,931** | 851 |
| raw backend, same YAML but `backend`, ctx 32,768 and `parallel:4` | nothing | 12.27–12.63 | 11,303–11,354 | 923 |

- **VeGa costs 3–4 %** (~3 ms a text): on the test harness 3.7 ms a request,
  the wrapper's hop ~1.8 and the receipts GET ~1.8 (`louise dev
  embedder-profile --stages`, #207).
- **A CPU job beside the card halves it.** Every LocalAI backend is a child of
  one `local-ai` process, in one container whose cgroup has an 8-core quota
  (`cpu.max 800000 100000`) on the 12-thread host. The CPU twin's 4.2–5.4
  cores plus one busy SYCL backend's ~3.4 cross it, and the kernel then stops
  the whole container until the period ends, the card's feeder included. Over
  2 days 5 h, 14.3 % of LocalAI's periods were throttled (45,949 s).
  **The cap is 12 since 10:59:49** (operator: "Raise localai CPU cores cap to
  12 permanently"; STATUS.md 2026-11-02).
- **Requests in flight and batches:** measured on wikidata once harrier won it
  (batch 16, 1/2/4 in flight, 51.57 chunks/s at saturation, every arm
  exact): "The re-embed time" under wikidata above. The contention A/B
  (the twin at nice 0, then nice 19, and the quota) runs when the CPU
  reference resumes; its rows go in scheduling-log.md.
