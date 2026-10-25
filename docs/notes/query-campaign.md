# The query campaign

Live acceptance tests of what each kind of query promises (docs/05, 02 §2.10),
in `tests_campaign/` (its README says how to run them). Operator, 2026-10-20:
"ensure that the query does what it promises".

## Run 1 — 2026-10-20, gutenberg + gutenberg-book-editions, node at 678450f

40 tests: **33 pass, 7 fail** (213 s). One more failed on the first run and
passed on the second: "a captain obsessed with hunting a white whale". That
first run's detail was lost, so it is not reproduced.

What holds: the answer's shape and fields, `limit`, determinism of one query
twice, one hit per work, the §5.3 edition rule, language filters, exclusions,
an empty filter as an answer, undeclared facets refused, facts beside the
lists, annotations collections and unknown collections refused, nothing
withheld silently, every claim field on hits, the editions as the whole work,
traverse (by relation, depth 2), lookup unresolved, and chat cited by
returned uids only.

What fails is **retrieval**. Every failing book is held, indexed and embedded
(`fts_done = embed_done = 1`):

| query | where the work is |
|---|---|
| "War and Peace" | 2600 absent from the top 100 |
| "Alice's Adventures in Wonderland" | no English edition among the 50 candidates; the German one at 4, behind "Literature for Children" and two histories of Project Gutenberg |
| "Pride and Prejudice" | none of the five English editions among the candidates; the Finnish one (45186) at 1, so §5.3 rightly shows it |
| "a man wakes up transformed into a giant insect" | 5200 at vector rank 26, fused rank 51 |
| "a scientist creates a living creature from dead bodies" | 84 not in the top 100; another edition at 73 |

### Causes, measured

1. **Title matches are thrown away.** The catalog leg (title, authors,
   subjects; `app/catalog.py`) runs on every search (`timings.catalog`).
   `/api/ask/search` never uses its hits: they reach only the legacy LLM
   prompt (`retrieval.py`, "[catalog matches]"). docs/05 §5.1 names two
   legs, text and vector, and no catalog leg. The text leg indexes chunk text
   alone (`fts.py` has no title field). So a title reaches a book only through
   the book's own text, and anthologies and catalogues that quote the title
   beat the book.
2. **Gutenberg's queries are encoded without Qwen3's instruction.** The Codex
   has `space.query.instruction: null`, and Qwen3-Embedding expects
   `Instruct: <task>\nQuery: `. Vector leg, top 50 records, measured in the
   web container (`embeddings.search_records`):

   | query | bare: work at / fragments in top 10 | with instruction |
   |---|---|---|
   | giant insect | 26 / 7 | 41 / 1 |
   | living creature from dead bodies | – / 5 | 45 / 0 |
   | white whale | 11 / 10 | **1** / 0 |
   | War and Peace | – / 10 | 45 / 2 |
   | Alice's Adventures in Wonderland | – / 10 | 30 / 3 |
   | rabbit hole | 2 / 0 | 4 / 0 |

   A "fragment" is a chunk under 200 characters. Bare, the top hits are
   11–65 characters ("You are a giant?"). The instruction removes that
   attraction; it does not make titles findable (cause 1).
3. **Fragment chunks exist.** `fixed` cuts per chapter with no minimum, so a
   one-line epub section is a chunk of its own, with no title or chapter
   context. Re-chunking means re-embedding gutenberg; not proposed.

The instruction is inside `space_id` (02 §2.4, 09 §9.3). Adding it is a new
Codex, and the document vectors carry unchanged, since only the query side
changes (RULE 1). The calibration's `query_vectors` must be recomputed with
the instruction prefixed. Checked for the other collections: pubmed (MedCPT)
and wikidata (jina-v3) have their own query encoders; normattiva has
`query: `. Only gutenberg lacks it.

## Run 2 — the two fixes measured before deploy (2026-10-20, evening)

**Cause 1, the catalogue as a text variant (2efe665).** Replayed offline on
the 2026-10-09 captured legs through the product's `_merge_rrf`, with the
catalogue's records exactly as 2efe665's `_catalog_variant` returned them on
the node. It was captured in a read-only container with 2efe665's app
mounted (`tools/measure/catalog_rescore.py`). Pre-rerank, measured:

