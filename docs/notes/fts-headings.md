# The word-match leg and chunk headings (2026-10-23)

**Found** (München, the node): "white whale obsession captain" put Moby-Dick's
first chunk at #291. `fts.search` scores `Should(body) + Should(chapter)`; a
heading is a few tokens, so BM25's length normalisation makes one heading term
worth ~10 points, more than a body full of the words.

## Literature (the operator: "check if there is already some paper")

- **Robertson, Zaragoza & Taylor, "Simple BM25 extension to multiple weighted
  fields", CIKM 2004** ([doi 10.1145/1031171.1031181](https://doi.org/10.1145/1031171.1031181)):
  summing per-field BM25 scores "can lead to poor performance by breaking the
  carefully constructed non-linear saturation of term frequency"; BM25F weights
  each field's term frequency, sums them, and saturates once. Exactly our
  `Should(body) + Should(chapter)`.
- **Cross-field document frequency** (Elasticsearch `cross_fields`;
  [softwaredoug, "BM25F from scratch", 2025](https://softwaredoug.com/blog/2025/09/18/bm25f-from-scratch)):
  a term rare in the heading field gets a high field IDF; blending df across
  fields (max) removes the bias.
- **Weights are tuned on held-out labelled queries**
  ([Pérez-Iglesias et al., BM25F in Lucene, arXiv 0911.5046](https://arxiv.org/abs/0911.5046)):
  our campaign set.
- **Headings prepended to chunks before indexing** is current RAG practice:
  Anthropic's [Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)
  prepends 50–100 tokens of context to each chunk before embedding AND before
  the BM25 index (failed retrievals −35 % with contextual embeddings, −49 %
  with contextual BM25 added, −67 % with reranking); the
  [DAPR benchmark (arXiv 2305.13915)](https://arxiv.org/abs/2305.13915) tests
  title-prepending with BM25 hybrids.
- **A control to copy**: an October 2026 preprint
  ([arXiv 2610.10170](https://arxiv.org/abs/2610.10170), Notre Dame) shows any
  prepended text perturbs a chunk's representation and adds a
  **shuffled-heading placebo** of the same length to separate real structural
  gains from that effect.
- tantivy 0.26 ships per-field BM25 and query boosts, not a BM25F scorer
  ([MG4J's BM25FScorer](https://mg4j.di.unimi.it/docs/it/unimi/di/mg4j/search/score/BM25FScorer.html)
  is an example of one built in).

### The published numbers

| Source | Setting | Result |
|---|---|---|
| Anthropic, Contextual Retrieval (vendor blog, not peer reviewed) | 1 − recall@20, avg of 4 corpora | embeddings 5.7 % → contextual embeddings 3.7 % → + contextual BM25 2.9 % → + rerank 1.9 %. No BM25-alone row. |
| arXiv 2610.10170 (dense only, cov-nDCG@10) | Wikipedia FA / QASPER | structure-cut chunks + real heading path beat contextualized fixed windows +0.022 / +0.012; real heading beats shuffled placebo +0.010 / +0.016; where the text is CUT is worth 2–4× the heading; LLM contextualization ranks below bare structure-cut chunks. Effects small (dz 0.06–0.11), Holm-significant. |
| DAPR, arXiv 2305.13915 | 5 doc-aware passage sets | 53.5 % of SoTA retriever errors come from missing document context; title-prepending helps hard queries, hurts overall; BM25 hybrid best on the mixed set. |
| mFAR, arXiv 2410.20056 | STaRK (Amazon, MAG, Prime) | per-field BM25 does NOT consistently beat whole-document BM25 (Amazon, MAG); they cite BM25F's saturation argument as the reason. Gains come from fields + dense + query-conditioned weights. |
| BM25F, Robertson/Zaragoza/Taylor CIKM 2004 | Reuters, TREC .gov | "beneficial"; the tables could not be read here (host unreachable). A later study: BM25F with UNIFORM weights is worse than plain BM25 — weights must be tuned. |

None measures our case (BM25, short heading vs body, mixed-language books).
Our boost table below is the only number on it.

## The forms, in our code

| form | commit | what it is |
|---|---|---|
| `CHAPTER_BOOST` | eabc9e6, REMOVED | scaled the heading's separately saturated score: the sum the 2004 paper shows breaks BM25. Removed (operator, 2026-10-23: "do not reinvent the wheel") |
| `HEADING_IN_BODY = k` | 9db4e3a | BM25F itself, heading weight k, body weight 1: with integer weights the paper's weighted tf and length ARE the heading written k times plus the body; a test checks tantivy's score against the paper's formula to 1e-4 |
| BM25F with float weights / per-field b | — | Lucene `CombinedFieldQuery`, Elasticsearch `combined_fields`; not offered by tantivy 0.26 |

## Measured (fts leg alone, English labelled queries, the node)

| form | titles MRR / R@10 / R@50 | descriptive MRR / R@10 / R@50 | Moby-Dick |
|---|---|---|---|
| CHAPTER_BOOST 1.0 (today) | 0.114 / 0.414 / 0.897 | 0.077 / 0.133 / 0.440 | 291 |
| 0.25 | 0.124 / 0.379 / 0.862 | 0.123 / 0.253 / 0.507 | 14 |
| 0.1 | 0.160 / 0.310 / 0.862 | 0.141 / 0.273 / 0.513 | 1 |
| **HEADING_IN_BODY 1** | 0.208 / 0.448 / 0.966 | 0.143 / 0.273 / 0.533 | 1 |
| **HEADING_IN_BODY 2** | 0.383 / 0.690 / 1.000 | 0.140 / 0.287 / 0.547 | 1 |
| **HEADING_IN_BODY 3** | **0.528 / 0.793 / 1.000** | 0.139 / **0.293 / 0.553** | 1 |

The boost rows are kept as the record of why; they are not candidates. The
choice is BM25F's weight k, tuned on the labelled queries as the literature
tunes it.

**How the k rows were measured** (München, 2026-10-23/24 night): gutenberg's
whole en group rebuilt twice into scratch fts_dirs on tank (not snapshotted or
replicated) on 9db4e3a's tree, `fts.HEADING_IN_BODY` = 1 and = 2, each in a
container capped at 2 cpus, nice 19: 63,203 documents, 8,506,767 chunks, 0
skipped, 16,617 s each (3.8 docs/s, the two in parallel beside run 5), 30 GB
each (the live en index is 27 GB). Then `fts.search_records("en", q, 50)` on
the same 29 title, 150 descriptive and 3 pair English queries as the boost rows
(`tools/measure/heading_build.py`, answers from works.json). The boost rows ran
on the live index; same queries, same answers, the corpus read at the same
snapshot.

**Reading.** k = 2 wins both sets on the leg: titles MRR 0.114 → 0.383, R@10
0.414 → 0.690, R@50 0.897 → 1.000; descriptive MRR 0.077 → 0.140, R@10 0.133 →
0.287. Unlike the boost, folding the heading into the body does NOT cost
titles: the heading's words now add term frequency inside one saturation
instead of a separately saturated short field. k = 1 helps less on titles
(0.208). The shuffled-heading placebo (arXiv 2610.10170) is not needed to
separate signal from length: k = 2 over k = 1 is a large margin, and the gain
is in titles, where the heading is the signal.

**k = 3** (2026-10-24, the same build alone at 2 cpus: 63,206 documents — three
more than the k = 1/2 builds read the day before — 8,507,047 chunks, 0
skipped, 10,821 s, 30 GB): titles rise again, MRR 0.383 → 0.528, R@10 0.690 →
0.793; descriptive is flat (MRR 0.140 → 0.139, R@10 0.287 → 0.293). The leg
alone has not yet found where titles stop gaining; the descriptive set says
the heading weight costs nothing there up to 3. Next: k = 0/2/3 through the
full search, reranker on, on the whole labelled set
(`tools/measure/heading_full_search.py`, the night of 2026-10-24), before any
FTS_VERSION rebuild.

Deploy rule: the full search (reranker on) on the whole labelled set, no loss
on titles or descriptive. A HEADING_IN_BODY choice rebuilds the text indexes
(a new FTS_VERSION), collection by collection: CPU, no GPU, no re-embedding.
