# Knowledge-base pipelines, and Louise's features in LocalAI's family

*Proposal, 2026-10-22. Operator: "we might consider adding their way too for
their specific knowledge base flavor collections … make a proposal about
extending localAI/localAGI/localRecall API to support the unique features
that Louise offers. I would like to build knowledge base pipelines to group
collections/annotations, and fuse the results with rerankers and rrf."
Nothing is built. Three decisions are in §6.*

**WAITING (operator, 2026-10-22): "Write the specs but wait for the end of the current developments. Then we summarize what's planned and we decide. Some decisions need time to decant."** Specs: [`kb-pipelines-spec.md`](kb-pipelines-spec.md), [`kb-protocol-extensions.md`](kb-protocol-extensions.md).

## 1. Where things stand

Louise already speaks three knowledge-base protocols:
- LocalRecall's, as LocalAGI's knowledge base (07 §7.4, the LocalAGI routes);
- OpenAI's vector stores (61d3be3);
- MCP.

None of them can name **more than one collection at once**. None carries
what makes a Louise hit worth trusting, because their result objects have
no field for it. LocalRecall's `metadata` is a flat map of strings.
OpenAI's `attributes` holds strings, numbers and booleans. What has no
field:
- the space;
- the snapshot;
- the producer and trust level;
- the editions;
- the claims;
- the facts placed beside the hits;
- each stage's rank.

**LocalRecall's own flavour** is upload-and-search, with sources fetched
again on a timer. It is what Louise's deposit collections already are
(02 §2.9), sources included. The one difference is fusion: LocalRecall adds
weighted BM25 and vector scores, while Louise fuses by reciprocal rank. RRF
needs no weights and does not care that the two scores live on different
scales. So no new collection kind is proposed. A pipeline leg may ask for
`fusion = "weighted"` only if a measured case shows it wins (§3).

## 2. The pipeline

A **pipeline** is a named, declared way to answer a question over several
collections. It is desired state: one TOML file under
`${CONFIG_DIR}/pipelines/<name>.toml` (invariant 10). Its id is the
SHA-256 of its canonical form, so every answer can say which version of
the pipeline produced it (the `Louise-Pipeline` header and the result's
`pipeline` field).

```toml
title = "Classics, with their editions and the encyclopedia"

[[leg]]                                   # one leg = one space (invariant 1)
collections = ["gutenberg"]
annotations = ["gutenberg-book-editions"] # work claims group editions; claims beside
rerank = { model = "louise-rerank-bge", depth = 50 }

[[leg]]
collections = ["wikipedia-en"]
rerank = { model = "louise-rerank-bge", depth = 30 }

[fuse]                                    # across legs: by RANK, never by score
method = "rrf"                            # k = 60; `weights` per leg, optional
final_rerank = { model = "louise-rerank-bge", depth = 40 }   # optional: one model, one scale

facts = ["refs-hgnc"]                     # lookups by key: beside, never ranked (invariant 4)
answer = { lang = "auto" }                # optional: the chat model writes from the fused list
```

**What runs, in order**:
1. Each leg runs as an ordinary search does today: text, vector and
   catalogue legs fused by RRF within its one space (05 §5.1). It is then
   reranked by its own reranker at its own depth.
2. The legs are fused **by rank**, with RRF over the legs' reranked lists.
3. Optionally, the top `depth` of the fused list is reranked by **one**
   cross-encoder. That model scores each (query, passage) pair on its own
   scale, whatever space the passage came from.
4. Editions are grouped by the claims (05 §5.3). Claims and facts are
   attached beside the hits.
5. Optionally, the answer is written.

Every hit carries `leg`, `space_id` and its rank at each stage
(`ranks: {text, vector, catalog, leg_rrf, leg_rerank, fused, final}`), so a
result can be explained.

**What a pipeline may not do**:
- put two spaces in one vector search (invariant 1);
- rank a fact (invariant 4);
- fuse legs that answer different questions. Homology is never fused with
  relevance; such a leg is a separate list in the answer, as in 05 §5.1.

**Cost**: each leg reranks at its own depth, and the final rerank adds one
more pass. On the node, the strict reranker measured 6.6 s for 50
candidates (München, 2026-10-22). A two-leg pipeline with a final rerank is
three passes, so depths are per leg and small, and the final rerank is off
by default. The models a pipeline names are declared, which is exactly what
the RAGE lookahead needs to load them ahead (rage-prior-art §8).

**Where a pipeline shows up** (RULE 2: the person picks a name, nothing else):

| surface | the pipeline is |
|---|---|
| Ask page | a choice in the scope selector, beside the collections |
| `/v1/models` | `louise/<pipeline>` (Open WebUI, the pipe) |
| OpenAI vector stores | a read-only store with id `<pipeline>` |
| LocalRecall API | a collection named `<pipeline>`, searchable and read-only |
| MCP `louise_search` | `pipeline: "<name>"` |
| CLI | `louise ask search --pipeline <name>`; `louise pipeline list / show / check` |

`louise pipeline check <name>` refuses:
- a leg mixing spaces;
- an annotations collection about none of the leg's collections;
- a reranker LocalAI does not serve;
- a collection that is withheld or unlicensed.

## 3. Extending LocalRecall, LocalAGI and LocalAI

There are two directions. The first is ours to do. The second asks upstream
for little.

**A. Louise extends the protocols it speaks.** Optional fields only, so an
unmodified client sees exactly what it sees today:

| protocol | request: added, optional | answer: added |
|---|---|---|
| LocalRecall `POST /api/collections/{c}/search` | `filters`, `as_of`, `annotations`, `rerank: {model, depth}`, `explain` | `metadata.reader_url`, `metadata.source_url`, `metadata.snapshot`, `metadata.space`; an `extra` object holding editions, claims, facts, trust and ranks (a client that does not know it ignores it) |
| LocalRecall, new `POST /api/search` | `{query, collections: [...] \| pipeline, max_results, …}`: several collections at once | the same results plus `leg` |
| LocalRecall `GET /api/collections/{c}` (new) | — | `{kind: deposit \| curated \| pipeline, space: {encoder, dims, chunker}, snapshot, license_class, read_only}` |
| OpenAI `POST /v1/vector_stores/{id}/search` | `louise: {annotations, as_of, rerank, explain}` | `attributes.reader_url`, `attributes.space`, `attributes.snapshot`; a `louise` object per result (the SDK keeps unknown keys) |
| `/v1/chat/completions` | `pipeline` (the model name does the same) | `louise_sources` (done), plus `pipeline` and `ranks` |

Written up as one extension note: `docs/notes/kb-protocol-extensions.md`,
once this proposal is decided.

**B. What we would ask upstream.** Three small changes, each useful
without Louise:
1. **LocalAGI: an agent's knowledge base names several collections, or one
   pipeline, plus a reranker.** Issue #11753 and draft PR #12145 already
   give an agent `embedding_model` and `reranker_model`. Ours adds
   `collections: [...]` and passes the agent's KB search parameters
   through unchanged. This is where Louise's pipelines plug in.
2. **LocalAGI and LocalRecall clients: render `metadata.source_url` as a
   citation.** Today an agent's answer cannot link to its source.
3. **LocalAI: a model may declare its retrieval as a pipeline.** A model
   YAML's `mcp.remote` already reaches Louise. Add an optional
   `knowledge: {url, pipeline, max_results}`: before the chat turn, LocalAI
   searches that pipeline and passes the hits as context with citations
   (the `search_result` pattern Claude's API uses). Any LocalAI model then
   becomes "a model with a library", with no agent loop.

What we do not ask upstream:
- LocalRecall hosting pipelines itself: its stores have no spaces, no
  snapshots and no claims, and adding them would be rebuilding Louise
  inside it;
- the RAGE run identity: that stays the patch already planned
  (rage-over-localai §4).

## 4. Order of work

1. **The pipeline in Louise:**
   - TOML, canonical id, `louise pipeline check`;
   - the stages above in `app/ask/pipeline.py`, wrapping
     `results.search` per leg;
   - per-stage ranks in the hit;
   - tests: one-space legs, rank fusion, the final rerank, a fact never
     ranked, a homology leg kept apart, the id changing when the file
     changes.
2. **Surfaces:** the Ask scope selector, `/v1/models`, vector stores,
   LocalRecall's collection list, MCP, CLI.
3. **The optional protocol fields of §3A**, and the extension note.
4. **Measure on the node**:
   - a two-leg pipeline against the same questions searched per
     collection (query campaign: MRR@10, R@10, latency per stage);
   - whether `fusion = "weighted"` ever beats RRF.
5. **Upstream (§3B)**: issues first, PRs only for what the maintainers
   accept.

## 5. What this changes in the specification

05 §5.1 says "collections in different spaces are searched separately and
presented separately", and 05 §5.6 says "never across lists … two spaces
are never put on one score scale". A pipeline that fuses legs from
different spaces changes both, for pipelines only. An ad-hoc search keeps
its lists per space. The amendment would say:
- fusion across legs is by **rank** (RRF), never by score;
- the only cross-space score scale is the final reranker's, which scores
  the text pair and never a vector;
- every hit names its leg and space, so the reader still sees where it
  came from.

## 6. Decisions

**D1 — Fusing collections from different embedding spaces in a pipeline.**
Today a search over gutenberg (one encoder) and pubmed (another) returns
two lists. A pipeline could merge them into one list.

| choice | effect |
|---|---|
| **by rank, optional final reranker** | one list, as asked; 05 §5.1 and §5.6 amended for pipelines only; each hit names its leg and space |
| separate lists per space, as today | no amendment; a pipeline only groups collections that share a space, plus annotations and facts |

Recommendation: by rank. If nothing is said: by rank, since this is what
was asked.

*Refined by the operator, 2026-10-24: merge "only if it makes sense" —
any collection returns something, pertinent or not.* A leg passes a
**pertinence gate** before it is fused: one cross-encoder scores each leg's
best records on its one scale, and a leg below a threshold measured on
labelled queries is left out and named (spec §P3a).

**D2 — Where a pipeline is defined.**

| choice | effect |
|---|---|
| **Louise TOML** (`${CONFIG_DIR}/pipelines/`) | desired state, versioned by hash, served through every protocol Louise speaks |
| LocalRecall / LocalAGI configuration | each client would define its own, and none of them knows spaces or claims |

Recommendation: Louise. If nothing is said: Louise.

**D3 — Asking upstream (§3B).**

| choice | effect |
|---|---|
| **after the pipeline works here**: issues for §3B 1–3, PRs where welcomed | we propose something already running; nothing waits on upstream |
| now | earlier feedback, but on a design not yet built |
| never | our own extensions only (§3A) |

Recommendation: after. If nothing is said: after.

**DECIDED (operator, 2026-10-24): after.** Upstream hears from us once the
pipeline runs here: issues for §3B 1–3 (the third reshaped by D2's
proposal of 2026-10-24 into a `pipeline.retrieval` stage of a LocalAI model
YAML, beside the realtime pipeline's `vad`/`transcription`/`llm`/`tts`),
PRs only where the maintainers welcome them.
