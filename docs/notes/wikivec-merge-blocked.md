# The wikivec → wikipedia merge cannot carry the archive's vectors

**Measured on the node 2026-09-10, before running step 8. The answer is:
do not run it.** This is the "prove the function on ONE shard before
running the rest" the migration plan asks for; the proof failed at a
level below the one the plan was watching.

## What step 8 planned

> `wikivec` … stops being a collection and becomes **`wikipedia`'s layer
> 1**, which is preflight decision 7 (one collection, two layers:
> `verbatim` over the documents the archive covers, the default chunker
> past its coverage).
>
> Today's wikivec vectors are 12.7 million and they DO [matter] — but
> they are the archive's, so they arrive by the archive route, which
> re-proves them against this node's own encoder … **That works only if
> layer 1 reproduces the archive's function exactly**: bge-m3 as Upstash
> ran it, and the input `f"{title}\n{text}"`.

The plan expected the risk to be the **input format**. It is not. It is
three levels deeper, and any one of the three is fatal on its own.

## What was measured

`genesis._local_chunk_texts` — the function the same-function proof
compares against — derives its chunks from **the collection's own
plugin** (`collection.plugin_instance()`) and **the collection's own
layers**. So a merged `wikipedia` collection chunks with the WIKIPEDIA
parser, not the archive's. Section counts for the same articles, both
plugins, on the node:

| native_id | wikivec (`embedded`) | wikipedia (dump) | same lead text? |
|---|---|---|---|
| 12 | 63 | 24 | **no** |
| 39 | 57 | 23 | **no** |
| 290 | 21 | 17 | **no** |
| 303 | 162 | 51 | **no** |
| 305 | 68 | 31 | yes |
| 307 | 145 | 35 | **no** |
| 308 | 104 | 53 | **no** |
| 309 | 25 | 8 | yes |

Article 12 (*Anarchism*) in full:

```
wikivec    uid=embedded:12   63 sections, each ~460–810 chars, no label,
                             each prefixed "Anarchism\n"  (embed_input)
wikipedia  uid=wikipedia:12  24 sections, labelled by heading, 7–3,384 chars
```

## The three reasons, each sufficient

**1. The cuts are different objects.** Upstash emitted PARAGRAPHS; our
dump parser emits titled SECTIONS. Two to three times as many units, at
different boundaries. `chunk_verbatim` "emits one chunk per SECTION
exactly as the plugin emitted it" — so over wikipedia-parsed records it
faithfully reproduces the WIKIPEDIA parser's cut, which is not the
archive's. The proof compares position for position and every position
disagrees.

**2. The uid prefix is the plugin's, and a collection has one plugin.** A
uid is `<plugin>:<native_id>` (02 §2.3) and a chunk id is
`<uid>:<seq>:<version>` (09 §9.5). The archive's rows are keyed
`embedded:12:0:1`; a `wikipedia`-plugin collection produces
`wikipedia:12:0:1`. `possession.json` binds uids and the coverage map is
per chunk id, so these can never be the same rows. Layers partition
documents by CHUNKER; they do not change where records come from. The
merge needs one collection to hold records from two parsers, and 09 §9.3
gives a Codex exactly one `plugin`.

**3. The corpora are different snapshots of Wikipedia.** Six of eight
sampled articles have different lead text. Upstash's set is
`wikipedia-2024-06`; ours is the `latest` dump. Even where the cut
matched, the text would not — and `content_hash` is over title and
sections, so those are different records by construction, not merely
differently chunked ones.

The articles DO align by `native_id` (both key on the Wikipedia page id),
which is the one thing that works and is why the plan looked reasonable
on paper.

## What it costs, per option

* **Keep `wikivec` and `wikipedia` as two collections** — today's shape.
  12.7M vectors stay valid in the space that produced them; nothing is
  re-embedded; nothing is lost. Cost: zero. The pre-embeddings are not
  "incorporated into the collection they belong to", which was the
  operator's stated end state — but they are already credited and already
  usable, and 02 §2.7 has sisters for exactly this relation.
* **Merge and re-embed** — 12.7M vectors of bge-m3 from scratch, plus
  whatever the 19.4M-document `wikipedia` needs beyond the archive's
  coverage. Weeks on the Arc, months on CPU. This is what the plan was
  explicitly trying to avoid.
* **Merge with the archive as layer 1's source** — needs a Codex that
  names two plugins, which is a format change to 09 §9.3 and touches
  `collection_id`. Not a step-8 change.
* **Re-fetch the 2024-06 dump** so the revisions align. Fixes reason 3
  only; reasons 1 and 2 stand.

## Recommendation

Keep them separate. Reasons 1 and 2 are structural — they are properties
of how records, uids and layers are defined, not of this node's data —
so no amount of re-fetching or re-chunking makes the archive's rows
belong to a `wikipedia`-plugin collection. The end state the operator
described is reachable, but by making the two SISTERS in one lineage (02
§2.7), not by making one a layer of the other.

`pubmed` is unaffected and its plan stands: it is already the shape the
design wants, its plugin is its own, and the MedCPT archive covers
`pubmed:*` uids that its own parser produces.
