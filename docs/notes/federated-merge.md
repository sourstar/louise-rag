# Merging several collections' results (2026-10-31)

*Operator, 2026-10-31: B — merge on each reranker's score calibrated into a
probability of relevance — "keep A in the list of the potential
improvements to be evaluated". 05 §5.6 and 07 §7.8 are amended. The text of
CLAUDE.md invariant 4 ("Ranked lists from corpus legs ALWAYS go through
RRF") is the operator's to change.*

## 1. What it replaces

Each collection's list is reranked on its own (05 §5.6), and the lists were
then merged by reciprocal rank (`retrieval._merge_rrf_federated`). Rank
fusion gives every collection's first record the same weight, however weak
it is. A collection with nothing relevant still places its best weak
records at positions 2, 4, 6… of the merged list: a medical question
placed normattiva's best record second. Within a collection the reranker
sorts that out; across collections nothing did.

## 2. What the studies say

- **Choosing collections for a query** is the core of federated search:
  - CORI (Callan, Lu & Croft, 1995) and ReDDE (Si & Callan, 2003) rank the
    collections themselves;
  - "vertical selection" in aggregated search (Arguello, Diaz, Callan &
    Crespo, SIGIR 2009);
  - the TREC FedWeb tracks, 2013–14, measured resource selection and
    results merging apart.
  
  Equal weights are the baseline these improve on. Arguello found that
  evidence from the query (A) and from the collections' own results (B)
  combined do better than either alone.
- **Merging across scorers** needs a common scale. Raw scores of different
  models are not one (Fox & Shaw, 1994; Montague & Aslam, 2001). Ranks are,
  and RRF (Cormack, Clarke & Büttcher, 2009) was designed for runs over ONE
  collection, where every run has relevant records to rank. A probability
  of relevance is a common scale. Platt scaling (Platt, 1999) gives one from
  a classifier's score with two parameters; isotonic regression (Zadrozny &
  Elkan, 2002) is the non-parametric alternative once there is data enough.
- **Judging at scale:** LLM relevance labels reproduce TREC assessors'
  system rankings (Thomas et al., 2023; UMBRELA, Upadhyay et al., 2024).
  Paired significance by randomization (Smucker, Allan & Carterette, 2007).

## 3. B, as built

- `[reranker] calibration = {a, b, rerank_model, judged, set}` in a
  collection file (07 §7.8): P(relevant | score) = 1/(1 + e^-(a·score + b)),
  fitted on judged candidates of that collection, under the reranker it
  names.
- `federated_search` merges on that probability when EVERY list has a
  calibration for the reranker that scored it and was reranked this time
  (`retrieval._merge_by_relevance`). Otherwise it merges by rank as before.
  The result says `merged_by` and, under `merge_withheld`, why a configured
  calibration was not used.
- **Each list keeps its own order.** A record weighs the best probability at
  or below it in its own list. So the catalogue's first record, a
  language's slots and the tail the reranker did not read stay where their
  collection put them.
- Nothing changes until a collection file carries a calibration.

## 4. The gap: three collections rerank nothing

On the node today (München, 2026-10-31, resolved through
`collections.reranker_for`):

| Collection | Reranker |
|---|---|
| gutenberg | louise-rerank-bge |
| pubmed | louise-rerank-medcpt |
| wikipedia, normattiva, wikidata | none (`RERANK_URL` is empty) |

A list with no reranker has no score to calibrate, so any search that
includes one of the three stays merged by rank.

| Choice | Pros | Cons |
|---|---|---|
| Keep them unreranked (today) | No new card time per query | B applies only to searches over gutenberg and pubmed alone |
| Give them louise-rerank-bge (multilingual, already loaded for gutenberg: no new VRAM) | B applies to every search. It may also improve each collection's own ranking: gutenberg's descriptive queries went from MRR 0.121 to 0.223 | Card time on every query: wikipedia measured 0.36–0.67 s with a reranker earlier; normattiva and wikidata are unmeasured. A reranker can also hurt a list: gutenberg's titles fell from 0.738 to 0.578 until the catalogue-first rule |

The measurement below decides it per collection: its `reranked` pass lends
gutenberg's reranker to the three, and `score` reports each list's own
MRR@10 before and after.

## 5. The measurement (`tools/measure/fusion_calibration.py`)

On the node, in the web container:

1. **Collect the queries:**
   - `collect --from-query-log lists.jsonl --pass today`
   - `collect --from-query-log lists.jsonl --pass reranked --rerank-like gutenberg`

   These are the 243 distinct queries of the query log (search and ask), each
   over every searchable collection. Each collection's list is kept as the
   merge sees it.
2. **`judge lists.jsonl judged.jsonl`:** every record in the top 10 of any
   list, graded 0–3 by the chat model (louise-chat-qwen3-8b) with the
   UMBRELA prompt. That is about 50–70 records a query.
   - The labels are stored, and they are the set: the judge is not held to
     invariant 12, but every number reproduces from the files.
   - Judging each list's top 10 suffices: both merges keep each list's
     order, so any merged top 10 is made of prefixes no longer than 10.
3. **Check the judge:**
   - `sample judged.jsonl lists.jsonl second.jsonl -n 300`, graded by a
     second judge (München, blind to the first);
   - then `agree judged.jsonl second.jsonl`: Cohen's kappa, and agreement on
     relevant (grade ≥ 2).
   
   A weak kappa means a stronger judge (one of the models tested tonight)
   before any number is used.
4. **`score lists.jsonl judged.jsonl`:**
   - MRR@10 and nDCG@10 for three merges, over five query folds (calibration
     fitted on four, measured on the fifth):
     - today's merge;
     - by rank with every list reranked;
     - B.
   - The paired randomization p, against today.
   - Each calibration's held-out Brier score and ECE.
   - Each list's own MRR before and after a reranker.
   - The TOML line per collection, fitted on every query.

**What switches B on:** B is better than today on both MRR@10 and nDCG@10,
p < 0.05, and the judge passes step 3. Then München writes the calibration
lines. A collection gains a reranker only if its own list is not worse with
it, and its added latency goes to the operator with the numbers.

**Cost:** about 243 × 2 searches, including about 243 × 3 reranks that do
not happen today, then about 12–17 k judgments on the card's chat model.
Hours, so it is scheduled around the embedding. `collect --limit 120` and
the judge's resume halve it.

## 6. A, on the list to evaluate

A means weighting collections by what the query is about, for example a
medical question giving pubmed more weight. It is measured on the same
judged set once B's numbers are in:
- a query classifier (the chat model, or the query's similarity to each
  collection's description) gives each collection a prior;
- the prior multiplies B's probabilities;
- it is never 0, so a misread query does not hide a collection.

Pros: the studies' best results combine A with B. Cons: a classification
per query (time on the card); a misclassification lowers a whole
collection; questions that span domains, for example medical liability
(normattiva and pubmed), need both collections.
