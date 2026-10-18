# Wikidata Embedding Project (philippesaade-wmde/WikidataTextEmbedding, `Wikidata_Vectors_0.2`, `philippesaade/wikidata`)

Measured 2026-10-18. The method: their own code at pinned commits
(WikidataTextifier 0befd7c, WikidataTextEmbedding c9dc819, the versions
`uv.lock` pins), their Wikibase formatter (`wikibase/wikibase:mw1.44.0`), and
jina-embeddings-v3 run locally (`task=retrieval.passage, truncate_dim=512`).
The local model agrees with Jina's API to 1e-6 for the same text. On 1,000
random English entities (corpus file `chunk_100`): **86 % of the published
vectors are reproduced exactly; ~14 % are vectors of a different text.**

## 1. Vectors of older texts are kept as current

**Root cause:** `wikidataVectorCache.filter_for_update` re-embeds an entity
only when its own `modified` is newer than the cached vector. An entity's
text also changes when a linked entity's label, the textifier code or the
property page changes, and none of those moves its `modified`.

**Example:** Q9208205's vector is the embedding of
"24-hour urine collection." (cosine 0.999999). Every textifier since
February 2026, and the live textifier API, produce
"24-hour urine collection" (no full stop; cosine 0.963).

```python
# vector from data/en/chunk_219.parquet, id "Q9208205_en_1"
v = np.frombuffer(base64.b64decode(row["vector"]), "<f4")
m = AutoModel.from_pretrained("jinaai/jina-embeddings-v3", trust_remote_code=True)
for t in ("24-hour urine collection.", "24-hour urine collection"):
    print(t, m.encode([t], task="retrieval.passage", truncate_dim=512)[0] @ v)
```

**Suggested fix:** decide re-embedding on a hash of the chunk TEXT, not on
`modified`. And publish the text with each vector (`content`), which also
lets anyone verify a vector.

## 2. The text depends on a live wiki page

**Root cause:** `WikidataPropertyFilter.get_property_sort` reads
"MediaWiki:Wikibase-SortedProperties" at run time. That page is edited about
monthly, and it decides claim order and filtering. Separately, a newer
beautifulsoup4 than the lock's 4.14.3 parses the page to 0 properties,
silently, which leaves every claim unsorted and unfiltered.

**Suggested fix:** publish the `property_sort.json` used with each release,
and fail when the parse returns no properties.

## 3. The corpus drops information about the entity

**Root cause:** `WikidataJSONCleaner.clean_entity` removes `snaktype` (so
"no value" and "unknown value" can no longer be told apart), statement ids,
a property's `datatype`, sitelink `badges`, and the entity's
`modified`/`lastrevid`.

**Suggested fix:** keep them. They are a few bytes per statement, and
`modified` is what a user needs in order to know which vectors are current.

## 4. Dataset card

The card says 44.57 M vectors. The files hold **53,666,646**: en 22,667,152,
fr 11,978,401, de 11,180,375, ar 7,840,718 (from every Parquet footer).
