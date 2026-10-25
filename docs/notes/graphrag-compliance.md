# Louise against GraphRAG — retrieval capabilities and cost (2026-10-24)

*An assessment, not a decision. Operator: "Check the compliancy of project
louise to this architecture, according to retrieval capabilities and
performance (microsoft/graphrag); check also GraphRAG-Local-UI; possibly
check the lazy graphrag and explain how it works." Nothing here is built
or amended. Where a number is ours it is labelled **measured** with its
source; GraphRAG's numbers are **published by its author** (measuring.md
§5) and were read from the repository's own docs and Microsoft Research's
posts, because microsoft.github.io and arxiv.org did not resolve from the
session's sandbox; what could not be read is marked UNVERIFIED.*

## 0. The verdict in four lines

1. Louise is not a GraphRAG implementation and its specification never
   claims to be one. It is a hybrid retriever (text, vector and catalogue
   legs fused by reciprocal rank, a per-collection cross-encoder, facts
   placed beside the lists) over verifiable, shareable collections.
2. Of GraphRAG's four query modes Louise covers **Basic Search** (and
   exceeds it), covers **Local Search** in part (identifiers → pinned
   facts and filters, typed relations → `louise_traverse`, the chat loop
   with three tools), and has **no Global Search and no DRIFT** — nothing
   that answers a question about the corpus as a whole.
3. The GraphRAG layer — entity mentions, relations, Leiden communities
   with summaries — is **specified to the byte** (Codex version 9,
   claim types `mention` / `relation` / `community`, 09 §9.5, §9.14) and
   **unbuilt at both ends**: no tool produces a claim of those types and
   `ClaimIndex.graph` is built but attached nowhere
   (gutenberg-graph-amendments.md, step 3).
4. The plan as written (wikidata-plugins.md "gutenberg-graph") is FULL
   GraphRAG: an LLM at index time, 2–3 weeks of B70 for 15 % of
   Gutenberg, claims that are not bit-exact and need a measured
   agreement floor. LazyGraphRAG's split — no LLM at index time, the LLM
   at query time — fits Louise's invariants better than the plan does
   (§5). That is the one thing this note asks the operator to weigh.

One spec-versus-code finding came out on the way (§6): the code adds an
entity BOOST to the fused score (`retrieval.ENTITY_BOOST`) where 05 §5.2
says "a filter restricts; nothing boosts".

## 1. GraphRAG, as it stands (v3.3.0, 2026-10-08)

Sources: the repository's `docs/index/default_dataflow.md`,
`docs/index/methods.md`, `docs/query/*.md`, `docs/index/outputs.md`,
`config/defaults.py` (read from raw.githubusercontent.com, main);
https://github.com/microsoft/graphrag/releases.

**Index.** Documents are cut into TextUnits (1,200 tokens, 100 overlap).
Per text unit ONE LLM call extracts entities (title, type, description)
and relationships (source, target, description, strength), plus
`max_gleanings = 1` follow-up call — about two calls per text unit;
descriptions of an entity seen in several units are summarised by a
further call each. Optional claim extraction ("covariates", off by
default). Hierarchical **Leiden** (graspologic, `max_cluster_size 10`,
fixed seed) on the entity graph; one LLM **community report** per
community at every level (8,000 input / 2,000 output tokens), then a
summary of each. Embeddings of text units, entity descriptions and
community reports go to a vector store — **LanceDB by default**; tables
to Parquet. The docs state graph extraction is "roughly 75 % of indexing
cost" and give no per-unit token figure. An LLM cache keyed on
prompt+parameters makes re-runs idempotent.

**`--method fast`** (since v2.0.0): entities are noun phrases (NLTK
regex or spaCy), relationships are text-unit co-occurrence, no
descriptions, no claims; community reports still by LLM; the docs
recommend 50–100-token chunks for it and call the graph "quite a bit
noisier". This is the index half of LazyGraphRAG (§5).

**Query modes.**

