# Louise's extensions to the knowledge-base protocols it speaks (DRAFT)

**DRAFT, not decided (operator, 2026-10-22: wait for the end of the current
developments, then summarize and decide).** Proposal:
[`kb-pipelines.md`](kb-pipelines.md) §3A. Pipelines:
[`kb-pipelines-spec.md`](kb-pipelines-spec.md).

**The rule.** Every addition is optional. A client that sends none of the
new fields, and reads none of the new keys, sees exactly what it sees
today. Nothing existing is renamed, retyped or removed.

**Common vocabulary.** These are the values the protocols' metadata cannot
carry today, under one name everywhere:

| name | type | meaning |
|---|---|---|
| `reader_url` | string | this node's reader, with the passage highlighted |
| `source_url` | string | the record at its origin |
| `collection`, `collection_id` | string | where the hit is held |
| `snapshot_id` | string | the snapshot the search read (05 §5.1) |
| `space_id` | string | the space that found it |
| `trust_level`, `producer` | string | 03 §3.6 |
| `editions` | array of `{uid, title, lang, held}` | the work's other editions (05 §5.3) |
| `claims` | array of `{type, uid, title, lang, method, confidence, state}` | annotations beside the hit |
| `facts` | array | lookups by key beside the list (answer level) |
| `ranks` | object | per stage (kb-pipelines-spec §P4) |

Flat maps (LocalRecall `metadata`, OpenAI `attributes`) receive the
**scalars** only. The arrays go in one object per result, named for the
protocol (below), which a client that does not know it ignores.

## E1. LocalRecall / LocalAGI (`/api/collections…`, 07 §7.4)

`POST /api/collections/{c}/search` takes these new request fields, all
optional:
- `filters` (05 §5.2);
- `as_of` (`YYYY-MM-DD`);
- `annotations` (names, `"all"` or `"none"`);
- `rerank` (`{model, depth}` or `"none"`);
- `explain` (bool).

Each result gains:
- `metadata.reader_url`, `metadata.source_url`, `metadata.snapshot_id`,
  `metadata.space_id`, `metadata.trust_level`, all strings, as LocalRecall's
  map demands;
- `extra: {editions, claims, ranks}`.

`data` gains `facts` and `withheld`.

`POST /api/search` is **new**. It takes `{query, collections: [...] |
pipeline: "<name>", max_results, …the fields above}`. Its answer is the
search's results, each also carrying `metadata.collection` and
`metadata.leg`, in the same envelope.

`GET /api/collections/{c}` is **new**. It answers `{name, kind: deposit |
curated | pipeline, space: {encoder, dims, chunker}, snapshot_id,
license_class, read_only}`.

## E2. OpenAI (`/v1/vector_stores`, 61d3be3)

`POST /v1/vector_stores/{id}/search` takes a new request object
`louise: {annotations, as_of, rerank, explain}`.

Each result gains:
- `attributes.reader_url`, `attributes.space_id`, `attributes.snapshot_id`,
  `attributes.trust_level`;
- `louise: {editions, claims, ranks}`. The SDK's models keep unknown keys.

The page gains `louise: {facts, withheld, pipeline}`.

A vector store's `metadata` carries `kind` (deposit, curated or pipeline)
and `space_id`.

## E3. `/v1/chat/completions`

New request fields: `pipeline` (the model name `louise/<pipeline>` does the
same), `sources` (done), `search_id` (done), `explain`.

New answer fields:
- `louise_sources`, done, each source gaining `space_id`, `ranks` and
  `claims`;
- `louise: {pipeline, legs, withheld}`.

## E4. MCP

`louise_search` gains `pipeline`, plus `explain` for the ranks. Its result
gains `pipeline`, `legs` and each hit's `ranks`. Results stay
`structuredContent` JSON. A client that renders citations gets each hit as
a resource link to `reader_url`.

## E5. What would be asked upstream (kb-pipelines §3B, D3)

None of these is needed for E1–E4. Each is useful on its own.

1. LocalAGI: an agent's knowledge base may name `collections: [...]`, or a
   `pipeline`, and a `reranker_model` (beside issue #11753 / PR #12145).
   Search parameters reach the KB API unchanged.
2. LocalAGI / LocalRecall clients: `metadata.source_url` (or `reader_url`)
   is shown as a citation in the agent's answer.
3. LocalAI: a model YAML may declare `knowledge: {url, pipeline,
   max_results}`. Before the chat turn, LocalAI searches it and passes the
   hits as cited context.

## E6. Tests (to write when built)

- Every new field is optional: the existing contract tests
  (tests/deposit/test_deposit_localagi.py, tests/deposit/test_openai_stores.py,
  tests/ask/test_ask_api.py) pass unchanged.
- OpenAI's SDK parses every extended answer (the scratch-install check).
- Flat maps hold only scalars.
- `explain` off returns no `ranks`.
