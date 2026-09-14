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

* **Two sources, one `origin.spec` — settled on main (8068727, the
  operator's choice).** `origin.spec` stays the dumps' spec, shape
  unchanged; a record whose text a credited archive supplies carries its
  `origin` as a URL into that archive (06 §6.4, 09 §9.5), and a record
  located in an archive the Codex does not credit is refused at build.
  Code still to follow on this branch: the archive records' `origin`
  becomes that URL (today `data/en/000.parquet#2::3`, relative), and
  `hold records build` gains the refusal.
* **The encoder's window: 8,192 tokens** (operator: the minimum that fits
  both layers). Wikivec's vectors were made by `SentenceTransformer`
  bge-m3 (the dataset card), whose `max_seq_length` is 8,192 — and the
  node's endpoint truncates at 2,048: an archive row past 2,048 tokens
  re-embeds there at cosine 0.90–0.98 against its own vector (18 rows of
  3.1k–16.7k tokens, shard 000 of en, de and ja), a short row at 1.000000
  (2026-09-14). Rows up to 16,723 tokens exist, so only exactly 8,192
  reproduces them. Layer 2's largest CDC piece is 7,103 tokens (zh; ja
  6,350, fa 4,788, en 4,225 — the 64 longest pieces of three shards per
  language), inside it. The endpoint must serve 8,192 before the
  recipe is written, and the comparison is repeated there.
* **The worker's origin root.** The worker discovers every collection
  from its TOML's `corpus_dir`, so both origins must appear under one
  root in the worker too — a compose mount, the operator's to approve.
  `hold records build --corpus` takes one root on the command line.
* **A dated dump, not `latest`.** A record's `origin` must name bytes that
  can be re-fetched; `latest` moves monthly. The node's `enwiki/latest` IS
  the 20260801 dump — both files' sha1 equal Wikimedia's published sums
  (2026-09-14) — and all ten other wikis are offered at 20260801
  (45.2 GB), so the collection pins 20260801 (`Collection.snapshot`, the
  spec's `dated-dumps`).
* **A pinned archive revision, not `main`.** The node's archive is
  Upstash's revision `ce4e0ea49276d99975816b8bc85a85bf416b8b62` (last
  modified 2025-02-17): `data/en/000.parquet` and `data/tr/000.parquet`
  equal the Hub's LFS sha256. The other 1,445 shards are checked the same
  way before records are built. An archive record's `origin` URL pins
  that revision (`…/resolve/<revision>/data/…`), never `main`.
* **The corpus is the records** (02 §2.3): one drag, its shards cut from
  both layers — the archive's articles and the dump's pages. Wikivec's
  parquet and the dumps are ORIGINS only, never a second corpus (operator,
  2026-09-14: "read the manual"). What is left is local: discovery reads
  the two origins, which sit under two separate mounts on the node.
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