| mode | what runs | LLM calls per question |
|---|---|---|
| Basic | vector top-k over text units (k 10), one answer | 1 |
| Local | query embedding → nearest 10 entities by description embedding → their text units, relationships, community reports, covariates, packed into 12,000 tokens → one answer | 1 (+ the embedding) |
| Global | map-reduce over all community reports of one level in 12,000-token batches: a scored-points call per batch, one reduce; **dynamic community selection** rates reports from the root with a cheap model and prunes | tens: 1,500 reports at level 1 on AP News, ~470 with dynamic selection (−77 % cost, published) |
| DRIFT | HyDE-expanded query → top community reports → a primer answer with follow-up questions → each follow-up a local search (`drift_k_followups 20`, `n_depth 3`) → a ranked Q/A tree reduced to one answer | tens (no cost figures published); beat Local 78 % comprehensiveness, 81 % diversity on 50 AP News questions, published |

**Data model** (Parquet): `documents`, `text_units`, `entities`,
`relationships`, `communities` (id, parent, children, level, entity ids,
text unit ids), `community_reports` (summary, full content, rank,
findings), `covariates`. **Incremental update** (`graphrag update`):
new documents found by title, indexed as a delta, entities merged by
title with descriptions appended, the delta's communities appended with
shifted ids — the merged graph is NOT re-clustered; the result goes to
`update_output/`. **Prompt tuning** (`graphrag prompt-tune`) samples the
corpus and writes domain, persona, entity types and few-shot examples
into the three indexing prompts; on a podcast corpus tuned prompts gave
4,896 entities / 1,027 communities against 1,796 / 352 (published).

## 2. Louise, as it stands (this branch, 2026-10-24)

The specification is 05; the code map is app/retrieval.py (its
docstring), app/query_service.py, app/ask/.

**Index.** Per collection: Tantivy text indexes, one per language group,
BM25 over `body` + `chapter` (app/fts.py); a Tantivy catalogue over
title, creators, subjects (app/catalog.py); a LanceDB table of f16
vectors with a local IVF_PQ index (√N partitions, `nprobes 30`,
`refine_factor 30`, app/vector_index.py); facet postings; per-collection
relation edges in SQLite, indexed both ways (app/ask/traverse.py). No
LLM at index time anywhere. Chunks are paragraph-aligned windows of
1,000 tokens with 120 overlap inside a chapter (`book`/`fixed`), one per
abstract, one per article, FastCDC for changing sources — all pure
functions pinned in `space_id`.

**Query.** `resolve → expand → plan → execute → fuse → rerank` (05 §5.2):
identifiers in the query resolve to canonical ids and one-hop facts; the
query is translated once per main language; catalogue, text and vector
legs each rank RECORDS; RRF with k = 60, the record the unit; the top 50
reranked by the collection's cross-encoder (two chunks per record under
a title line, cut to the reranker's window); the catalogue's first record
kept first; one hit per work by the annotation claims; facts and claims
beside the lists, never in them. Several collections: one list per
space, fused by rank only. The answer, where asked for, is ONE chat call
over the hits (`/v1/chat/completions`, `_chat_answer`), or the tool loop
`ask chat` (at most 6 rounds over `louise_search`, `louise_lookup`,
`louise_traverse`).

**Graph, built:** typed relations stated by the SOURCE (`eli:cites`,
`eli:amends`, `eli:repeals`), walked breadth-first up to 6 hops;
work claims (`same-work`, `translation-of`) grouping editions; a
dictionary entity tagger (`doc_keys`) feeding facts and the boost of §6.
**Graph, specified and unbuilt:** `mention`, `relation`, `community`
claims with a `model` method (weights digest, prompt hash, temperature
0) and a `model-agreement` check (03 §3.2); GLiNER mentions linked to
Wikidata, Gemma relations, multi-level Leiden communities with LLM
summaries (wikidata-plugins.md). **Summaries:** app/summarize.py writes a
map-reduce summary per BOOK into the manifest; nothing in retrieval
reads it.

## 3. Capability matrix

