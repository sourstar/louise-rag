# Knowledge-base pipelines — specification (DRAFT)

**DRAFT, not decided (operator, 2026-10-22): "Write the specs but wait for
the end of the current developments. Then we summarize what's planned and
we decide. Some decisions need time to decant."**

Nothing here is built. The specification documents (01–09) are not
amended yet. The proposal and its three decisions are in
[`kb-pipelines.md`](kb-pipelines.md) §6. Where a rule below depends on a
decision, it says which one.

## P1. The file

`${CONFIG_DIR}/pipelines/<name>.toml` is desired state (invariant 10).
`<name>` follows the rule for local names: `[a-z0-9][a-z0-9._-]{0,63}`. It
must not equal the name of a held collection, because the two share every
namespace a client sees (§P6).

| key | type | default | meaning |
|---|---|---|---|
| `title` | string | the name | shown in pickers |
| `leg` | array of tables, 1–8 | — | see below |
| `fuse.method` | `"rrf"` | `"rrf"` | across legs; `"rrf"` is the only method (D1) |
| `fuse.k` | int ≥ 1 | 60 | the RRF constant |
| `fuse.weights` | array of floats | all 1.0 | one per leg, in order |
| `fuse.final_rerank` | `{model, depth}` or absent | absent | one cross-encoder over the fused top `depth` (D1) |
| `facts` | array of reference collection names | `[]` | lookups by key, placed beside the hits (invariant 4) |
| `answer` | `{lang}` or absent | absent | the chat model writes from the final list; `lang = "auto"` means the question's language |
| `limit` | int 1–100 | 10 | results returned after grouping |

A **leg**:

| key | type | default | meaning |
|---|---|---|---|
| `collections` | array, ≥ 1 | — | held collections; **all in one space** |
| `annotations` | array, or `"all"`, or `"none"` | `"all"` | annotations collections consulted for these collections (05 §5.3) |
| `filters` | table | `{}` | facet filters (05 §5.2), applied to this leg |
| `rerank` | `{model, depth}` or `"none"` | the node's reranker, depth 50 | this leg's reranker |
| `question` | string | `"relevance"` | 05 §5.1's question; legs that answer another question are never fused (§P3) |
| `fusion` | `"rrf"` or `"weighted"` | `"rrf"` | inside the leg, across its text, vector and catalogue lists; `"weighted"` only after a measured win (kb-pipelines §1) |
| `weights` | `{text, vector, catalog}` | — | for `"weighted"` only |

## P2. Identity

The **pipeline id** is `sha256:` plus the SHA-256 of the file's canonical
form: the TOML parsed and re-serialised as canonical JSON (09 §9.2's rules:
sorted keys, no insignificant whitespace, UTF-8), with every default filled
in. Two files that mean the same thing therefore have the same id.

The id names **what was asked**, not what was held. The collection ids and
snapshots each leg read are in the answer (§P4), never in the id.

Every answer carries the pipeline's name and id: the header
`Louise-Pipeline: <name> <id>` and the result field `pipeline: {name, id}`.

## P3. A run

For a query `q`:

1. **Per leg.** Run the ordinary search of 05 §5.1–5.6 over the leg's
   collections, with its filters and annotations, at a depth of
   `max(rerank.depth, fuse.final_rerank.depth, limit)`. Rerank with the
   leg's own reranker, using 05 §5.6's record candidates. The leg's list is
   its reranked records, in order.
2. **Questions.** Legs whose `question` differs are not fused. The answer
   has one list per question, as in 05 §5.1. Steps 3–4 run within each
   question group.
3. **Fusion across legs, by rank (D1).** A record `r` scores
   `Σ_legs w_leg / (k + rank_leg(r))` over the legs that hold it. A record
   is identified by uid; two legs hold the same uid only when their
   collections share records (a lineage, 02 §2.7). Ties are broken by uid
   bytes, then by the earliest leg, so the list is the same on every peer
   with the same members (05 §5.1).
4. **Final rerank (D1, optional).** The fused top `depth` is reranked by
   one model with 05 §5.6's candidates. The rest keep their fused order
   below. Without it, the fused order stands.
