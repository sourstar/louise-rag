# One backend for every collection, by template (2026-10-30)

*Operator, 2026-10-30: "All collections must share the same kind of
backend. Depending on the peculiarity of each collection the backend may
use a different template, but any collection must fall in a 'template'.
Depending on the future needs we can add cases to the backend."*

The **backend** is how a collection's origin is held, addressed and read:
- the origin's form on disk;
- what is derived from it, and how that is built, placed, kept fresh and protected from a second writer;
- how one record's bytes are read, and what a reader caches.

This is not LocalAI's model backends ([backend-consolidation.md](backend-consolidation.md)).

The August pack design had already named the pubmed pack "the prototype…
generalised to every plugin" ([pack-format.md](pack-format.md) §1). That
never happened: each plugin grew its own. On 2026-10-30 the same defects
had to be fixed three times over, in pubmed, wikipedia and wikidata:
- a thread lock where processes race;
- a cache capped in items whose size the data decides.

## 1. The templates

`app/backend.py` declares them. Each plugin declares, for every origin form
it reads, the template that form falls in (its `backend` attribute). A
template is a contract, and its machinery has one implementation that
every plugin in it uses.

| template | when | contract | the one implementation | forms |
|---|---|---|---|---|
| **MEMBER** | the origin addresses each record itself | read where it lies; nothing derived but small indexes; a reader's cache is budgeted in bytes | the shared group cache (§3 S2); the handle cache (S4) | gutenberg (a book directory), deposit (a file), normattiva (a zip member), wikipedia (a bz2 stream of the multistream dump; a row range of the archive's shards), embedded (a row range) |
| **CONTAINER_PACK** | records are members of a container that cannot be read at a record (gzip XML, SQL dump) | one Parquet pack per container, built in one pass; one builder across processes (`derived.building`); a writer's own temp file, then an atomic replace; fresh by its Parquet metadata (version, the container's size and mtime); placed by `derived.place`; a record read from its row group | `derived.building` (#94); the pack class (S3) | pubmed (the XML containers), wikipedia (page_props) |
| **DUMP_PACK** | one origin too large for one pass | a pack directory of chunk files and side tables; resumable passes with `meta.json` as the checkpoint; ONE writer (`derived.writing`, never waited for: a second one defers); a record read from a row group of its chunk file | `derived.writing` (this change) | wikidata (the JSON dump) |

Every form keeps derived data in one directory name beside its origin,
`derived.BESIDE` (`.pack`), or under the fast drive (`derived.place`). An
origin walk never enters it (`walk.files`).

`tests/test_backend_templates.py` refuses:
- a plugin that declares no form;
- a form outside the templates;
- a declaration its code contradicts:
  - the derived kinds it places, against the kinds it declares;
  - a CONTAINER_PACK that does not build under `derived.building`;
  - a DUMP_PACK that does not write under `derived.writing`;
  - a MEMBER form that derives anything.

## 2. Adding a case

A collection whose origin fits no template gets a new one: a member of
`Template`, its contract in `app/backend.py` and in the table above, and
its machinery written once. A plugin's private variant of an existing
template is not a new case. Candidates already in view:
- a live API origin read page by page (arXiv, parked);
- a mirror tree of HTML pages (the raw web pages the operator chose to keep, 2026-10-25).

## 3. The order of work

**S1, DONE (this change): every collection declared; the defects the
inventory found, fixed.**
- `derived.adopt` had a fixed temporary name and no lock. pubmed calls it from every parse-pool child, so two copies could rename a half-written pack into place. It now runs under `derived.building` with a temp name per process; a killed adopter's copy is removed.
- The wikidata pack had no inter-process writer. Its build is reached from the worker's scan, the API, the web, `hold records` and `carry`. Its codes pass rewrote `meta.json` from every child that needed it. The build, the codes pass and the regroup now run under one `derived.writing`. A second writer defers: discovery reports "deferred…; resumes next scan", and the records build reports `ContextUnavailable`.
- `covered_articles` wrote its `.npy` in place. It is now written to a temp file and replaced, and a truncated file is read again.
- `walk.files` entered `.pack`, so a page-props pack beside its dump read as an archive shard.

**S2, DONE (the cache): one group cache, budgeted in bytes.**
- `backend.GroupCache` replaces five copies of the same LRU, four of them counted in items. The new budgets:

  | reader | before | now |
  |---|---|---|
  | pubmed pack | 4 groups | 64 MiB |
  | page_props | 4 groups | 64 MiB |
  | archive | 8 groups | 256 MiB |
  | `pack/records` | 16 frames | 64 MiB |
  | wikidata | its own byte cap | 256 MiB in the shared cache |

- A test refuses any `_MAX_GROUPS` or `_MAX_FRAMES` in `app/`.
- **Left: the per-file layouts** (wikidata 8, archive 64, `pack/records` 64 indexes). wikidata's 8 re-opens a ParquetFile per line on a sparse family: 3.6 % of a build child (München, 2026-10-30). A chunk file's footer is ~3 MB in memory, so the fix is not a bigger count. It is the footer's own `metadata=` reuse under a byte budget, measured first. While the build is parent-bound (47 % of the children idle, #95), it does not bind.

**S3 — the CONTAINER_PACK class.** pubmed's and page_props' `ensure`,
`build`, `is_fresh` and `_signature` become one implementation. The plugin
gives only its rows and its schema.

**S4 — MEMBER readers and the per-record costs.**
- One handle cache for normattiva's zips, pubmed's md5 sidecars and deposit's Codex.
- gutenberg runs `pick_form` twice per record and opens the epub three more times.
- pubmed resolves its pack's place (a realpath) per record.
- wikidata pays 6–9 stats per line.

**S5 — placement.**
- `covered_articles` moves under `derived.place`.
- `PUBMED_PACK_DIR` is keyed by the containers' identity.
- page_props stops falling back to the temporary directory.

Each step is one PR with its tests; the performance ones (S2, S4) carry
München's BEFORE and AFTER numbers.