| GraphRAG | Louise | where | state |
|---|---|---|---|
| TextUnits with provenance to the document | chunks `uid:seq:version`, every result names collection, snapshot, record, chunk | 05 §5.3 | **built**, stronger (content-addressed, signed) |
| Text-unit embeddings in LanceDB | vectors in LanceDB, IVF_PQ local index, shared as Parquet bundles | 02 §2.4, 09 §9.7 | **built** |
| Basic Search (vector top-k) | three legs + RRF + cross-encoder; measured on gutenberg (§4) | 05 §5.1, §5.6 | **built**, exceeds |
| Entities extracted by LLM, with descriptions | `mention` claims (GLiNER → Wikidata QID), entity = a wikidata RECORD, so its description is the record | 09 §9.5, wikidata-plugins.md | **specified** |
| Relationships extracted by LLM | `relation` claims (`wikidata:P…`, evidence chunk) | 09 §9.5, §9.14 | **specified** |
| Relationships stated by the source | relations member, `louise_traverse` | 05 §5.5 | **built** (law corpora) |
| Covariates / claims | nothing | — | **absent**, not planned (the plan keeps conflicts with Wikidata as flagged relations instead) |
| Leiden communities, hierarchical | `community` claims `{id, level, members, summary}` | 09 §9.5 | **specified**; no Leiden in app/ (networkx only in the louise-python backend's requirements) |
| Community reports (LLM) | the `community` claim's `summary` | 09 §9.5 | **specified** |
| Local Search | identifiers → facts + filters; traverse; the chat loop | 05 §5.2, §5.4 | **partial**: no entity-neighbourhood context assembly, no community report in the context |
| Global Search (map-reduce over reports) | nothing | — | **absent**; wikidata-plugins.md names it as the use of community summaries ("broad questions"), nothing specifies the map-reduce |
| Dynamic community selection | nothing | — | **absent** |
| DRIFT (primer, follow-ups, Q/A tree) | `ask chat`'s 6 rounds are the nearest thing: the model may call `louise_search` with its own follow-ups, but nothing ranks or reduces a question tree | app/ask/chat.py | **absent** |
| Prompt tuning | nothing; a Codex pins a `prompt` hash per method, so a tuned prompt is a new method version | 09 §9.5 | n/a by design |
| Incremental update | snapshots with deltas; re-embedding only the changed chunks; a claims collection "updated, not replaced" | 02 §2.2, annotation-collections.md "Updating" | **built**, stronger — and it re-checks claims against the targets' new content hashes, where GraphRAG appends and never re-clusters |
| LLM cache for idempotent re-runs | every stage resumable, delete-then-insert on uid / chunk id | invariant 2 | **built** |
| Multiple corpora in one answer | one list per space, pipelines (D1–D3) specified | 05 §5.1, kb-pipelines-spec.md | **built** / **specified** |
| Reranking | per-collection cross-encoder, measured | 05 §5.6 | **built**; GraphRAG has none |
| Verifying what another party computed | the whole of 03; `model-agreement` for LLM claims | 03 | **built** / **specified**; GraphRAG has nothing — its index is one party's |

## 4. Performance, side by side

Where the two are comparable at all. GraphRAG's authors publish WIN RATES
against a judge; Louise measures MRR and recall against labelled queries.
Neither number converts into the other.

**Index cost.** Louise embeds and indexes; no LLM. GraphRAG standard: about
two LLM calls per 1,200-token text unit plus one per multi-description
entity plus one or two per community (its own docs, no token figure).
Louise's own plan for gutenberg-graph (wikidata-plugins.md, modelled):
mentions over all of Gutenberg 2–5 days, relations ~1 week and
communities 2–5 days for ~15 % of Gutenberg, on the B70 — Gemma 4 26B
Q8_0 alone on the card, at night.

**Query cost and latency, Louise, measured.**

| what | number | source |
|---|---|---|
| vector leg, IVF_PQ, one index | 24 ms a query; 41 ms with 13 delta indices | app/vector_index.py (2026-10-23) |
| vector leg, flat scan | 90 ms per 100k rows; 13 s on 15.8M rows | app/vector_index.py |
| Lance search to the leg's depth | median 491 ms at 50 records, 833 at 400 | one-language-per-work.md |
| text leg, all variants | median 54 → 164 ms (300 chunks) | one-language-per-work.md |
| catalogue leg, pubmed (40M records) | 18–55 ms warm; the variant 136–492 ms cold | query-campaign.md run 2 |
| reranker, 50 candidates | pubmed 335–543 ms, wikipedia 361–671 ms, gutenberg 2.8–6.4 s (1,500-token chunks); f16 on the GPU 72–95 ms model time, 300–392 ms through search | STATUS.md 2026-10 "latency_budget_ms"; reranker-scope.md |
| hybrid search end to end, pubmed, "aspirin platelet aggregation" | 4.3 s (text 1.2 s, vector 2.4 s, rerank 0.5 s) | STATUS.md |
| LLM calls per search | 0; 1 for an answer; ≤ 6 rounds in `ask chat` | app/ask/chat.py |
| target | p95 ≤ 2 s, reranker ≤ 400 ms | gazzetta-plugin.md |

**Query cost, GraphRAG, published.** Local and Basic: one generation call
each. Global: a map call per 12,000-token batch of reports plus one
reduce — 1,500 reports at level 1 on 5,000 AP News articles, ~470 with
dynamic selection. One third-party figure for a global query (12 LLM
calls, ~150k tokens, ~$0.80 on gpt-4o, baeke.info 2024) is UNVERIFIED —
the page did not resolve. On a local chat model at the node's rates a
global question would take minutes, not seconds.

**Retrieval quality, Louise, measured** (query-campaign.md, the gutenberg
set, through the API with the reranker): 40 titles MRR 0.782, R@10 0.975;
347 descriptive queries MRR 0.411, R@10 0.671. There is no labelled set
for a GLOBAL question ("the main themes of …") because nothing in Louise
answers one; that is the gap, not a number.

## 5. LazyGraphRAG — how it works, and why it matters here

Source: Edge, Trinh, Larson, "LazyGraphRAG: Setting a new standard for
quality and cost", Microsoft Research blog, 2024-11-25 (editor's note
2025-06-06); BenchmarkQED post, 2025-06-05;
https://github.com/microsoft/graphrag/discussions/1490.

