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
  order changes on every query, unmeasured: there is no labelled pubmed set.

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

### Fixed in this run

A broad exclusion was refused (§5.7: the postings hold values of at most
20 000 records) with "'lang' = 'en' admits 63193 records". For
`{"not": "en"}` that reads as if English had been asked for. It now says
"excluding 'lang' = 'en' needs its 63193 records"
(`app/ask/filters.py`, `tests/ask/test_facets.py`). The campaign test passes
once the node runs it.
