# Wikipedia parser 5: the QIDs, from the source

*2026-10-18. Operator: option 3 of the Wikipedia/Wikidata comparison, "QIDs
only", and "we would rely on another collection instead of on the source" —
so the QIDs come from Wikimedia's own `page_props` table, never from the
`wikidata` collection's sitelinks. Code: `app/plugins/wikipedia/page_props.py`,
`source.py` (parser 5); tests: `tests/test_wikipedia_page_props.py`.*

## 1. What a record holds

Each dump run publishes `<wiki>-<date>-page_props.sql.gz` beside the articles.
Parser 5 reads the run the collection pins, and every record of either layer
takes its page's rows of it (the page id is Wikipedia's, and stable, so an
archive article of June 2024 takes the pinned run's rows by its id):

- `extra.wikidata`: `wikibase_item`, the page's Wikidata item (`Q42`);
- `extra.page_props`: every other property MediaWiki holds for the page, by
  name — sitelink badges (`wikibase-badge-Q17437796`, a featured article),
  the lead image (`page_image_free`), `disambiguation`, `displaytitle`,
  `defaultsort`, the maintenance flags. Kept whole, by invariant 11: once the
  table is an origin, what it says about the page is held. A value that is
  not UTF-8 is `{"base64": …}` (MediaWiki compresses some; on articles none
  were seen).
- **`pp_sortkey` is not kept**: MediaWiki's float copy of a numeric value,
  for its own index — class (a) of invariant 11, and the row's bytes, sort
  key included, stay in `origin_hash`.

Title and sections do not move, so no chunk does (tested on both layers).

## 2. The unit and the hash

A page's rows are contiguous (the dump is in primary-key order): its unit is
the decompressed table's bytes `[start, end)` from its first tuple to its last,
a statement break included when they straddle one (37 pages in trwiki). The
locator gains a last segment, `…|<page_props>::<start>::<end>`, so `origin`
names both files (09 §9.5). `origin_hash` is BLAKE2b-128 over the page's bytes,
`\0page_props\0`, the unit's length (8 bytes) and the unit — gutenberg's form for
its catalogue entry; a page with no rows hashes as parser 4 did. Discovery's
signature covers the table (latest mtime, summed sizes), so a new table
rebuilds every record of its language, and a run whose table is not on disk
waits (a container report, never an error on a document).

gzip is not seekable: a derived Parquet pack (`page`, `start`, `end`, `rows`) is
built once per file in a hidden `.pack/` beside it, or in the temporary
directory when the origin is read-only (a check). The reader is strict: a row
it does not recognise fails the file rather than being skipped.

**An update** fetches the new run's `page_props` with its dump (the spec lists
it per run, 1.07 GB for the eleven; enwiki 458 MB). A new pinned run is a new
Codex anyway; within it only records whose rows changed differ.

Measured on trwiki-20260801: 22 MB, 1,278,431 pages with rows, 1,127,794 QIDs,
pack built in 4.8 s (enwiki, ~20× the bytes, about 100 s), every unit equal
to its byte slice of the decompressed file.

## 3. The switch on the node — RULE 1, and its order

The live collection: Codex version 2 (`b8ff5fab…`, its `space_id` holding the
TEI image), parser 4, 52,923,125 documents, 33,882,169 embedded, 144,109,540
vectors all the Upstash archive's (`genesis:upstash-wikipedia-2024-06`), no
record and no snapshot ever built. Lance 333 GB and the text index 80 GB on
ssd4: the carry MOVES them (a copy would not fit).

By the book this is a new collection that re-derives 52.9 M documents and
re-imports 144 M vectors. Carried instead, one Codex for everything due:

1. **Fix the character cut first** ([char-cut-truncation.md](char-cut-truncation.md)):
   until the encoder truncates to `ctx` tokens, the archive's long rows sit
   at 0.961 and the tolerance cannot leave 0.95.
2. **Measure the tolerance** over the calibration set across the engines a
   peer may run (the B70's SYCL, the CPU, the AMD peer's ROCm next week), and
   set it with margin under the worst of them (operator: tighter than 0.95,
   different GPUs in mind).
3. **One new Codex**: version 6 (layers with `input`, the engine frozen, the
   live `vega-bge-m3` GGUF's weights), parser 5, the measured tolerance, and
   the recipe's origin with the eleven `page_props` files (sizes in
   `acquire/specs.py`), the filter `*-page_props.sql.gz`, and the form's
   sentence naming the table.
4. Acquisition adopts the dumps on disk and fetches only the tables
   (1.07 GB, through the transfer manager).
5. `louise dev carry wikipedia <new> --rebuilt --keep producer:upstash-wikipedia-2024-06`
   (A1: v2→v6, engine; A1b: the weights differ, only the archive's vectors
   stay — they are all of them; A1d: the origin adds the tables), moving.
6. `louise dev carry wikipedia <new> --resign --yes` (A1d): the new parser's
   locators and signatures on every document, every mark and vector kept,
   the sample re-cut to the same chunks; refused while any table is missing.
7. The vector proof A1 asks before use, on the same sample: a snapshot does
   not exist to run `vectors-sample` over (to build into `--resign`).
8. The old collection paused and kept; removing it is the operator's call.

Amendment A1d (02 §2.4, 07 §7.3) is written; steps 1, 2 and 7 are not built.

**Deploying parser 5 before the switch is safe — since 2026-10-18's fix,
not before.** The first attempt (rollout phase B, step 3a) refused it: the
plugin's spec now lists 33 files per run, the live Codex pins 22, and
`pinned_snapshot` matched exactly, so no collection loaded. Worse was behind
it: acquisition listed the PLUGIN's files, so the parser-4 collection would
have fetched the tables, and its next scan would have read every document as
changed. Now a Codex pins the snapshot as its own filters select it, and a
Codex collection fetches by its Codex's filters (`acquire/spec.pinned_snapshot`,
`engine.effective_spec`). With no `page_props` on disk the live collection's
scan reports each wiki as waiting and yields no document; documents already
there keep their parser-4 locators and parse as before. A table landing in
`/corpora/wikipedia` while the old collection scans would still move every
signature, so step 4 runs with it paused (as normattiva's re-creation did).