**Index time, no LLM.** Noun phrases are extracted by NLP from each chunk
(the concepts); concepts that co-occur in a chunk are linked; graph
statistics prune the graph; hierarchical community detection (Leiden)
groups the concepts; the chunks are embedded. The authors state the
index costs "identical to vector RAG and 0.1 % of the costs of full
GraphRAG". This half shipped as `graphrag index --method fast` (v2.0.0).

**Query time, the LLM everywhere, under one budget.**
1. *Refine.* An LLM splits the question into 3–5 subqueries, each refined
   with matching concepts from the graph, merged into one expanded query.
2. *Match*, per subquery. Best-first: chunks ranked by embedding
   similarity, communities ranked by their top-k chunks. Breadth-first:
   an LLM relevance assessor reads the top untested chunks of the
   communities in rank order, sentence-level. Iterative deepening: after
   z communities in a row yield nothing relevant it recurses into the
   relevant sub-communities. Stops when no relevant community remains or
   the **relevance test budget / q** is spent. The budget is the only
   cost–quality knob.
3. *Map.* The concepts of the relevant chunks form a subgraph; its
   community assignment groups related chunks; an LLM extracts the
   claims each group makes about the subquery; claims are ranked and cut
   to one context window.
4. *Reduce.* One LLM call answers the expanded query from the claims.

**Published results** (5,590 AP News articles, 100 questions, LLM judge):
budget 100 with a cheap model everywhere costs about what vector RAG
costs and beats every method except GraphRAG global search on global
questions; budget 500 (cheap model for the relevance tests, the stronger
model for refine and map/reduce) beats everything, global search
included, at **4 % of global search's query cost**; the same
configuration reaches global-search quality at more than 700× lower
query cost than global search; budget 1,500 keeps gaining. BenchmarkQED
(2025, 1,397 health articles, 200 questions, GPT-4.1 judge): LazyGraphRAG
won 96 of 96 comparisons against vector RAG at 8k and 120k context and
every GraphRAG mode; it lost only on relevance for local questions to a
1M-token-context vector RAG. Exact win rates are in the posts' figures
only.

