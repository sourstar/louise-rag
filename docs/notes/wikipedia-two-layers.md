# Wikipedia: one collection, wikivec's languages, two layers

**The plan (operator, 2026-09-10, restated 2026-09-14).** `wikivec` and
`wikipedia` become ONE collection. It holds the same languages as wikivec
(en, de, fr, ru, es, it, ja, pt, zh, fa, tr), one encoder (bge-m3), two
layers:

| | layer 1 | layer 2 |
|---|---|---|
| documents | the articles the Upstash archive covers | every other article of each language's dump |
| text | the archive's own text (its paragraphs, June 2024) | the Wikimedia dump (parser 3's cleaning) |
| chunker | `verbatim` — the archive's cut, one chunk per paragraph | `cdc` |
| vectors | the archive's 118.8M, imported and re-proved as today | embedded with bge-m3 |

[`wikivec-merge-blocked.md`](wikivec-merge-blocked.md) measured a version
of this that fed layer 1 with the DUMP's text — the dump's sections of a
newer snapshot, which the archive's vectors cannot match. Layer 1 reads the
archive's own text, which is what the archive's vectors were computed
from. That note's three reasons do not apply to this shape.

## How it maps onto what the documents already allow

* **Which layer a document is in — by id, from the Codex.** 06 §6.3's
  `archive:` rule reads the archive's `coverage` from the Codex's credits,
  and 09 §9.3 admits `{ids_per_shard, shards}`. So the native id carries
  both the language and the source: an archive-read article keeps
  wikivec's own number (`ordinal × 10^10 + page id`, the frozen table), a
  dump-read one is numbered from 10^12. Coverage `{ids_per_shard: 10^10,
  shards: 11}` is exactly the archive's ids. (`wikipedia` parser 4.)
* **One source per article, recorded per record.** The plugin reads the
  archive's text where the archive covers an article and the dump
  otherwise, and writes which in `extra.source` — invariant 11's "where
  several origin forms exist, choose and record", as gutenberg chooses
  epub or plain text book by book.
* **The archive's vectors under Wikipedia's names.** The archive's
  `uid_prefix` is ours, not the data's: `wikipedia:` instead of
  `embedded:`, over the same numbers.

## Still to settle

* **The Codex's `origin` names one acquisition spec** (09 §9.3, §9.17);
  this collection has two sources, the dumps and the archive. A layer-1
  record's `origin` can already be the archive's URL (§9.5 admits one);
  whether `origin.spec` becomes a list or the archive is reached through
  its `credits` entry is a spec amendment, on main.
* **Acquisition of ten more dumps** — 45.9 GB beside the English 27.1 GB
  already on the node (the spec's `all`, 73.0 GB, probed 2026-09-14).
* The legacy `wikipedia` and `wikivec` stay parked in
  `config/collections-legacy/`, data untouched, until this collection
  exists (codex-only rule of 2026-09-11).

## Done

* 2026-09-14, step 1: language-bearing ids (parser 4), all eleven
  languages in the spec (additive profiles, `demo` the smallest, Turkish),
  simplewiki refused as not one of the languages — it used to pass as "en"
  and share enwiki's page ids, which the old collection's 19.4M documents
  did.
* 2026-09-14, step 2: the plugin reads both sources. Discovery walks the
  archive's shards first (wikivec's own ids), then each dump, skipping every
  page the archive holds — the archive's page list is read from the `id`
  column and cached per shard, independent of the scan cursor, so a
  re-scan that skips the archive still mints no dump twin. An archive
  article parses to exactly the sections the legacy plugin makes from the
  same rows (tested against it), so `verbatim` cuts the chunks its vectors
  belong to; `extra.source` says which source a record came from. The
  archive's reading moved from `embedded` into `wikipedia/archive.py`
  (`embedded` imports it back until wikivec is migrated), and a record's
  `origin` locates both shards of an article that straddles two.
* 2026-09-14, step 3: the archive route. The Upstash entry declares its
  eleven language blocks (`{ids_per_shard: 10^10, shards: 11}`; it said
  "all", which would have put every document in layer 1), and 06 §6.3's
  `archive:` rule reads it — from the Codex's credit, or from the registry
  before a Codex exists. Its vectors land under the collection's ids
  (`wikipedia:`; the legacy wikivec keeps `embedded:` over the same
  numbers). The candidate walk takes archive shards only, so a dump page
  is never read as one. Under "all" the local encoder holds off the
  archive's declared range except for what no read covers: straddlers, a
  rejected shard's documents, text that would not load. A rejected shard
  is not re-read. The plugin declares the two layers (`verbatim` by the
  archive rule, `cdc` by default). Measured on the node's wikivec manifest
  (22.0M documents, 4.23M eligible and not embedded): the encoder's
  backfill with the range held is an index search, 2.05 s from a cold
  cursor and at most once per 120 s cursor lifetime (a local import never
  resets it); the candidate walk at its cap costs 0.21 s. The first cost
  follows the archive's import backlog, not the corpus.

## Next

* The recipe (two layers, bge-m3) and the corpus layout on the node: the
  archive's `data/` and the dumps under one corpus directory.
* A page the archive covers must be known BEFORE the first scan: the
  archive is frozen and already complete on the node (1,200 shards), but a
  shard landing after a dump page was minted would leave that page's dump
  twin in the manifest (discovery never prunes).
