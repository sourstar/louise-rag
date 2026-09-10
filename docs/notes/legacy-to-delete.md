# Legacy artefacts left behind, and what has to be true before each goes

**The operator's rule, 2026-09-10: complete the migration, confirm it
works, and ONLY THEN delete.** This file is the list. Nothing here is
removed on a hunch, and nothing here is forgotten either — a migration
that leaves both copies forever has not finished.

Sizes measured on the node 2026-09-10. **Total recoverable once every
condition below is met: about 587 GB.**

---

## 1. `gutenberg` — SWITCHED 2026-09-10, not yet confirmed

The collection now runs from its Codex
(`sha256:dd528265706eddb1…`); `config/collections/gutenberg.toml` names
it and the worker resolves the tree under
`/data/collections/gutenberg/`.

| what | size | path |
|---|---|---|
| legacy manifest | 56 M | `data/manifest.db` |
| legacy text index | **35 G** | `data/fts` |
| legacy vector table | 512 B | `data/lancedb` (empty — the 4B move emptied it) |
| legacy backend baseline | 65 K | `data/backend_baseline-gutenberg.json` |
| legacy scan cursor | 1.7 M | `data/gutenberg_scan_cursor.json` |
| the replaced TOML | 5 K | `config/collections/_replaced-2026-09-10/` |

**Delete only when ALL of these hold:**

1. `/data/collections/gutenberg/index` holds the full text index —
   `SUM(fts_done)` in the migrated manifest reaches 77,866, matching what
   the legacy manifest reached. **In progress**: the worker is
   re-indexing now, ~178 documents / 20,018 chunks per 72 s commit.
2. A search over gutenberg in the Library returns results, run by a
   person, not inferred from a row count.
3. `hold check gutenberg --root /data/collections` still passes — it did
   on 2026-09-10 before the switch, and the switch changed which manifest
   the worker writes, not the drag.
4. `data/lancedb` is confirmed to be nobody else's. **CAUTION**:
   `config.DB_PATH` defaults to `DATA_DIR/manifest.db` and `LANCE_DIR` to
   `DATA_DIR/lancedb`, so a code path that takes the default rather than a
   Collection would land in exactly these files. Grep before deleting;
   an empty directory that something still opens is a different problem
   from a stale one.

**Do NOT delete** `config/collections/_replaced-2026-09-10/gutenberg.toml.legacy`
until 1–3 hold: it is the one-`cp` way back.

---

## 2. `pubmed` — MIGRATING, nothing to delete yet

`collection_id sha256:02da1b554313670c…`, acknowledged, records build
running. Its legacy side stays untouched until it has been through the
same gate gutenberg is in now, plus its own:

| what | size | path |
|---|---|---|
| legacy collection | **164 G** | `data/pubmed` |

**Extra conditions beyond gutenberg's:**

5. `hold vectors import --archive medcpt-pubmed` has landed the archive's
   35.3M vectors into the migrated collection and `hold check` reports
   `vectors-sample` as a PASS rather than inconclusive.
6. The document-count difference is explained. The legacy manifest holds
   **39,994,988** and the migrated scan found **35,494,988** — a gap of
   4,500,000. The working hypothesis is `DeleteCitation` tombstones, which
   the legacy manifest keeps as errored rows and a fresh scan does not
   add. **This is a hypothesis and not a finding**; deleting 164 GB on an
   unexplained 4.5M-row difference would be exactly the wrong order.

---

## 3. `wikipedia` and `wikivec` — BLOCKED, delete nothing

| what | size | path |
|---|---|---|
| `wikivec` | **345 G** | `data/wikivec` |
| `wikipedia` | 30 G | `data/wikipedia` |

Step 8 planned to fold `wikivec` into `wikipedia` as layer 1. Measured
2026-09-10, it cannot — see
[`wikivec-merge-blocked.md`](wikivec-merge-blocked.md): different cuts
(63 paragraphs vs 24 sections on article 12), a uid carries its plugin's
name so the archive's `embedded:*` rows can never be a wikipedia-plugin
collection's, and the two corpora are different Wikipedia snapshots.

**Until the operator decides, `wikivec` is a LIVE collection holding 14.2M
archive vectors, not a leftover.** It is also still importing from the
archive, cleanly, at ~222 documents/s. Nothing here is deletable under
any reading.

---

## 4. genome × 3 — STAYING legacy, never deletable on these grounds

| what | size | path |
|---|---|---|
| `genome-hg38` | 2.2 G | `data/genome-hg38` |
| `genome-dm6` | 189 M | `data/genome-dm6` |
| `genome-sacCer3` | 19 M | `data/genome-sacCer3` |

No DNA calibration set exists, so `create` cannot write them a Codex at
all (migration plan, step 8). They are not "left behind" — they are the
current and only form of those collections. **Delete nothing.**

---

## 5. Things that look like leftovers and are not

* **`data/pubmed-pack`, 61 G** — the pubmed plugin's derived per-container
  Parquet cache (`PUBMED_PACK_DIR`), NOT a collection. It is what made
  pubmed's `records build` cheap: rebuilding costs ~17 s × ~1,200
  containers. Deleting it is legitimate housekeeping AFTER pubmed is
  migrated and no further build is planned, and pointless before.
* **`/mnt/tank/louise-packs/*`, 17 G total** — pre-Codex packs
  (`gutenberg` 7.0 G, `gutenberg-2026-06` 7.9 G, `pubmed` 729 M,
  `wikipedia` 1.4 G). `gutenberg-2026-06` is the intermediate product the
  operator dropped, and its collection is already gone; the packs
  themselves are the only copy of what those builds produced. Cheap to
  keep on a pool with terabytes free.
* **`src.old`, 15 M** — the pre-migration source tree, kept by the deploy
  procedure until the operator is satisfied with the new one. Delete when
  they are.

---

## What "confirmed working" is not

A row count is not a confirmation. `hold check` passing on the DRAG is
not a confirmation that the WORKER is running the collection correctly —
the drag was verified before the switch and did not change. The
confirmation is a person searching the collection and getting the right
answer back, plus the counts agreeing.

## Already deleted, for the record

`gutenberg-pack` and its 48 GB (2026-09-10), before this rule was
stated. It sat outside the ferry list so the migration does not need it,
and its 48 GB was a text index over a pack that survives on
`/mnt/tank/louise-packs/gutenberg-2026-06`. Recorded here because the
rule arrived afterwards and the deletion should not be invisible.
