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
| `gate` | table or absent | absent (every leg fused) | the pertinence gate (§P3a): a leg with nothing pertinent to the question is left out of the fusion |
| `gate.model` | a reranker LocalAI lists | `fuse.final_rerank.model`, else the node's reranker | the ONE cross-encoder whose score decides, for every leg |
| `gate.top` | int 1–10 | 3 | how many of each leg's best records it scores |
| `gate.threshold` | decimal string | — (required with `gate`) | the score a leg's best record must reach; set from `louise pipeline calibrate`, never by hand guess (§P3a) |
| `gate.threshold_by_lang` | table lang → decimal string | `{}` | per passage language, where the gate model's scale is measured to differ by language (reranker-scope.md §14) |
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
3a. **The pertinence gate (§P3a, optional).** Before step 3, each leg's
   best `gate.top` records are scored by `gate.model`; a leg whose best
   score is below the threshold is not fused.
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

### P3a. The pertinence gate

*Operator, 2026-10-24: "my base idea about pipelines is to merge results,
but only if it makes sense: any collection will return something, no matter
what, but is it meaningful? Is there a way to discard non pertinent
collections?"*

Every leg returns its best records whether or not any of them answers the
question; fusing them puts a pertinent leg's hits beside another leg's
least-bad ones. The gate keeps a leg out of the fusion when nothing it
found is pertinent.

**The signal.** A cross-encoder scores each (question, passage) pair on
its own absolute scale, whatever space or collection the passage came
from: the one cross-space scale a pipeline has (05 §5.6). Vector distances
and RRF scores are not: one is per space, the other is a rank. Measured
scale: LAMAR's card, a relevant passage near +5 and unrelated ones near
−13; on our pairs the cross-language bias is about −1.7 (reranker-scope.md
§14a), small against that gap, and `threshold_by_lang` absorbs it where the
calibration shows it matters.

**The rule.**
- `gate.model` scores each leg's top `gate.top` records, with 05 §5.6's
  candidates; where a leg's own reranker IS `gate.model`, its scores are
  reused and nothing is scored twice.
- A leg passes when its best score ≥ the threshold (by the passage's
  language when `threshold_by_lang` names it, else `gate.threshold`).
- Only passing legs enter step 3. A leg left out is NAMED, never dropped
  silently: the answer's `not_pertinent` lists it with its best score and
  the threshold (§P4).
- **No leg passes**: the list is the best-scoring leg's, flagged
  `below_gate: true`, and a written answer is told so and says that no
  collection held a pertinent passage — the reader decides what it is worth.
- The gate model is bit-exact (invariant 12), so the same question on the
  same members gates the same legs on every peer.

**The threshold is measured, never guessed** (`louise pipeline calibrate
<name> --queries FILE`): labelled queries whose answer lies in one leg,
run over all of them. It prints, per candidate threshold, the share of
pertinent legs kept and of non-pertinent legs dropped, per language, and
recommends the highest threshold that keeps every labelled answer's leg.
`louise pipeline check` refuses a `gate` without a threshold.

**Cost.** At most `gate.top` pairs per leg not already scored by the
gate model — a fraction of one leg's rerank (6.6 s for 50 candidates
measured on the node, 2026-10-22).

**The standard, and what is not built** (CLAUDE.md, "Do not reinvent the
wheel"). This is federated search's *resource selection* (TREC FedWeb
2013–2014), done POST-retrieval with a cross-encoder threshold, the
simplest form that fits a few legs that are all searched anyway. The
standard PRE-retrieval methods — CORI (Callan et al., 1995), ReDDE (Si &
Callan, 2003), or query-performance predictors such as NQC — choose which
collections to search before searching them, from samples or statistics of
each. They pay when a pipeline has many legs and searching all of them is
the cost; adopting one means keeping a document sample per collection and
its statistics per snapshot. Not specified until a measured pipeline needs
it. Routing by the chat model is not used: not bit-exact, and a model call
on every search.

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

Per answer: `legs: [{collections: [{name, collection_id, snapshot_id}], space_id, question, reranker, ms, gate: {best, threshold, passed} | null}]`; `not_pertinent: [{leg, collections, best, threshold}]` (§P3a); `below_gate: true` when no leg passed.

## P5. CLI

- `louise pipeline list`, and `louise pipeline show <name>`, which prints
  the canonical form and the id.
- `louise pipeline check <name>`, which runs §P3's refusals without
  searching; exit 0 or 2.
- `louise pipeline calibrate <name> --queries FILE`: the gate's threshold
  measured on labelled queries (§P3a); prints, writes nothing.
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
11. The gate (§P3a): a leg below the threshold is not fused and is named
    under `not_pertinent`; a leg whose reranker is the gate model is not
    scored twice; `threshold_by_lang` is applied by the passage's
    language; no leg passing returns the best leg flagged `below_gate`;
    `check` refuses a gate without a threshold; the same question gates
    the same legs on two runs.
12. Plan check and timing of any new query against a real manifest
    (CLAUDE.md): a pipeline adds no per-round cost proportional to the
    number of collections.

## P8. Amendment text for 05 (applied only if D1 is "by rank")

> *Amended <date> (operator).* **Pipelines** (`docs/notes/kb-pipelines-spec.md`) may fuse lists from different spaces, by RANK only: each leg is one space and is reranked by its own reranker, the legs' lists enter a reciprocal-rank sum, and an optional final reranker scores the fused top on its own scale, over text pairs and never vectors. A leg with nothing pertinent — its best record below the pipeline's measured threshold on that one reranker's scale — is not fused, and the answer names it. Every hit names its leg and space. An ad-hoc search keeps one list per space, as above.
