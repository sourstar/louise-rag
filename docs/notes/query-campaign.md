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

### Fixed in this run

A broad exclusion was refused (§5.7: the postings hold values of at most
20 000 records) with "'lang' = 'en' admits 63193 records". For
`{"not": "en"}` that reads as if English had been asked for. It now says
"excluding 'lang' = 'en' needs its 63193 records"
(`app/ask/filters.py`, `tests/ask/test_facets.py`). The campaign test passes
once the node runs it.
