# wikidata and gutenberg-graph — the build plan (2026-10-19)

*The operator decided D1–D5 as recommended on the evening of 2026-10-19
("Go ahead with the decisions as you recommended, D1 to D5"); D6 on
2026-10-18. The design is [wikidata-plugins.md](wikidata-plugins.md), the
mapping [wikidata-implementation.md](wikidata-implementation.md). This note
is the ORDER OF WORK, the decisions the mapping left to the build, and the
measurements still owed. The gutenberg graph is called `gutenberg-graph`.*

## 1. The decisions, fixed

| | decided | what it fixes |
|---|---|---|
| D1 | the official Wikidata JSON entity dump (`dumps.wikimedia.org/wikidatawiki/entities/<date>/wikidata-<date>-all.json.bz2`), the current one | `origin.form`: the dump, not the HF corpus (its cleaner drops `snaktype`, statement ids, `datatype`, badges, `modified`, `lastrevid`); `cutoff_rule: dated-dumps`; items AND properties (the lexeme dump is separate and not taken) |
| D2 | no bulk import of Wikimedia's vectors; ask Wikimedia for the chunk texts; meanwhile embed nothing in bulk | no `kind: "archive"` credit at creation (the Codex is written once: an archive adopted later needs a daughter); the collection embeds through the worker like any other, at night |
| D3 | one layer; the language list a parameter; a language added = a daughter | see §3: the carry-by-hash rule of 06 §6.3 does the rest |
| D4 | ~~Wikibase's `wbformatvalue` in a pinned image~~ **REVISED 2026-10-19 (operator: "D4 Python")**: dates, coordinates and quantities formatted by a deterministic Python formatter of our own (`app/plugins/wikidata/format.py`), vendored beside the textifier; no service beside the parser | the text is a pure function of the dump and the plugin's code (invariant 3); the formatter's version is the parser's; byte parity with Wikimedia's own texts is not sought (D2 never relied on it) |
| D5 | gutenberg-graph = an annotations collection amended (A-K1..K4) | written into 02 §2.10, 05 §5.5, 09 §9.5/§9.8/§9.14 when its build starts, after wikidata |
| D6 | `tokenizers` (Hugging Face, Rust) in the platform image, pinned | the chunker cuts at tokens of jina-v3's tokenizer |

## 2. What the mapping left to the build — decided here

**Ids.** The dump holds items `Q<n>` and properties `P<n>` in one id space
per letter. 06 §6.1: a `native_id` is a string the platform parses only
under `slicing.order: numeric`, and `order_key` wants an integer. So, as
Wikipedia encodes the language in its id (`plugins/wikipedia/ids.py`),
the kind is encoded in the number: an item's `native_id` is its number
(`Q42` → `"42"`), a property's is `1_000_000_000 + n` (`P31` →
`"1000000031"`; QIDs are below 1.5 × 10⁸ and grow by ~10⁷ a year).
`uid = wikidata:<native_id>`. The entity's own id goes in `keys` as the
canonical id `wikidata:Q42` / `wikidata:P31` — the namespace
`wikidata-refs` registers (§4) — and in `extra.id`. The rule is a pure
function of the origin's key, recorded here (06 §6.1).

**What is a record.** One entity, one record. `title` is the label in the
first language of the list that has one (else the id); `lang` is `"mul"`
(an entity has no one language; 09 §9.5 asks for 639 codes and `und`;
`mul` is ISO 639-2's "multiple languages" and 639-3's); `creators` `[]`;
`date` null; `subjects` `[]`; `keys` the entity's canonical id and the
ids of its `P31` (instance of) and `P279` (subclass of) values — what it
is about, in the sense a reference source resolves; `extra` keeps the
WHOLE entity as the dump states it minus what `sections` carries:
`type`, `id`, `labels`, `descriptions`, `aliases` in every language,
`claims` whole (every statement with its id, rank, qualifiers,
references, `snaktype`, `datatype`), `sitelinks` with badges, `lastrevid`,
`modified` (invariant 11: the dump form is the one that keeps them).
`sections`: one section per language of the Codex's list, heading the
language code, text the TEXTIFIED entity in that language — Wikimedia's
textifier, vendored at the pinned commits — so `content_hash` covers the
text that is embedded, and the chunker is a pure cutter.

**Pruned entities.** Scholarly articles (`P31` = `Q13442814`, any rank)
are not records: ~46 M of the ~115 M entities, as the design's corpus
left them out; pruning whole entities is allowed (invariant 11), the rule
is the plugin's and recorded here and in `origin.form`. Nothing else is
pruned: items without a sitelink stay (the design keeps them).

