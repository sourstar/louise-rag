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

**Names in search** (*operator, 2026-10-24: "names only"*). The text stays
English (D3), but every label and alias of an entity, in every language, is
searchable by name: index-only keys on the record's first chunk, ranked
longer-name-first then by sitelinks (05 §5.1 amended; `fts.search_names`).
The records are unchanged.

**What is a record.** One entity, one record. `title` is the label in the
first language of the list that has one, else `mul`, else — parser 2,
operator 2026-10-24: "showing an id is meaningless for humans" — the name
by names.py's order (the original name in Latin characters, any label in
Latin characters, the original name, any label), the id only for an
entity with no label and no name statement; `lang` is `"mul"`
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

## Created (2026-10-20)

The collection exists on the node since 10:57:00Z (München), recipe
1de5d1c: `collection_id sha256:ed34502a…d176d`, `space_id
sha256:bb514f97…2724`, Codex version 8 (the token cut; two stated runs of
louise-python-20261020c behind VeGa), `work.tokens` 430,000,000 a month (the
operator's active count: two hours a day at the measured 2,016 tokens/s).
What the day cost: five refusals or pre-check failures of the recipe — the
calibration set, `--query-encoder`, a `label` on the query encoder, the
`[origin]` block, `sample = 512` — two of them AFTER 28 minutes of
embedding; `create` now builds and validates the Codex with placeholder
hashes before it asks the card (app/hold/collection.py `_dry_build`).
`snapshot = "20261012"` lives in the held file: the Codex's origin.spec
pins no files. Next: the fetch and the first streaming scan (§2), the first
snapshot, the gate against the document run, then §4.5's model-identity
check is done and gutenberg-graph's methods (gutenberg-graph-amendments.md,
steps 4–5) can run against its vectors.

## Parser 2 (2026-10-24): a referenced label falls back to `mul`

The vendored textifier looked a referenced entity's label up in the
language, then in the fallback language. Upstream's `get_lang_val` — and
our rendering of the entity's OWN label — tries `mul` in between. Since
2024 `mul` is Wikidata's default label, the name in every language, so an
entity labelled only in `mul` was written as its id (`Q…`) in the text of
every entity naming it. Found while designing the section below, before
any record existed: `PARSER_VERSION = 2` (textify.py `Normalizer._chain`;
tests/test_plugins_wikidata_textify.py). Parser 2 also carries Italian
connectives (W1 of upstream-reports/wikidata.md), inert for an English
collection. Counted on the node (München, 2026-10-24,
`tools/wikidata_coverage.py mul`, 8,823 kept entities in 100 row groups —
a clustered sample): 280 English texts change (3.2 %); of 155,247 label
lookups, 870 were written as an id by parser 1 and 506 by parser 2. What
parser 1 wrote as ids: "PlayStation 2" (Q10680), "Marcel Vibert",
"Bodendenkmal", and taxa — Suidae, Rallidae, Bovidae — whose scientific
name is their `mul` label. The label table: 93,171,448 English rows,
20,594,687 `mul`, 10,598,629 entities with `mul` and no English.

