# The `arxiv` collection — every paper, searched by its abstract (2026-10-22)

Operator, 2026-10-22: "Design the collection that only searches by
abstracts, pointing to the remotely hosted version." The plugin's context
— bulk forms, licences, pre-embeddings, the full-text options left for
later — is docs/notes/arxiv-plugin.md. Nothing here is built yet.

## 1. What it is

One record per arXiv paper, all ~3.2 M of them (the bucket's count,
2026-10-22: 3,210,040 papers). Searched on its title and abstract; every
hit opens the paper AT ARXIV (abstract page, PDF), never a copy here. The
metadata is CC0 for every paper whatever the paper's own licence, so the
collection is **green and public**: it is shared, embedded peer to peer
and taken whole like gutenberg. RULE 2: the Library shows "arXiv"; a user
names it and takes it.

## 2. The origin, and why this form (invariant 11)

arXiv's OAI-PMH interface, `https://oaipmh.arxiv.org/oai` (arXiv's bulk
metadata page names it the preferred method; updated daily). It serves
each paper in three formats, and two of them each carry something the
other does not:

| format | carries | lacks |
|---|---|---|
| `arXivRaw` | every version with its date and size, submitter, the licence URL, comments, journal-ref, DOI, report numbers, MSC and ACM classes, proxy | authors parsed |
| `arXiv` | authors parsed (keyname, forenames, suffix) with their **affiliations** | the version history |
| `oai_dc` | nothing the two above lack | — |

**The unit is the paper in BOTH formats**, as gutenberg's unit is the book
in its form plus its catalogue entry (parser 9): `origin_hash` covers the
two OAI records' bytes, so either changing rebuilds the record. Not the
Kaggle JSON: a derived weekly file that cannot be re-fetched per paper,
which `records-vs-origin` and freshness need. Not the 2020 copy in the
Google bucket: stale.

Recorded beside the plugin, as the invariant requires: "OAI-PMH, arXivRaw +
arXiv, per paper; chosen because it is arXiv's own per-record form and
together they hold everything arXiv states about a paper."

## 3. The record (09 §9.5)

| field | from |
|---|---|
| `uid` | `arxiv:<id>`, the id as arXiv writes it (`2310.01234`, `hep-th/9901001`) |
| `title` | arXivRaw title, whitespace normalised (LaTeX kept as written) |
| `creators` | the `arXiv` format's parsed authors, in order (`Keyname, Forenames Suffix`) |
| `date` | the first version's date (v1); every version's is in `extra.versions` |
| `lang` | detected by a pinned detector over title + abstract, `extra.lang_source = "detected"` — arXiv states no language; most papers are English, some are not |
| `subjects` | the categories, primary first |
| `sections` | `[["Abstract", <abstract as written, LaTeX math kept>]]` |
| `extra` | `versions` [{v, date, size}], `submitter`, `affiliations` per author, `licence`, `comments`, `journal_ref`, `doi`, `report_no`, `msc_class`, `acm_class`, `proxy`, `withdrawn` |
| `origin` | the OAI identifier, `oai:arXiv.org:<id>` |

Nothing arXiv states about the paper is dropped: what has no §9.5 home is
under `extra.<name>`, and `extra`'s keys are fixed for the collection's
life, so they are all named now.

## 4. Search and the link out

- **One layer, one chunk per paper**: the title, a blank line, the
  abstract. arXiv caps an abstract at 1,920 characters, so every record is
  one window; no chunk ever splits.
- **The hit** shows the title, authors, primary category, year and the
  abstract (translated like any entry, docs/notes on the Ask page). Its
  links: **"read at arXiv ↗"** `https://arxiv.org/abs/<id>` (versionless:
  the latest), and **"PDF ↗"** `https://arxiv.org/pdf/<id>`. `reader.ORIGIN`
  gets the `arxiv` entry; a reader opening the hit sees the abstract and
  the full metadata, and the paper itself one click away at arXiv. This is
  also what arXiv asks of tools built on its data: link back.
