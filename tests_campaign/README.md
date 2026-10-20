# The query campaign

Live acceptance tests: **does each kind of query do what it promises** (docs/05,
02 §2.10), run against a **running node**. They are kept apart from `tests/`, and
the unit suite and its gate never collect them (operator, 2026-10-20).

```
LOUISE_URL=http://192.168.178.10:30042 python -m pytest tests_campaign -q -rA
```

Without `LOUISE_URL`, every test is skipped. The tests only read: the ask routes
are open on the LAN and change nothing (07 §7.4).

| file | queries | promises checked |
|---|---|---|
| `test_search_gutenberg.py` | `POST /api/ask/search` | The answer's shape. `limit`. Determinism. A title finds its work. A description finds its work by meaning (the vector leg). One hit per work. The edition shown is in the query's language. Language and exclusion filters. A filter matching nothing is an answer. An undeclared facet is refused. Facts sit beside the lists. An annotations collection is not a search target. Unknown collections are refused. Nothing is withheld silently. |
| `test_editions_annotations.py` | search, `POST /api/ask/traverse`, `POST /api/ask/lookup` | Claims are placed on hits with every field. The editions listed are the whole work. A translation and its original are one work. `traverse` reaches every edition, narrows by relation, and works at depth 2. `lookup` of a record uid is unresolved, never an empty fact. |
| `test_chat.py` | `POST /api/ask/chat`, `POST /v1/chat/completions` | Answered from the tools and cited by returned uids. No invented record. Numbered citations. |

The ground truth (`ground_truth.py`) is the node's own data: the gutenberg
manifest and gutenberg-book-editions' relations, read on 2026-10-20. A test
asserts what the data says, never what a query happened to answer.

Results of each run, and what failed and why: `docs/notes/query-campaign.md`.
