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

## 7. The space

The operator's default (arxiv-plugin.md D1): **Qwen3-Embedding-4B**, the
engine gutenberg runs, measured bit-exact; dim 2560, f16, last-token
pooling, normalised; its own calibration set, so `arxiv` is its own space
(invariant 1) even with gutenberg's weights. The query side gets a
scientific-retrieval instruction ("Given a question, retrieve arXiv paper
abstracts that answer it"), applied once (`embeddings._asked`).

| size | |
|---|---|
| records | 3.2 M, ~4 GB of text |
| vectors | 3.2 M × 2560 × 2 B ≈ 16.4 GB, plus the index |
| first embedding | ~1 B tokens ≈ 185 GPU-hours at ~1,500 tok/s: about four days on the node and Perugia together, shared by slice |
| monthly | ~25 K papers, about an hour |

## 8. Build order

1. The plugin (`app/plugins/arxiv/`): OAI client (resumption, from-date,
   both formats, transfer-manager budget), parser, `current()`, the
   language detector pinned in the image. Tests: a recorded OAI page of
   each format → records; every arXivRaw and arXiv field reaching the
   record (a coverage table, as tests/test_gutenberg_promise9.py); a
   resumed harvest; a withdrawn paper; an old-style id.
2. `reader.ORIGIN["arxiv"]` and the hit's two links; the facets.
3. The recipe (`docs/notes/arxiv-recipe.toml.txt`), `hold collection
   create`, a 1,000-paper trial on the node: harvest, build, embed, ask.
4. The full harvest in the operator's windows, then embedding by slice,
   then `share`.