- **Every version, linked; only the latest embedded** (operator,
  2026-10-22: "point to the previous versions of the same article, but
  keep only the embedding of the latest one"). The record is the paper,
  ONE per id, and its one chunk is the latest title and abstract — the
  only text the OAI interface serves, so the only one held. The hit lists
  every version from `extra.versions` with its date and size, each linked
  to `https://arxiv.org/abs/<id>v<n>` (and its PDF), the latest marked
  current. A new version replaces the chunk (a new `content_hash` only if
  the title or abstract moved; the version list always grows); no vector
  of an older version is kept, so a search never returns two versions of
  one paper.
- **Facets** (05 §5.7): primary category, category group (cs, math,
  physics, q-bio, q-fin, stat, eess, econ), year, licence.
- **No full text, no figures** (record_line 1). A later full-text
  collection (arxiv-plugin.md) can be annotated against this one by id.

## 5. Slicing

By the primary category's GROUP: `cs`, `math`, `physics` (astro-ph,
cond-mat, gr-qc, hep-*, math-ph, nlin, nucl-*, physics, quant-ph), `q-bio`,
`q-fin`, `stat`, `eess`, `econ`. A peer can take only the groups it wants
(`share fetch --slice`), and the parts within a slice stay bounded by the
usual part size. A paper's primary category does not change, so its slice
never moves.

## 6. Acquisition and updates

- **First harvest**: `ListRecords` for each format, ~1,000 records a page,
  ~3,200 pages each; one request at a time, at the pace arXiv's API terms
  set (one request every 3 seconds), every byte through the transfer
  manager (invariant 5). About six hours for both formats. Resumable at
  the resumption token after every page (invariant 2: a page is the unit).
- **Daily**: `ListRecords from=<last datestamp>` for both formats. A new
  version changes the record (a new `content_hash` only if the title or
  abstract moved; `extra.versions` always). ~25 K papers a month.
- **Withdrawn papers**: arXiv keeps the record and marks the withdrawal;
  `erasure = retractable`, the tombstone stays (04 §4.4), the hit says
  "withdrawn".
- **OAI datestamps** do not equal submission dates for old papers (arXiv
  re-stamped records in bulk): only the harvest cursor uses them; the
  record's dates come from the version history.
- `current()` = `GetRecord` for both formats, so freshness (05 §5.3) and
  `records-vs-origin` re-fetch one paper exactly.
- **Attribution**: arXiv's API terms ask for a credit line; it becomes the
  plugin's `corpus_attribution` byte for byte (the exact wording copied
  from the terms when the plugin is built, not from memory).

## 7. The space — ON HOLD (operator, 2026-10-22)

The encoder is not chosen. The collection is created, harvested, built and
shared as records first; `create` writes the space once the choice is
made, so nothing below waits on it except the embedding itself.