**Why textification is the PARSER's and not the chunker's.** The text of
Q42 names the labels of Q5, Q30, … — other entities' lines. Chunking is a
pure function of the cleaned text (invariant 3), so the chunker cannot
look labels up; the record's `sections` must already hold the text.
`parse()` therefore reads the entity's bytes AND a **label table** of the
same dump — a derived, local artefact the plugin builds in its first
streaming pass (`labels.sqlite`: id → label per language, descriptions
not needed) — and formats dates, coordinates and quantities itself (D4,
revised: `format.py`, no service). Two consequences, both amendments
(06 §6.4, §3 below): `origin_hash` still covers the entity's own line
(the unit a query returns), and a verifier reproducing a record needs the
same dump's label table, which the snapshot pins by naming the dump
(`origin.spec.files`) and which is rebuilt deterministically from it.

**The pack.** The all-dump is one file of ~100 GB bz2, ~1.5 TB of JSON;
nothing in it is addressable without a pass. As PubMed's containers have
their pack (`plugins/pubmed/pack.py`), the dump gets ONE streaming pass
that writes a Parquet pack (one row per entity: index, id, type,
`modified`, the line's bytes zstd-compressed; row groups of 1,000) and the
label table at the same time. `discover` reads the pack's index columns;
`fetch` reads one row group; `origin` is `<dump rel path>#<entity index>`;
`unit` is `container-member`. Whether the bz2 is multi-stream (pbzip2
blocks, seekable with an offset index) is measured first (§5): if it is,
the locator can name the block and the pack shrinks to an index.

**Slicing and parts.** One id-range key, `range` = `native_id // 10⁷`
(slices `range=0`, `range=1`, …, properties in `range=100`), `order:
numeric`, `shard_records` 20,000 (≈ 50,000 chunks a part at 2.5 chunks an
entity over four languages; PubMed's parts are 30,000 records) — about
5,000 parts over the ~69 M kept entities. Pinned only after the
measurement of §5 (entities per range, chunks per entity).

**The chunker `wikidata-text` v1.** Per section, the text cut into
windows of `max_tokens` tokens of the pinned tokenizer (its sha256 a
parameter; D6), the window boundaries at the textifier's own claim
boundaries where Wikimedia's `chunk_item_text` puts them (their code,
vendored), so a chunk of ours is a chunk of theirs for the same text. No
overlap. Chunk ids `<uid>:<seq>:1` in section (language) order. The
tokenizer's file is fetched at image build from the Hub at a pinned
revision and checked against the sha256 the layer names.

**The space.** jina-v3 (`ab036b0`, remote code `bd55a5e`), dim 512 (the
Matryoshka cut Wikimedia used), f16, normalised, `pooling` mean,
`retrieval.passage` for documents and `retrieval.query` for queries (an
asymmetric space: `query.encoder` the same weights, another adapter),
`ctx` 1,024 with `cut: tokens` (version 8), engine `louise-python` task
`jina-v3` behind VeGa on the B70 — installed on the node when wikidata is
created (a new tree is a new run: the five live models re-stated). Encoder
determinism (invariant 12): torch's controls are not yet in the wrapper's
`DETERMINISM` table — measured before the gate, as approved-encoders.md
asks, with `torch.use_deterministic_algorithms(True)` and the XPU
equivalent of `CUBLAS_WORKSPACE_CONFIG` tried first. `work`: tokens per
month from the throughput measured (45 chunks/s at fp32 batch 32).

**Licence.** CC0 corpus → green, no terms text; the encoder's CC BY-NC
4.0 is the space's, on the card (commercial peers consume, never
contribute, as DAR-legal-it). `corpus_provider` "Wikidata contributors
(Wikimedia Foundation)", attribution the CC0 statement.