Titled by their id (no English, no `mul` label), counted exactly over the
node's manifest: 15,765,162 of the 75,896,183 kept records, 20.8 % — about
one in five, not the one in four first read off 20 entities; 18.1 M of the
dump's 121.9 M entities (14.9 %) have neither. The examples are such
entities as Q3235171 (fr "Les Roses de la vie", de "Die Rosen des
Lebens", hu "Az élet rózsái"), Q3242104 (ru, fr) and Q3239462 (fr "ligne de
Charpal", nl "spoorlijn Charpal").

**No id where a name exists** (operator, 2026-10-24: "As a general rule
showing an id is meaningless for humans; in case there are multiple non
English languages use in order: 1. the original name if it can be
determined and if it's in latin characters 2. Any other language in latin
characters. 3. The name in the original language if it can be
determined"). names.py, in parser 2, everywhere an id would be read:
- the order: the ORIGINAL NAME — what the entity states as its own name
  (native label P1705, name in native language P1559, official name P1448,
  title P1476, name P2561), then its label in its ORIGINAL LANGUAGE where
  it states one (language of the work or name P407, original language of a
  film or series P364, native language P103 — each a language item, read
  as its Wikimedia code P424 through the pack's `language` table) — in
  Latin characters; else a label in Latin characters; else the original
  name in its script; else any label (the general rule). Among labels
  (operator, the same day: "Go with your recommendation and the language
  lookup"): the languages of the entity's own wiki pages first, then
  French, Spanish, Italian, German, then the order Wikidata lists them,
  then the code. A place's official language (P37, on its country) would
  determine more originals — written down, not built;
- where: the record's `title`, the entity's own name at the head of its
  text, the name of every entity its text refers to, and the reference
  source's label (`ask lookup`);
- the pack (PACK_VERSION 2): `labels.sqlite` gains `language(id, code)`
  (every language item's P424, written with the chunks) and `name(id,
  lang, label)` for every entity with no English and no `mul` label,
  written by a pass over the pack's own lines AFTER the chunks — a name
  may need the code of a language item later in the dump. RULE 1, named: a
  complete version-1 pack is UPGRADED in place — a pass for the codes
  (only lines naming P424 are parsed), a pass for the names (a byte check
  skips every line holding an English or `mul` label) — not rebuilt from
  the bz2; chunk files and label rows stay byte for byte those a version-2
  pass writes (tests/test_plugins_wikidata_names.py). Parser 2 parses
  only from a pack whose names are done (`pack.ready`); before that every
  document DEFERS, so no title or text is written without its names.

The Codex of 2026-10-20 names parser 1, so wikidata is CREATED AGAIN from
the same recipe before Monday's records build: nothing was published (no
snapshot, share off), so nothing is superseded. RULE 1, named: the
calibration's document side is reused (`hold collection create
--calibration-from wikidata`, only the query side embedded); the pack is
a cache of the dump and stays; the text index was to be rebuilt on Monday
anyway. The new collection's manifest is discovered again from the pack's
index columns.

## Full text in more languages for monuments, buildings and events (operator, 2026-10-24)

*"I want that also monuments, buildings and events are full text translated
as well / As ancient and uncommon words (barbacane, barbican) if it fits /
Any improvement to wikidata goes in a report to be submitted to the
wikidata team for integration."*

**How.** A daughter (D3, and 02 §2.7's sentence on it) after the English
collection is built. For the entities of the selection, its parser writes
one more section per added language: the same textifier, fed Wikidata's
OWN labels in that language. That is the translation Wikidata already
holds — a property's label exists in nearly every language, a value's
label where editors wrote one, else `mul`, else English. Not machine
translation, for three reasons:
- a record is reproduced from the dump (records-vs-origin, invariant 11);
- translating before embedding made retrieval worse where we measured it
  (translate-before-embed.md, classical-translation.md), and MADLAD costs
  seconds a passage on the shared card;
- jina-v3 reads Italian, German, French and Spanish as they are.

The English sections stay byte-identical, so their vectors are carried by
chunk hash: only the new sections are embedded.

**The selection** is a pure function of the dump:
- every entity whose P31 is in the P279 closure of architectural structure
  (Q811979: buildings, bridges, towers…), monument (Q4989906),
  archaeological site (Q839954), event (Q1656682) or occurrence
  (Q1190554);
- every CLASS in those closures. "Ancient and uncommon words" fit here as
  the classes they are: a barbican ("barbacane") is a class of
  fortification; the run says which roots reach it (`--find`), and whether
  a root such as architectural element is needed for words like merlon;
- every entity with a heritage designation (P1435).

Words as words are Wikidata's lexemes, a separate dump D1 did not take:
a collection of their own, if ever wanted.

**The pack** needs the added languages in its label table. Today a
language change rebuilds the whole pack from the bz2; the chunk files do
not depend on the languages, so a labels-only pass over the pack's own
lines is the RULE 1 shortcut (to build with the daughter).

**Measured first**, read-only on the node (`tools/wikidata_coverage.py
classes`):
- entities and classes per root;
- per language, how much of their text Wikidata's labels write;
- each added language's cost in chunks and tokens, through the real
  chunker on a sample;
- P279 cycles in the closures, and the most-referenced entities with no
  label in each language: W3 and W4 of
  [upstream-reports/wikidata.md](upstream-reports/wikidata.md).

**Measured** (München, 2026-10-24, dump 20261012, 6 processes, 51 min;
outputs in [wikidata-coverage-2026-10-24/](wikidata-coverage-2026-10-24/)):

| root | classes in the tree | instances |
|---|---|---|
| built structure Q811979 | 24,378 | 5,640,728 |
| — of which building Q41176 | 8,164 | 2,949,836 |
| monument Q4989906 | 1,431 | 672,737 |
| archaeological site Q839954 | 595 | 363,793 |
| planned event Q1656682 | 56,778 | 1,441,964 |
| occurrence Q1190554 | 442,711 | 3,895,782 |
| heritage designation only (P1435) | — | 513,755 |
| **selected, all together** | | **10,437,567** |

- Wikidata's own names for them (label or `mul`): English 72.5 %, German
  25.3 %, French 21.2 %, Spanish 12.7 %, Italian 11.6 %. The rest of the
  text is better covered: of the 192 M labels their texts look up (118 M
  of them property names), English 99.0 %, French 94.2 %, German 91.4 %,
  Spanish 89.4 %, Italian 86.0 % exist.
- One more language for all of them: 10.5 M chunks, 1.61 G tokens (a
  sample of 10,359 rendered through the real chunker: 1.01 chunks and 154
  tokens an entity) — 3.7 months of wikidata's `work.tokens` (430 M a
  month), 222 h of the card at 2,016 tokens/s. Four languages: 15 months.
- Only where Wikidata names the entity in that language: it 1.21 M, de
  2.64 M, fr 2.21 M, es 1.33 M — 7.4 M sections, 1.14 G tokens, 2.7 months
  for all four. Those are the sections whose name is in their language;
  the others would be an English name in a translated frame.
- "Barbacane" fits: barbican (Q23394) is a subclass of built structure and
  building, and the Italian-only class "Barbacane" (Q3634551) sits under
  it; merlon (Q23419) comes in through its class. Machicolation (Q23398)
  does not: it is under architectural element (Q391414), a root to add for
  such words.
- The occurrence tree is not "events" as a reader means it: clinical
  trials (391,649), doctoral theses, statutory instruments, parliamentary
  motions, red giant stars and coins come in through chains of P279
  (W5 of the report). Its roots are re-measured with each class's chain
  (`--top`, the path per class) before the daughter: planned event, and
  the classes of occurrence that are events.
- No textifier error on 10.4 M entities.

**When parser 3 lands.** The daughter's parser is a new PARSER_VERSION
(the Codex binds `parser: {id, version}` and no parameters, so the
languages and the roots are the version's), and an image whose wikidata
parser is 3 can no longer build the parser-2 English collection
(03 §3.4: not equivalent by versions). So parser 3 reaches the branch only
when the daughter is created from the English collection's last snapshot,
in the same deploy; until then every deploy carries parser 2. Its pack
needs (in-place passes, RULE 1, as the names): the labels of the added
languages, and the P279 edges for the closure.

**Open for the operator, with those numbers:** the languages, and
whether a language's section is written for every selected entity or only
where Wikidata names it in that language. Recommended: it, de, fr, es,
only where named (2.7 months of the budget, against 15). If nothing is
said: that.