| | titles (39) MRR | R@1 | R@10 | R@50 | descriptive (451) MRR | R@10 |
|---|---:|---:|---:|---:|---:|---:|
| deployed fusion (reproduces 05's numbers) | 0.501 | 0.359 | 0.744 | 0.872 | 0.219 | 0.397 |
| + catalogue variant | **0.752** | **0.641** | **0.897** | 0.949 | 0.219 | 0.397 |

- Titles: 13 better, 5 worse (sign p = 0.10). The five worse slip one or two
  places, behind a book about the work whose catalogue entry ranks first
  ("Don Quixote Cervantes" 13 → 15, "Gulliver's Travels Swift" 1 → 2).
- Descriptive: identical on all 451. The catalogue requires every word of the
  query in title, creators or subjects, and none matched.
- Cost, measured cold in a fresh container: gutenberg median 52 ms, max
  501 ms (catalogue plus variant). On pubmed the variant alone adds 160–758 ms
  a query, 50 per-record `fts.search` calls over 40 M records. Every medical
  query filled all 50 catalogue slots through MeSH subjects, so pubmed's
  order changes on every query. **That change stays unmeasured until a
  labelled pubmed set exists.**
- 1f800e8 asks one text query per language group. Re-captured on the node
  with its code, pubmed's variant costs 136–492 ms cold (median 235) and
  23–42 ms warm (median 36); the catalogue's own lookup is median 362 ms
  cold, 40 ms warm. Gutenberg's variant: median 28 ms. The records and
  their order are identical to 2efe665's in 498 of 498 queries. The chunk
  carrying a record differs in 18 (gutenberg titles), among chunks of
  equal score. The per-record `limit=1` had taken tantivy's arbitrary
  pick, and the batched answer sorts the tie by chunk id (`fts.search`).

**Cause 2, the instruction (A1f).** The vector leg alone, top 100 records,
measured on the node (`tools/measure/query_instruction_probe.py`), the 39
titles and the first 52 descriptive queries. The probe was stopped there
when the node swapped Qwen3 out for normattiva's embedding:

| form | titles MRR | R@10 | R@50 | fragments in top 10 | descriptive MRR | R@10 | R@50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| bare (deployed) | 0.209 | 0.282 | 0.436 | 9.95 | 0.332 | 0.577 | 0.788 |
| Qwen3 default: "Given a web search query, retrieve relevant passages that answer the query" | **0.294** | **0.795** | **1.000** | 2.85 | 0.417 | 0.788 | 0.923 |
| library: "Given a search for a book by its title, its author or a description of its content, retrieve passages of that book" | 0.251 | 0.718 | 1.000 | 0.97 | 0.433 | 0.808 | 0.962 |

Default vs bare: titles 28 better and 7 worse, descriptive 29 and 15.
Library vs default: titles 12 better and 24 worse, descriptive 17 and 14.
Chosen: the model's documented default, in its own format
(`Instruct: <task>\nQuery:`). It is better on titles and level on
descriptions, and no wording of ours is pinned.

## Run 3 — the API's as-typed path, and the catalogue as a leg (2026-10-20, night)

Deployed: f4cee56 at 20:48 (the catalogue as a text variant), 79098b6 at
21:02, and the A1f twin of gutenberg live at 21:45:40 (sha256:d913cceb…,
Qwen3's default query instruction). Campaign on f4cee56: 37 of 40. The
three failures were "War and Peace" and the two descriptions.

**Why "War and Peace" still failed.** The API runs the query as typed in
all 17 language groups, each a variant of the text leg. Seventeen records
tie at best rank 1, seventeen at 2, seventeen at 3, so the catalogue's
rank 4 (2600) sat at text position 57, fused rank 113. The 10-09 captured
legs were the Ask page's translated variants, so the replay of run 2 never
saw this flood. a46042b makes the catalogue a leg of its own.

**Measured on the API's own path.** In the live web process, after the
re-pin: translate false, limit 10, depth 50. The lists entering
`_merge_rrf` were captured and the search stopped there, before the reranker
and any write. All 491 queries had their vector leg. They were replayed
under each tree (`tools/measure/astyped_rescore.py`). Pre-rerank:

| | titles (40) MRR | R@1 | R@10 | R@50 | descriptive (451) MRR | R@1 | R@10 | R@50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 79098b6, catalogue a variant | 0.595 | 0.475 | 0.850 | 0.975 | 0.328 | 0.195 | 0.570 | 0.812 |
| **a46042b, catalogue a leg** | **0.738** | **0.600** | **0.950** | **1.000** | 0.328 | 0.195 | 0.570 | 0.812 |

- Titles: 17 better, 5 worse, each worse by one or two places (1 → 2,
  1 → 3, 5 → 6). "War and Peace": absent from the top 50 → 4. "Pride and
  Prejudice" 3 → 3, "Alice's Adventures in Wonderland" 1 → 1.
- Descriptive: identical, no catalogue hit.
- Against run 2's captured legs (0.219 / 0.397 descriptive, a different
  path and the bare vector leg), the descriptive numbers here include the
  instruction. A same-path comparison of bare against instructed is the
  probe of run 2.

**After the reranker: a regression on titles (0a2c6ef live, 22:41).** The
campaign: 33 of 40. The 40 labelled titles through the API (limit 10, one
hit per work): MRR **0.579**, R@1 0.425, R@3 0.700, R@10 0.925, against
0.738 / 0.600 / 0.950 at R@10 for the same path before the reranker. The
instructed vector leg now brings books ABOUT a work into the candidates
(biographies, criticism, anthologies), and the reranker prefers their
passages to the work's own. "Moby Dick" shows a Melville biography,
Harper's and a critic. "Pride and Prejudice" shows Famous Reviews and two
Austen studies. "War and Peace" goes from fused 4 to shown 10, behind four
books on Tolstoy. The run-2 reranker measurement (titles 0.660, 2026-10-10)
was on another path and the bare vector leg. Open for decision.

The two Spanish edition tests that failed are the §5.3 rule at work. For
"Don Quijote de la Mancha, el ingenioso hidalgo" the plan reads no
language: the function-word guess abstains against the 56 languages held,
Catalan and Galician among them. With the language unknown, English is
preferred, and an English edition (77095) now ranks. A limitation of the
guess ("ingenioso" and "hidalgo" are Spanish-only words, but it reads
function words only), not of the grouping.

### Fixed in this run

A broad exclusion was refused (§5.7: the postings hold values of at most
20 000 records) with "'lang' = 'en' admits 63193 records". For
`{"not": "en"}` that reads as if English had been asked for. It now says
"excluding 'lang' = 'en' needs its 63193 records"
(`app/ask/filters.py`, `tests/ask/test_facets.py`). The campaign test passes
once the node runs it.

## Run 4 — three ways to keep the reranker from burying titles (2026-10-21, night)

The operator's rule: adopt the variant that brings titles back to at least
0.738 MRR and 0.950 R@10 after the reranker, without lowering the
descriptive queries after the reranker; (b) first, (a) only if (b) loses
descriptive. `tools/measure/rerank_variants.py` replays each query through
the product's own `results.search` on the node, once per variant, with the
reranker on and measured bit-exact (DNN=0, `parallel:1`, `cache_ram:0`).
(b) is the product function itself, `retrieval._rerank_order` with
`RERANK_FUSE = True`.

| Titles (40), after the reranker | MRR | R@1 | R@3 | R@10 |
|---|---:|---:|---:|---:|
| base (the reranker's order) | 0.578 | 0.425 | 0.700 | 0.925 |
| (a) the catalogue's first record first | **0.782** | 0.700 | 0.825 | 0.975 |
| (b) RRF of the reranker's and the fused order | 0.720 | 0.575 | 0.825 | 1.000 |
| (c) chunks cut to 300 characters | 0.625 | 0.450 | 0.725 | 1.000 |

| Descriptive (the 347 of 451 reranked under all three) | MRR | R@1 | R@3 | R@10 |
|---|---:|---:|---:|---:|
| base | 0.411 | 0.308 | 0.467 | 0.671 |
| (a) | 0.411 | 0.308 | 0.467 | 0.671 |
| (b) | 0.378 | 0.256 | 0.441 | 0.692 |

**(a) is adopted** (902ceb4, `retrieval.CATALOG_FIRST`, 05 §5.6). It clears
the titles bar, and on the 376 descriptive queries the reranker answered it
moved no rank at all: a descriptive query's catalogue leg rarely ranks the
book first, and when it does the reranker already agrees. (b) misses the
titles MRR and lowers the descriptive MRR by 0.033.

The reranker gave no answer within its budget for 75 of the 451 descriptive
queries, and (b)'s second call failed for 29 more. Those are left out of
the tables. The failures grew over the run (1, 2, 5, 5, 9, 14, 13, 18, 8 per
50 queries), and from 00:10 the card was shared with gutenberg's embedding
(set to p2p in the Library then). This is a reranker budget under load, not
a fault of any variant. Where the reranker gives no answer, the product
shows the fused order, in which the catalogue is already one of the legs.

## The entity boost against 05 §5.2 — measured on TREC Precision Medicine (prepared 2026-10-25)

`retrieval.ENTITY_BOOST` adds 1/60 (one virtual rank-1 vote) to the fused
score of a candidate tagged with an entity the query names. 05 §5.2 says "a
filter restricts; nothing boosts". The spec wins until it is changed, so
this is a known defect (task #33, found by PR #4). Either the boost goes, or
05 §5.2 is amended with its weight pinned. That choice waits on a number,
because no labelled pubmed set existed (Run 2: "stays unmeasured until a
labelled pubmed set exists").

**The set is a standard one:** TREC Precision Medicine 2017 and 2018,
scientific abstracts. Its topics name a disease and a gene or variant, and
NIST assessors judged the PubMed abstracts 0/1/2, independently of
Louise's tagging. The files are TREC's own, as ir_datasets catalogues
them; the node fetches them, since this sandbox cannot reach
trec.nist.gov. Judged documents that are not PubMed (AACR/ASCO) are left
out. The query is the participants' common baseline: disease, then gene.

**The harness:** `tools/measure/entity_boost_trecpm.py` (prepare anywhere;
search on the node with `DATA` prepended, as the other harnesses do). It
uses the deployed path with the reranker on, and runs each topic twice,
with the boost as deployed and with 0, the order alternating.

**Metrics, per topic, at 10:**
- NDCG (trec_eval's, unjudged counted as 0) and the condensed NDCG (Sakai
  2007: unjudged documents removed, because Louise contributed nothing to
  the pool);
- P, RR, the judged share, R@50.

A topic counts only when both arms were reranked. Topics where the boost
applied (a candidate tagged with a named entity) are reported apart: only
there can the arms differ.

**Reading it:**
- no loss without the boost → drop it and test_entity_boost.py becomes its
  negative;
- a gain with it → amend 05 §5.2 with the weight pinned in the Codex.
Either way it is a fusion change, so the numbers go in its commit.