**Reference source.** `wikidata-refs` registers the namespace `wikidata`
(QIDs and PIDs); table `wikidata_entity(id TEXT pk, kind, lang, label)`,
one row per entity with ONE label — the first of the pack's languages
that has one, `mul` last, the preference the plugin's `title` follows —
synced from the plugin's own label table (`labels.sqlite` beside the
dump) of the newest dump under `<ref_root>/wikidata` whose pack is
COMPLETE (06 §6.5: node-local, never a member). Keys are written only
after it exists (memory: keys-registered-only). *Built 2026-10-20
(app/refs/wikidata.py; ref_version 2 — the first shape keyed `Q42|en`
and no lookup by canonical id could reach it):* `louise dev refs-sync
--src <collections root>` syncs it, as it syncs `normattiva-refs` from
`<ref_root>/normattiva`; the store commits per batch of 50,000 (a 10⁸-row
sync cut off keeps its rows and is not recorded as done) and re-syncs when
the pack's meta or label table is newer than the last sync. `ask lookup
wikidata:Q42` is the fact; the `keys` restriction (05 §5.2) is what the
namespace buys. OPEN, two decisions: (1) NO RESOLVER — a label typed in
a question is not resolved to an entity: the gene resolver's in-memory
dictionary does not exist at 10⁸ entities, and an exact-label index is
several GB on /data; gutenberg-graph's entity linking goes through the
collection's VECTORS by design, not this table. (2) SIZE: the table
duplicates the pack's labels (one language, ~10⁸ rows, order of 5 GB in
refs.db) — measure after the first node sync before deciding whether
refs.db should ATTACH the pack's table instead. Multi-language labels
stay in the pack; the row carries one.

## 3. Spec amendments

- **06 §6.3** — a row for `wikidata-text` in the chunker table.
- **06 §6.4** — "A parser whose text depends on other units of the same
  origin": the plugin builds a derived table of the pinned origin (the
  label table) and names, in `origin.form`, what of the text comes from
  it; `origin_hash` keeps covering the document's own bytes; a verifier
  reproduces the record from the same dump. The clause "a parser that
  runs a pinned service beside it" is WITHDRAWN with D4's revision: no
  service, the formatter is code.
- **02 §2.7** — adding a language is a daughter whose records gain a
  section: the recut is carried by chunk hash (06 §6.3), so the mother's
  vectors stay and only the new language is embedded. No new rule, one
  sentence that says so (the A-W1 of the mapping, reduced).
- **D5's A-K1..K4** — when gutenberg-graph's build starts.

## 4. Order of work

1. ✔ Decisions; this plan; `wikidata-refs` (the namespace).
2. The dump measured on the node (§5); the textifier and embedding repos
   cloned there at the pinned commits (this container cannot reach github,
   wikidata.org or dumps.wikimedia.org).
3. The plugin: dump reader (streaming, truncated-stream safe), pack and
   label table, `discover`/`fetch`, ids, pruning; tests on dump-shaped
   fixtures (three real entities from the node's probe).
4. ✔ (2026-10-19 night) The textifier vendored (BSD-3 notice kept;
   `textify.py`: the dataclasses and `to_text` as theirs, the normaliser
   adapted — labels from a callable over the label table, aliases in dump
   order, a missing value label rendered as the id) and the formatter of
   our own (`format.py`: time by precision and calendar, coordinates in
   DMS by precision, quantities with their unit label; en/de/fr/es/it
   month names, ISO elsewhere) — tests over the real entities. Still to
   do here: `parse` producing the sections; `records-vs-origin` on the
   sample.
5. ✔ (2026-10-19 night) The chunker `wikidata-text` (`chunker.py`:
   Wikimedia's rule over our rendered text, the instance-of line first and
   sticky, a tokenizer named by sha256 under `TOKENIZERS_DIR`, registered
   in app/chunking.py) and D6 (`tokenizers==0.22.1` in requirements.txt —
   München rebuilds the gate image); the slicing scheme `range=` in
   app/pack/slicing.py. ✔ (same night) The pack, the label table, the
   source (pack.py, source.py). ✔ (same night) The acquisition spec
   (`WIKIDATA_SPEC`: the dated dump + the run's md5sums/sha1sums, two
   connections, `dated-dumps`, the snapshot index keeping only the dated
   directories that hold a full dump), the registry entries (the plugin is
   now `wikidata` in the Library's list), the origin-coverage exemption
   (the label table, argued), and `layers.check_params`: a wikidata-text
   layer without its tokenizer's sha256 is refused before a Codex is
   minted. The recipe is a TEMPLATE, docs/notes/wikidata-recipe.toml, with
   four values only the node can give (marked <<...>>): the tokenizer's
   sha256, the weights digest, the stated run, `work.tokens`.
6. The jina-v3 task installed on the node (2026-10-20: the weights folder by deploy/jina-embeddings-v3/fetch.py — the five weight files and the ten remote-code modules as regular files, checked against the commits' trees and the layer's tokenizer sha256, the digest printed; the YAMLs deploy/vega/stage/models/vega-jina-v3*.yaml; the backend rebuild by docs/notes/louise-python-rebuild.md, München's heavy window; the tokenizer's sha256 is in: `f59925fc…`; the folder installed on the node the same day: code commit `bd55a5ec…`, weights digest `sha256:f0f752ae…`, both pinned in fetch.py and in the recipe template — two of the four values; the stated run and work.tokens wait on the rebuild); the calibration set embedded;
   determinism measured; `hold collection create wikidata`; the first
   fetch (~100 GB to tank) and build (the pass over 1.5 TB of JSON:
   measured, resumable at entity granularity).
7. gutenberg-graph: the amendments, then mentions over gutenberg (GLiNER),
   entity linking against wikidata's vectors, relations, communities — as
   wikidata-plugins.md orders them.

## 5. Measurements owed before pinning (asked of München, 2026-10-19 evening)

- the dump directory: dated dirs, the latest's files, sizes, checksum
  files; `Accept-Ranges`;
- the first entities of the latest all.json.bz2 from an 8 MB Range: the
  array framing, the trailing comma, items vs properties, JSON sizes;
  three real entities as fixtures;
- whether the bz2 is multi-stream (several `BZh91AY&SY` headers in the
  head);
- entities per 10⁷-id range and chunks per entity (from the probe's
  textifier run on a sample), for `shard_records`;
- that the two repositories clone at the pinned commits, with licences.

### Answered by München, 2026-10-19 evening (read-only, from the node)

- Dated directories: 20261007, 20261009, 20261012, 20261014, 20261016.
  Only 20261012 is a full run; 20261014 is lexemes + truthy-BETA and
  20261016 lexemes-BETA only. `latest-all.json.bz2` IS 20261012:
  103,409,622,204 B, Last-Modified 13 Oct 2026 07:58:53 GMT, md5
  `7afa3658634fb291fb5c8672631d3fb9`, `Accept-Ranges: bytes`; the
  directory's `md5sums` (401 B) and `sha1sums` (449 B) are the checksum
  files the acquisition spec names. The .gz is 156.5 GB; the RDF forms
  are larger still. So the fetch is ~103 GB and the spec's
  `cutoff_rule: dated-dumps` must take the newest directory that holds an
  `all.json.bz2`, not the newest directory.
- Framing, from an 8 MB Range (24 s): one bz2 stream so far (the
  multi-stream question is still open — the head alone cannot answer
  it), ratio ~9.2, line 0 is `[`, one entity per line, EVERY line ending
  `,` (the reader strips it; the last line's absence of a comma is not
  relied on). 968 complete entities, all items, Q31…Q8269 — no property
  this early; JSON sizes min 2,300, median 48,042, max 2,309,540 B.
- Pitfalls the parser must handle (fixtures: `tests/fixtures/wikidata-mini/dump-sample.jsonl`,
  Q2711 and Q3331 byte for byte):
  - P625 `globecoordinate` carries FLOATS (`45.772677777778`,
    `2.7777777777778e-06`): 09 §9.1 hashes no float, so the record writer
    renders coordinates as decimal strings from the dump's own JSON text
    (repr of the parsed float is not the origin's text — keep the text);
  - statement ids differ in case within one item (`Q3331$…` and
    `q3331$…`): compared case-insensitively, kept as written;
  - `sitelinks` include `commonswiki` `Category:…` titles — not an
    article, kept under `extra.sitelinks` like the rest.
- Repositories cloned on the node at the pinned commits, tarred without
  `.git`: WikidataTextifier @ 0befd7c (22K; BSD 3-Clause, Wikimedia
  Deutschland e. V., 2025) and WikidataTextEmbedding @ c9dc819 (106M, of
  which evaluation/ is 106M; src 74K; MIT, Jonathan Fraine, 2024). The
  textifier's `src` is what gets vendored; asked for its files.
- ✔ Multi-stream: the first 64 MiB hold ONE stream header (BZh91AY&SY
  at offset 0), 840 byte-aligned block magics, no end-of-stream marker —
  a single-stream file of many blocks. The fetch stays one Range-resumable
  download; parallel decompression would need a block-level bit search
  (lbzip2) or the .gz, neither taken.
- ✔ The two repositories' trees are on the branch `probe/wikidata-sources`
  (1b65fb3; never merged). München's reading: the textification is
  `src/Textifier/WikibaseTextifier.py` (12,918 B) + `JSONNormalizer.py`;
  time and coordinate values were formatted by POSTing `wbformatvalue` to
  a running Wikibase (hence their MariaDB + MediaWiki) — D4 revised to a
  formatter of our own; labels came from `LazyLabelFactory` (Action API +
  MariaDB cache) — ours from the dump's label table; the rank filter
  keeps the preferred statements of a property where any is preferred,
  else the normal ones, drops deprecated; external ids on, qualifiers on,
  references off; aliases were deduplicated through a `set`, so their
  order was NOT deterministic — ours keep the dump's order. Chunking is
  in WikidataTextEmbedding `src/utils.py chunk_item_text` (label +
  description + aliases + the sticky P31 claims + the largest run of
  further claims that fits 1,024 tokens, binary search; a single over-long
  claim trimmed by tokenizer offsets).
- Still owed: entities per 10⁷-id range; chunks per entity from a run of
  our textifier over the 968.