The candidates (arxiv-plugin.md D1, MTEB from the Qwen3 model card):
Qwen3-Embedding-4B (gutenberg's; multilingual retrieval 69.6, ~185
GPU-hours, ~16.4 GB of vectors) and bge-m3 (wikipedia's; 54.6, an
estimated 4–7× faster, ~6.5 GB). **The choice is settled by a measurement
kept for when it is wanted** (operator: "keep your params 20000 abstracts
and 500 questions"):

- 20,000 abstracts from the harvest, the 500 answers among them and the
  rest drawn from the same categories, so the wrong candidates are close;
- 500 questions, one per answer abstract, written by the node's chat
  model from that abstract alone (the prompt kept with the result);
- both encoders through Louise's own client on the deployed runs, the
  query instruction of each model's own card;
- MRR@10 (the primary figure), Hit@1/10 and NDCG@10 per model, with a
  bootstrap interval (±~1.5 points at 500 questions);
- cost: ~6 M tokens per model to embed (~70 min Qwen3-4B, ~15 min
  bge-m3), ~45 min of question writing.

| size | |
|---|---|
| records | 3.2 M, ~4 GB of text |
| first embedding | ~1 B tokens — by the encoder chosen |
| monthly | ~25 K papers |

## 8. The plugin, in code

### 8.1 The acquisition: an OAI-PMH transport (06 §6.2: "an origin that
is an API is still a spec")

`app/acquire/transport_oai.py`, a new `Transport.OAI_PMH`, run by the
platform like https or rsync, every byte through the transfer manager:

- **listing** = `ListRecords` pages, `metadataPrefix` × {`arXivRaw`,
  `arXiv`}; each page is written verbatim to
  `<corpus>/oai/<prefix>/<harvest>/page-<NNNNN>.xml` and is the "file" of
  the spec. The denominator is the resumption token's `completeListSize`,
  recorded per pass (the spec's one contract);
- **resumable**: the token after every page is checkpointed beside the
  pages (invariant 2: a page is the unit); an expired token restarts the
  pass from the last page's last datestamp, never from zero;
- **etiquette** (local, outside the Codex): one request at a time, 3 s
  apart (arXiv's API terms), `Retry-After` on 503 honoured, a
  contact-bearing User-Agent;
- **updates**: a pass `from=<previous pass's last datestamp>`, daily.

`origin.spec` (inside the Codex): transport `oai-pmh`, the base URL, the
two prefixes, `set` none, update form `from-datestamp`. `origin.clock`
`UTC`; `cutoff_rule` "the latest state of every record the pass harvested
up to the cutoff instant" (an OAI record exposes only its latest
version: a peer fetching later may read a newer abstract, which freshness
labels `changed at origin`, as it does a revised Gutenberg text);
`unit` "the paper's two OAI records, `GetRecord` per prefix"; `form`
"OAI-PMH arXivRaw + arXiv: arXiv's own per-record form; together they
hold everything arXiv states".

### 8.2 The plugin: `app/plugins/arxiv/`

| module | does |
|---|---|
| `source.py` | the `CorpusSource`: classes (green, public, mutable, retractable, text, `native_id_order = "bytes"`), corpus provider and attribution, default layer, slicing and facets; `discover`, `fetch`, `parse`, `current` |
| `oai.py` | reading one page: records, `deleted` headers, the token; the arXivRaw and arXiv field readers; pure functions over bytes |
| `pairing.py` | the two formats joined by identifier: a small SQLite index in `state_dir` (`id → page, offset` per prefix), rebuilt incrementally per new page; a paper whose second format has not arrived yet is DEFERRED, not an error (engineering rule: deferral is not failure) |
| `ids.py` | id grammar (new `YYMM.NNNNN`, old `archive[.SC]/YYMMNNN`), the slice of a primary category, the category group table |
| `lang.py` | the pinned language detector over title + abstract (its version inside the parser version) |
| `TERMS.txt` | arXiv's metadata licence statement and the API terms' attribution, byte-exact |

- `discover()` walks the pairing index in id order and yields one `DocRef`
  per paper with both formats (`locator` = the two pages and offsets,
  `extra.datestamp`); a deleted header yields a tombstone ref.
- `fetch()` reads the two record elements' bytes from their pages;
  `raw_hash = H(arXivRaw record ‖ arXiv record)`.
- `parse()` maps §3's table, nothing dropped; a withdrawn paper (arXivRaw
  comments "This paper has been withdrawn", or a v-history whose last
  size is 0) keeps its record with `extra.withdrawn`.
- `current()` = two `GetRecord` calls into `workdir`, read by the same
  `parse`.
- `reader.ORIGIN["arxiv"]` and a second link (`pdf`) in the hit template;
  facets: `extra.primary_category`, category group, year, `extra.licence`.

### 8.3 Tests (they ship with the code)

Fixtures are REAL pages, captured on the node (this container cannot
reach arXiv): one `ListRecords` page per prefix trimmed to ~10 records
chosen to cover a new-style id, an old-style id, a multi-version paper, a
withdrawn paper, a paper with affiliations, one with MSC/ACM classes and a
DOI, a non-English abstract, and a `deleted` header; plus a page carrying
a resumption token and one ending a list.

- **coverage table** (as tests/test_gutenberg_promise9.py): every field
  of both formats reaches the record or is named as dropped with its
  reason — none are;
- the pairing defers a paper with one format and pairs it when the other
  arrives; `raw_hash` moves when either record moves;
- the transport: a harvest stopped after page k resumes at page k+1; an
  expired token resumes from the last datestamp; `completeListSize`
  recorded; nothing moves without the budget (a stub budget asserts);
- ids, slices and category groups over the full category list;
- `current()` against two recorded `GetRecord` answers reproduces the
  record built from the pages.

### 8.4 Build order

1. Fixtures from the node (München), then `oai.py`, `ids.py`, `pairing.py`,
   `source.py`, `lang.py` with their tests.
2. `transport_oai.py` with its tests.
3. The links and facets on the Ask page.
4. On the node: `hold collection create` WITHOUT a space is not possible
   (the space is in the Codex) — so the trial harvests and builds records
   into a scratch collection whose Codex is thrown away; the real
   `create` follows the encoder choice. The harvest itself is kept: the
   corpus directory is the origin's bytes and is reused (RULE 1).
5. The measurement of §7, then the real collection.