**Status.** Not in the graphrag repository as of v3.3.0: no `lazy`
module under `query/structured_search`, no CHANGELOG entry, the
maintainers' "next milestone" of December 2024 still asked for in the
discussion through mid-2026. Microsoft ships it inside Microsoft
Discovery and an Azure Local preview (editor's note). A community fork
(TheAiSingularity/graphrag-local-ollama) exposes a `lazy` mode that is
not Microsoft's code.

**Why it matters for Louise — the one point to weigh.** Louise's
invariants make an LLM at INDEX time expensive in a way GraphRAG never
pays for:

- a published claim is never deleted, only superseded (promise 7), and
  must be reproducible by a stranger (promise 5). An LLM's relations and
  summaries are not bit-exact; the plan answers with a `model` method and
  a MEASURED agreement floor (03 §3.2 `model-agreement`), which is honest
  but makes every such collection a tolerance-checked artefact of the
  kind 03 reserves for cross-hardware vectors;
- the plan's cost (weeks of the one card, at night) buys summaries that
  are frozen at the snapshot and re-made as a block when the graph grows.

LazyGraphRAG's index is what Louise already builds plus THREE
DETERMINISTIC derivations — noun phrases, co-occurrence, Leiden with a
fixed seed — that a verifier reproduces byte for byte with no model and
no tolerance, exactly like a chunker (invariant 3). Its LLM work is the
asking peer's own: it never travels, never enters a member, and is
bounded by a budget per question — the position 05 §5.6 already takes
for the reranker ("nothing a reranker produces is written into a member
that travels"). The map step is RRF-free and the claims it extracts
are facts placed in the answer, which is invariant 4's shape. In short:
the index half fits a `community` claim whose `method` is NOT a model
(bit-exact, `mention`s as noun-phrase spans rather than linked QIDs —
or beside them), and the query half fits a fourth tool or a mode of
`louise_search`, node-local like the reranker.

Two frictions, named rather than solved:

- the fast method wants 50–100-token chunks for a usable co-occurrence
  graph; Louise's chunks are ~1,000 tokens. A co-occurrence graph over
  sentences or paragraphs WITHIN a chunk needs no re-chunking and no
  re-embedding (RULE 1), and its spans pin to the chunk's `text_hash` as
  a `mention` does;
- Leiden over 10⁸ noun phrases is a build, not a query; it would run
  where `index build --vector` runs, as a derived index under
  `LOUISE_CACHE_DIR` if local, or as the `community` claims collection if
  shared — the second is what makes it verifiable.

Neither the published blog nor the repository documents the exact
relevance-assessor prompt, the pruning statistics or the community
ranking formula; a Louise implementation would be our own reading of the
post, written down beside the code as the "do not reinvent the wheel"
rule asks (the standard: Microsoft's `--method fast` for the index;
nothing public for the query half).

## 6. Findings about Louise itself, found on the way

1. **A boost where the specification says there is none.**
   `app/retrieval.py:44` `ENTITY_BOOST = 1/60`, applied at ~1000–1005 to a
   hit whose record `doc_keys` tags with an entity the query named; 05
   §5.2: "A filter restricts; nothing boosts … A boost would hide that, and
   would make two peers rank one query differently unless its weight were
   pinned in the Codex, so there is none." The documents win until changed
   (CLAUDE.md). Either the code drops the boost (tests/test_entity_boost.py
   turns into its negative) or 05 §5.2 is amended with the weight stated.
   The campaign set has no identifier queries, so the effect on measured
   quality is unknown — measure before removing, as every fusion change
   has been.
2. **`ClaimIndex.graph` is dead code until step 3** — by design, recorded
   (gutenberg-graph-amendments.md). A community's `state_of` is `unknown`
   and pins nothing, which is right for the specified shape but means a
   community summary cannot go `stale` when its members' text moves — a
   GraphRAG weakness Louise would inherit unless the claim pins its
   members' hashes.
3. **Book summaries exist outside retrieval** (app/summarize.py, admin
   routes only). A map-reduce summary per record is the per-document
   layer GraphRAG lacks and RAPTOR has; nothing indexes it, nothing cites
   it, and it is not a member. Not a defect — noted because any "global"
   mode would start by asking what summaries already exist.
4. **The brief's assumptions that were wrong**, so nobody repeats them:
   the text index is Tantivy, not SQLite FTS5 (dropped 2026-09-19,
   app/catalog.py); there is no peer-to-peer QUERY routing — a search
   reads what the peer holds, peers exchange members, not questions.

## 7. GraphRAG-Local-UI (severian42)

Sources: the repository's README, `api.py`, `EMBEDDING_PROXY_README.md`,
`requirements.txt`, `indexing/settings.yaml`, `graphrag/pyproject.toml`,
its commits and issues (read 2026-10-24).

What it is: an MIT-licensed wrapper — a FastAPI server (port 8012)
exposing OpenAI-style `/v1/chat/completions` whose model name picks
`direct-chat`, `graphrag-local-search` or `graphrag-global-search` and
runs the `graphrag.query` CLI as a SUBPROCESS; a Gradio indexing and
prompt-tuning page; a legacy Gradio page with a Plotly graph view; and an
"embedding proxy" (port 11435) that rewrites Ollama's embedding answers
into the OpenAI shape GraphRAG expects. Everything local is reached as
an OpenAI-compatible endpoint (`api_base`), so LocalAI, LM Studio or
vLLM work the same way; the shipped settings name
`mistral-nemo:12b-instruct-2407-fp16` and `nomic-embed-text` on Ollama,
chunks 512/64, 25 concurrent requests.

Its GraphRAG is a **vendored fork of 0.1.1** (July 2024): pre-1.0
schema, no `update`, no fast method, no DRIFT, no dynamic selection, no
LazyGraphRAG. **Last commit 2024-11-09**; the README says updates are
slow and the code was tested on one Mac Studio; the open issues of
2025–2026 are unanswered dependency breakage and local models that fail
to return the tuple-delimited extraction or the JSON global search
needs (#83, active 2026-08). It is a reference for how a local stack
wires GraphRAG, not something to build on.

What it says to Louise: (a) Louise already is the local stack it bolts
on — every model is HTTP to LocalAI (invariant 7), the Ask page and
`/v1/chat/completions` are its API, `/connect` its UI; (b) its recurring
failure, a local model that does not honour the extraction format, is
the verification problem Louise's `model` method and agreement floor are
designed to catch rather than discover in production; (c) its proxy
exists because GraphRAG speaks one embedding protocol — Louise's VeGa
wrapper and the `Louise-Run` header are the same need solved with a
stated run instead of a shim.

## 8. What would close the gaps, by cost (for the operator; no decision taken)

| gap | smallest honest step | what it costs |
|---|---|---|
| the boost (§6.1) | measure identifier queries with and without, then drop it or amend 05 §5.2 | a day; a fusion change, so numbers in the commit |
| Local Search's context | after step 3 of the graph amendments, `louise_search` places a hit's `mention` and `relation` claims and the member communities' summaries beside it — facts, not legs | step 3 as specified; no spec change |
| communities without a model | a `community` claims collection whose method is noun-phrase co-occurrence + Leiden (fixed seed, pinned library versions), bit-exact, no agreement floor; summaries left empty or written by the ASKING peer | a new `method.kind` in 09 §9.5 (ask first: hashed bytes); a Leiden build off the GPU loop; the chunk-size friction of §5 |
| a global question | a LazyGraphRAG-style mode of `louise_search` (or a fourth tool): refine → budgeted relevance tests over communities → map claims → reduce, node-local, under `[ask]`'s budget, every claim cited to a chunk | the largest item; depends on the row above; 05 §5.4 amended for the tool |
| DRIFT-like follow-ups | `ask chat` already loops; a ranked question tree is a prompt and a reduce, not a format | small, but measure against the plain loop first |
| a labelled GLOBAL query set | 50 "themes of / how did X change" questions over gutenberg with judged answers | without it nothing above can be measured, and a scheduling change without numbers is a guess (CLAUDE.md) |

The recommendation, if any of it is taken up: the labelled set first,
then communities without a model, then the global mode. The full
GraphRAG plan (Gemma relations and LLM summaries at index time) stays
what it is — a stronger graph for point questions, at the cost and the
verification debt §5 names — and nothing here changes it; it is the
operator's to keep, defer or narrow.

## 9. Proposal — unlinked mentions, and linking them by what a passage says

*Added 2026-10-24 at the operator's request, from the discussion of this
note. A proposal: nothing here is decided or built, and two of its rows
touch hashed bytes (09 §9.5), so they are asked, never taken.*

### 9.1 The gap

In the gutenberg-graph plan a mention is a span GLiNER finds, linked to a
Q-id by a search over wikidata's vectors and an LLM's pick "or none"
(wikidata-plugins.md, step 2). A `mention` claim's object is a canonical
id `wikidata:Q…` (09 §9.5, A-K1), so a span that resolves to nothing has
no legal shape and is DROPPED — span, offsets and surface alike. No
relation can involve it either, since both ends of a `relation` are
Q-ids. What a passage is actually about can vanish from the graph while
the famous names it mentions in passing stay: Wikidata has Ahab, not his
crew, and almost no place an author invented. For history and
architecture the loss is small; for novels it is most of the graph.

**Where it matters** (operator, 2026-10-24): novels lose the most spans
and nothing is at stake there — Starbuck is Starbuck in one book. The
value is in NON-FICTION, where one real person appears under different
names in texts that never knew each other: a memoir's "Mr. B." and a
letter collection's full name, at the same dinner in the same house in
the same month (a pseudonym); two diarists in Florence in 1850 who never
met, each describing the same events with different names, places and
dates, matched event by event into one chronology with every line cited
(a story rebuilt from diaries); a soldier, a servant, a shopkeeper named
in three sources that Wikidata has no reason to hold — one person across
books, so anchoring to the book (§9.2) is wrong and minting an identity
has nothing to check against; matching across passages is the only way
they become one, and the confidence says how sure.

### 9.2 What to do with a span that resolves to nothing

| choice | the unlinked span becomes | cost | risk |
|---|---|---|---|
| drop it (the plan today) | nothing | none | the graph is only as complete as Wikidata; novels mostly fall out |
| **keep it as a span with no entity** | a `mention` whose object is null, carrying `surface`, `start`, `end` and GLiNER's type; searchable by surface, never traversable | 09 §9.5 amended (hashed bytes: ask first); no model work | spans do not merge: "Starbuck" and "the first mate" stay two things |
| mint a local entity | the annotations collection registers its own namespace (`gutenberg-graph:<hash of type + normalised surface>`), as a reference source registers one (05 §5.2); spans the LLM judges to be one thing merge under it; relations may use it | the row above plus a namespace and a merge rule — what GraphRAG does, by name | the GraphRAG failure: one thing under three names; an id nobody outside can check against anything |
| anchor it to the book | an unlinked entity belongs to the work it appears in — `gutenberg:2701#Starbuck`, a character or place OF that record, never shared across books | the same amendment, narrower; the book is already a verifiable record | a person two chronicles mention, missing from Wikidata, is two entities |

**Recommendation.** Keep unlinked spans (row 2) from the first run, so
nothing GLiNER found is lost and the loss can be COUNTED: mentions
resolved and unresolved, per LoC class, on the first 1,000 — the same
sample the agreement floor is measured on (gutenberg-graph-amendments.md,
"Decided"). Choose between minting and anchoring from that count, not
before. If the operator says nothing, the plan stands and unlinked
mentions are dropped.

### 9.3 Linking an unlinked span by the relation it takes part in

The operator's case: Wikidata (or another passage) states *Pippo met
Pluto in a park in Florence in 1850*; a passage says *Pippo met Mark in a
park in Florence in 1850*; "Mark" resolved to nothing. Three of four
participants agree, the event is the same, so Mark is probably Pluto. This
is **entity linking by relational context**, and each of its three parts
has a name (CLAUDE.md, "do not reinvent the wheel"):

1. **A tag-agnostic relation embedding.** Every entity span is replaced
   by a typed blank before embedding — `[PERSON] met [PERSON] in a park in
   [PLACE] in [DATE]` — so two statements of the same event shape embed
   close whatever the names, and "met", "ran into", "encountered" are one
   relation without a rule per verb. The standard is *Matching the Blanks*
   (Baldini Soares, FitzGerald, Ling, Kwiatkowski, ACL 2019,
   https://aclanthology.org/P19-1279/): a relation encoder trained so that
   statements sharing an entity pair, entities masked, come out near each
   other. Louise needs no new model for the first version: the masked
   template goes through ONE pinned encoder run named in the method —
   templates are compared with templates, never with a collection's
   vectors, so this is a space of the method, not of gutenberg or
   wikidata (invariant 1; the strategy note proposes jina-v3's passage
   run, which linking needs anyway) — bit-exact like any other vector
   (invariant 12, under torch's `LOUISE_TORCH_DETERMINISTIC=1`, measured);
   a Matching-the-Blanks encoder is the
   stronger choice if the plain one proves too coarse, and it is a stated
   run like any encoder's. The embedding only NARROWS: it finds the known
   statements with the same shape. It never decides — sentence similarity
   puts "Pippo met Mark in Florence in 1850" beside "Pippo left Mark in
   Florence in 1850".
2. **Slots aligned as structure, scored as record linkage.** Each
   candidate's participants are compared one by one: a filled slot by
   Q-id; a place by containment along Wikidata's `located in` (P131) —
   a park in Florence is in Florence; a time at the coarser of the two
   precisions — 12 March 1850 is in 1850. The score is **Fellegi–Sunter**
   (JASA 1969, https://doi.org/10.1080/01621459.1969.10501049), the
   standard behind every record-linkage system: an agreeing field adds a
   weight that depends on how discriminating it is (a shared year is weak;
   person + place + year together are strong), a disagreeing field
   subtracts, the sum is compared with a threshold. "3 of 4" is the
   intuition; the weights keep "Pippo met someone in Florence in 1850"
   from matching every acquaintance he had that year. The weights are
   estimated from the data (the EM fit the method describes), on the
   labelled first run, and pinned.
3. **One assignment per book, not per sentence.** "Mark" is resolved
   together with everything else the book says about him: other
   matched events raise the link, a contradiction (Mark dies in 1849,
   Pluto in 1870) lowers it. This is *collective entity linking* (AIDA:
   Hoffart et al., EMNLP 2011, https://aclanthology.org/D11-1072/), and
   within one record it is the ordinary coreference step.

**The result** is a candidate, never a merge: a claim `same-entity`
(new type, 09 §9.5 — hashed bytes, ask first) from the local span (row 2
above gives it a shape) to `wikidata:Q…`, with the matched relation as
`evidence` (the Wikidata statement or the other passage's `relation`
claim), the Fellegi–Sunter score as `confidence` (a decimal string, as
every score in a claim is), and a `method` whose kind is NOT a model:
the template encoder's stated run and wikidata's query run (the one
that linked the spans), the masked template version, the pinned
weights and threshold, the wikidata snapshot — deterministic given those,
so a stranger reproduces it the way a chunker is reproduced, with no
agreement floor. The entities' records are never merged: shown as
"probably the same person, because of these passages", it is useful and
honest; written as one entity it is the GraphRAG failure in reverse.

**Where the base relations come from.** Wikidata's statements with
qualifiers — `significant event` / `participant` / `location` /
`point in time` — are exactly n-ary facts to match against. Where
Wikidata has no such statement, two passages from two books can be
matched against each other the same way (their `relation` claims, both
with an unlinked end), which is how the graph grows past Wikidata without
inventing identities.

### 9.4 What it depends on, and the order

- Row 2 of §9.2 first: without a kept span there is no "Mark" to resolve.
- The `relation` claims of the plan (an LLM, agreement floor measured) OR
  the co-occurrence graph of §5: the slot alignment needs the event's
  participants as a tuple, however it was extracted. Linking by relation
  therefore follows relation extraction; it cannot replace it.
- Measured before any of it is kept: on the first 1,000 unlinked spans,
  how many gain a candidate above threshold, and of a judged sample how
  many are right. A link rate without its precision is not a number.
  Counted PER LoC CLASS, and the judged sample drawn from history and
  biography (C, D, E, F), not from fiction: that is where a right link is
  worth something and a wrong one is dangerous.

The per-category strategy that places this proposal in the whole plan —
which tiers of Gutenberg get the reader, which only the exact layer — is
[gutenberg-graph-strategy.md](gutenberg-graph-strategy.md).

Amendments, if approved: 09 §9.5 (`mention` with a null object and its
keys; the `same-entity` type; a deterministic `method` kind for a scored
procedure), 05 §5.3 (the candidate shown beside the hit with its
evidence), 03 §3.2 (its check: recompute the score). Asked, not written.
