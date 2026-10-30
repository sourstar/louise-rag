# Multi-core audit — where the 2026-10-30 lessons apply (2026-10-30)

On 2026-10-30, `hold records build` went from 1 core to about 6 of 8 (#65–#71), and `hold check` from 28.7 min to about 7 min for six checks (#76). Those fixes exposed six defects:

| # | the defect | the fix that paid |
|---|---|---|
| D1 | per-item CPU work done in ONE process | the parse pool / a spawned process pool |
| D2 | the parent as the bottleneck: a DB lookup per row, fat results pickled back, re-encoded | children look up their own inputs read-only and return slim results |
| D3 | writer work in threads, sharing the parent's GIL | writing in processes |
| D4 | per-part files read one after another | threads for I/O |
| D5 | a CLI pass sized quota − 1 | the whole quota (`config.build_parse_processes`) |
| D6 | each batch is a barrier: children idle while the parent writes, and head-of-line waits | results taken as they complete (`compute_pages`) |

This audit covers every stage, maintainer and pass that walks a whole corpus or collection. The code lines are cited at main 8f3cf19. The durations are the measured ones (cited); anything marked *est.* is an extrapolation.

## Ranked by wall-clock impact

### Worker stages (continuous; scheduling change → docs/notes/scheduling.md §6 checklist)

1. **rdf / fts** — `app/worker.py` 235–307 and 425–538; `app/parallel.py` 649–723. **D1 D2 D6.**
   - What runs in the parent:
     - inside `BEGIN IMMEDIATE`, `entities.tag_document` for every document (worker.py 505–516): a regex over the title and every chunk, then a Python loop over the tokens;
     - one tantivy `Document` per chunk, built in Python (fts.py 236–246);
     - whole chunk lists coming back from the children;
     - one `SELECT rowid … WHERE uid=?` per row (worker.py 184).
   - `compute` waits on its oldest future (parallel.py 704–705) and each batch is a barrier.
   - Measured:
     - 3.1–3.7 of 11 children busy (STATUS.md 447);
     - wikipedia rdf 54.7, fts 77.1 and import 77.7 docs/s, taking turns on one thread: layer 2 needs about 110 h (STATUS.md 5503–5541).
   - Bounds: tantivy allows one writer per directory, the manifest has one SQLite writer, and invariant 2 fixes the order (index → commit → marks).
   - The tagging is a pure function of title, text and resolver, so it can move to the child; the writes stay in the parent.

2. **Archive import (genesis)** — `app/bundles/genesis.py` 1469–1511. **D2 D6.**
   - One manifest SELECT per vector row (1479–1482), plus a probe for each absent row.
   - Arrow → list → numpy conversion per vector (1007–1015).
   - Reading, chunking and writing never overlap.
   - Measured: a 150 s wikivec pass = 6.3 s reading, 51.7 s chunking, about 91 s writing (scheduling.md 163–170).
   - Bounds: `vector_write_lock` (Lance has one writer).

3. **Embedding** — `app/worker.py` 1455–1690. **D1 D2.**
   - `_load_chunks` re-parses and re-chunks in the embed threads, under the GIL (1464).
   - Each document opens a new SQLite connection (1465) and runs a `columns_for` query and a possession lookup.
   - Measured:
     - pubmed 31 docs/s while the encoder answers in 5–36 ms (STATUS.md 5716–5723): the CPU side binds;
     - gutenberg's encoder 94 % busy (scheduling.md 1175): the GPU binds.
   - Bounds:
     - invariant 12 and `-np 1`: one text per request stays;
     - only the preparation can move.

4. **Bundle inbox** — `app/bundles/importer.py` 429–524. **D1 D2.**
   - Each document is re-cut in the thread.
   - One manifest lookup per document.
   - A **Lance delete+add per document** (492–515): pitfall 13 again (scheduling.md 294–304).

5. **Index maintainer: facets** — `app/ask/facets.py` 263–320. **D1.**
   - One to three pure-Python passes over the manifest per field.
   - Measured: 2,352 s on a busy node, 16.5 min paused (STATUS.md 349–351, 453).
   - One maintainer thread serves every collection, so this pass delays every other collection's vector-index refresh.
   - The fields are independent reads.

6. **Discovery** (cold scans only) — `app/discovery.py` 250–353. **D2:** one SELECT per ref (286–288); files are hashed one at a time (307). Bounds: arrival order is the last-write-wins rule.

7. **Wikidata pack, publisher** — the pack pool is sized quota − 1 (D5) and writes through one thread; contribute bundles parts one after another (D1).

### Operator passes (one-shot; no scheduling change)

8. **FIXED (#77): The next snapshot's delta** — `app/hold/snapshot.py` 781–814 (`_later_delta` → `_pairs`). **D1.**
   - Every re-issued part is decoded whole, old and new versions, in one thread.
   - *Est.* 1.5–2 h for wikipedia-v8's next snapshot; one side measured 43–57 min (snapshot.py 995).
   - The parts are independent; only the Arrow join is a single step.

9. **FIXED (#77): `hold check` vectors-sample** — `app/hold/vectors_check.py` 152–182. **D1.**
   - Measured: 17 min on 2026-10-28, ONE python process at 138 % CPU (München, 2026-10-30).
   - Every sampled part is decoded and re-cut in the parent (`land.records_for`, `land.cut`) only to draw 16 chunk ids. These are the parts the pooled chunks check has just cut.
   - On 10-28 only 8,805 chunks were checked, so most sampled parts paid that work for nothing (*est.*).
   - The encoder side, 16 one-text requests per part, is fixed by invariant 12; it can overlap with the CPU side.

10. **Rare tools** — **D1 D2**:
    - `dev index-drift` (drift.py 285–302): *est.* 50–57 min at 58–65 µs per record;
    - `carry.reconcile` (carry.py 635–676): two 52.9 M-entry dicts, which is also a memory risk;
    - `possession.record_hashes` (possession.py 371): only for a collection with predecessors;
    - `hold vectors build`'s `pending` (vectors_build.py 135): reads every vector only to test which ids exist;
    - `dev mark-embedded` (louise.py 1215);
    - `share attest`: reached `run_check` without widening the pool to the whole quota (D5). **FIXED (#77):** attest, `snapshot create` and `hold vectors check` now take the whole quota, through one helper (`louise._whole_quota`).

11. **The records build's plan.** `app/pack/build.py` 156–163 walks the manifest serially, one core.
    - Measured on wikidata (München's py-spy, 2026-10-30):
      - with the per-document `pack_dir` realpath: 350,000 documents per 30 s, i.e. 1 h 48 min for 75.9 M. **FIXED in #79**;
      - after #79: 70–74 k documents/s, 17–19 min.
    - Two of three samples are in SQLite's C code without the GIL, so the walk could be split into rowid windows on threads.
    - Not done mid-build: a fix could not beat the remaining 15 minutes. Next.

### Not defects

- **Relocate** is already pooled with slim results.
- **roots, records, chunks** are pooled (#76).
- **Lance vector-index builds** are native and use every core; building one collection at a time is deliberate (scheduling.md 861–867).
- **Pieces-root hashing** (seeder, accept, torrent_landed) is bound by tank's sequential read; parallel reads on an HDD may slow it. Measure before touching it.
- Validation, forget, integrity, certify, seals, coverage, leading, holdings and land walk no whole collection.

## The order of work

These are defects (CLAUDE.md: "Multi-threading is a must"), so they are fixed, not offered. The order: the operator passes first (8, 9), because they are self-contained, testable here and need no scheduling measurement. Then the worker stages (1, 4, 3, 2, 5), one change per deploy, each with BEFORE and AFTER numbers in `scheduling-log.md`.

## Done

- **#76:** roots, records and chunks are checked in processes. Measured: the six checks took ~7 min, against 28.7 min on 2026-10-28.
- **#77:** what changed:
  - `parallel.each_part`: a lazy process pool for an operator's pass over parts;
  - the later delta decodes both versions of every re-issued part in processes;
  - vectors-sample cuts and draws each part in processes while the parent embeds (the encoder side stays serial: invariant 12);
  - every operator pass takes the whole quota.

  Measured: not yet. The next `hold check` and the next snapshot of wikipedia-v8 will show it.
