# One parse: every stage reads the record (2026-10-31)

*Operator, 2026-10-31: either combine indexing with embedding, or
decompress the records, "smart, decompress ahead so to avoid starving the
embedding"; "Make it a general rule for any collection that can benefit
from it. Again, one 'backend' for all the collections."*

## 1. What it replaces

Each stage parsed the origin on its own: the text index parsed a
wikipedia article's wikitext to index it, and embedding parsed it again
to embed it.
- **Measured** (München, 2026-10-31 16:15, wikipedia with batch 16): the
  embed producer spent 52 % of its time waiting on the parse pool for
  that second parse.
- **The parsed form already exists:** the records (09 §9.5), built once
  by `hold records build` (52.9 M for wikipedia, 2026-10-30).

## 2. The rule

**Where a document has a FRESH record, every stage reads the record
instead of parsing the origin; the origin is parsed only where there is
none.**

A record is fresh when all three hold:
- the current snapshot holds the document's uid (`snapshot.shards_of`,
  then the part's uid map);
- its `origin_hash` equals the manifest row's `txt_hash`, so it was
  parsed from the same origin bytes;
- the plugin's `PARSER_VERSION` equals the Codex's `parser.version`, so
  the same parser made it.

When those hold, the record's sections are what a parse would give
(invariant 3: the chunks are a pure function of that text). The chunks
are still cut by `layers.chunks_for_document`, as `hold.land.cut` and
`hold vectors build` cut them (invariant 1).

**Kept honest:**
- At a sampled rate, the stage parses anyway and compares the sections
  byte for byte.
- A mismatch is a stale record or a parser that changed without its
  version. It stops the shortcut for that collection: a mark under its
  state directory, cleared only by a person. The parse is used meanwhile.

**One mechanism for every collection** (`app/record_source.py`): the parse
pool's per-document work (`parallel._one`) and the worker's in-thread cut
(`_load_chunks`) ask it first. No plugin code changes. A collection with
no records (a deposit collection, a new one before its first build), a
row newer than the snapshot, or a stale record parses as before.

## 3. In three steps, each measured alone (scheduling.md §6)

1. **The record source** for the text index and for embedding:
   - decompression (the part's zstd frame, cached per process) in place of
     the origin parse;
   - in the pool's children, so the work stays on the cores the profile
     allows.
2. **Reading ahead**: the next group's chunks are prepared while the
   current one is embedded, bounded in memory, so the encoder never waits
   on preparation.
3. **One parse for both stages, where a document has no fresh record**:
   - the text index hands the chunks it cut to embedding through a bounded
     queue;
   - a full queue never makes the index wait: embedding then reads that
     document's record once it exists, or parses it.

## 4. What it costs, what it buys

| | Pros | Cons |
|---|---|---|
| Parse in every stage (before) | No dependency on records | The costliest per-document step, done twice. Embedding waits on it; the index competes with embedding for the same cores |
| Records first (this rule) | One parse per document per version, at the records build. Decompressing a frame is far cheaper than parsing wikitext, and the chunks are the published record's by construction | Records must exist and be fresh; a part's index and frames are read per process (cached); the sampled check costs a parse in N |

**Standard alternative, for the record:** a shared columnar store of
parsed text (Arrow/Parquet sections beside the manifest) would serve
every stage without decompressing JSON lines. Our records are already
that store, compressed, so nothing new is added.