5. **Grouping.** One hit per work, from the claims (05 §5.3,
   app/ask/works.py), within each list.
6. **Beside the hits.** Claims (05 §5.3) and facts (`facts`, 05 §5.1). They
   are never ranked.
7. **Answer (optional).** Written from the first `limit` hits, numbered as
   the list. The hits come back beside it (`louise_sources`), so it is one
   search.

**Refusals** come before any search, with exit code 2 / HTTP 409:
- a leg's collections in more than one space;
- a collection that is not held, withheld (07 §7.8), unlicensed (invariant
  9), or an annotations collection;
- an annotations collection about none of the leg's collections;
- a reranker that LocalAI does not list;
- a `facts` entry that is not a reference collection.

A leg whose collections are only **deferred** (an encoder unreachable,
05 §5.6's latency budget) is left out, and the answer names it under
`withheld`. The rest are fused. This is deferral, not failure (CLAUDE.md).

**Time.** Each leg's search runs in parallel, with its own deadline (05
§5.6). The final rerank runs after them. Each stage's milliseconds go to
the query log per leg.

## P4. The result

As 05 §5.3, plus the following per hit:

| field | meaning |
|---|---|
| `leg` | the leg's index in the file |
| `space_id` | the space that found it |
| `ranks` | `{text, vector, catalog, leg, leg_rerank, fused, final}`, each 1-based or null |
| `pipeline` | `{name, id}` |

Per answer: `legs: [{collections: [{name, collection_id, snapshot_id}], space_id, question, reranker, ms}]`.

## P5. CLI

- `louise pipeline list`, and `louise pipeline show <name>`, which prints
  the canonical form and the id.
- `louise pipeline check <name>`, which runs §P3's refusals without
  searching; exit 0 or 2.
- `louise ask search --pipeline <name> <query>`; `--explain` prints each
  hit's `ranks`.

There is no `create` verb: a pipeline is a file. Library dialogs list
pipelines read-only (RULE 2).

## P6. Surfaces

A pipeline is reached by name wherever a collection is. It is read-only
everywhere.

| surface | how |
|---|---|
| Ask page | the scope selector lists pipelines above the collections; choosing one disables the collection and annotations pickers, because the file decides them |
| `/v1/models` | `louise/<pipeline>`; `/v1/chat/completions` with that model, or `pipeline: "<name>"` in the body |
| OpenAI vector stores | `GET /v1/vector_stores` lists it (`metadata.kind = "pipeline"`); `search` runs it; `files` lists the union of its legs' records, paged by uid |
| LocalRecall API | `GET /api/collections` lists it; `POST /api/collections/<pipeline>/search` runs it; writes answer 409 |
| MCP | `louise_search {pipeline}`; `tools/list` describes the field and lists the names in its description |
| CLI | §P5 |

## P7. Tests (to write when it is built)

1. A leg mixing spaces is refused; one space per leg passes.
2. RRF across legs: a hand-computed fixture; the tie-break by uid bytes,
   then leg; the same list from two runs and from two orders of `collections`.
3. Weights change the order exactly as the formula says.
4. The final rerank re-orders only the top `depth`.
5. A fact is never in a list; a claim is beside, never in the order.
6. A homology leg comes out as its own list.
7. The pipeline id changes when the file's meaning changes, and does not
   change when only formatting or a default spelled out changes.
8. A deferred leg is named under `withheld`, and the rest is fused.
9. Every surface of §P6 answers with the same hits for the same query.
10. One search per question when an answer is asked for.
11. Plan check and timing of any new query against a real manifest
    (CLAUDE.md): a pipeline adds no per-round cost proportional to the
    number of collections.

## P8. Amendment text for 05 (applied only if D1 is "by rank")

> *Amended <date> (operator).* **Pipelines** (`docs/notes/kb-pipelines-spec.md`) may fuse lists from different spaces, by RANK only: each leg is one space and is reranked by its own reranker, the legs' lists enter a reciprocal-rank sum, and an optional final reranker scores the fused top on its own scale, over text pairs and never vectors. Every hit names its leg and space. An ad-hoc search keeps one list per space, as above.
