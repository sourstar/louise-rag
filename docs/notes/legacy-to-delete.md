# Legacy artefacts left behind, and what has to be true before each goes

**The operator's rule, 2026-09-10: complete the migration, confirm it
works, and ONLY THEN delete.** This file is the list. Nothing here is
removed on a hunch, and nothing here is forgotten either — a migration
that leaves both copies forever has not finished.

Sizes measured on the node 2026-09-10. **Total recoverable once every
condition below is met: about 587 GB.**

---

## 1. `gutenberg` — SWITCHED 2026-09-10; legacy DELETED 2026-09-18

The collection runs from its Codex (`sha256:dd528265706eddb1…`);
`config/collections/gutenberg.toml` names it in a `codex` key and the
worker resolves the tree under `/data/collections/gutenberg/`.

**That sentence was wrong when this note first said it, and the way it
was wrong is worth keeping.** On 2026-09-10 the switch was made by
repointing the legacy TOML's `[paths]` at the migrated tree — so the data
was right, but the collection was still loaded down the legacy path, with
its plugin and layers taken from the FILE. No TOML on the node had ever
carried a `codex` key; the bridge in `app/collections.py` was built,
tested and unused.

The file declared its own layer:

```toml
[[layers]]
chunker = "fixed"
rule = "default"
```

with no `params`. `parse_layers` defaults params to `{}` and
`chunk_fixed` refuses that by design — "there is no default; a layer that
does not say is refused" (06 §6.3) — while the Codex says the window is
`{tokens = 1000, overlap = 120}`. So the node chunked by one answer and
named itself by another. **10,212 documents errored at the embed stage**
(`embed: the `fixed` chunker takes `tokens` and `overlap` from its
layer's params`), and because an error removes a document from every
selection, they were out of the pipeline until a human intervened. The
worker log shows the whole encoder budget going into it: `gutenberg: 100%
in 3677 turns (733s, 10212 units, 71 overruns)`.

This is exactly invariant 1's failure mode, and note that nothing
detected it: both halves were internally consistent and the
`collection_id` was computed honestly. Only the chunker's refusal to
guess made it visible at all — had `fixed` carried a default window, the
collection would have embedded 8.7M chunks under an id that promised a
different cut, and the vectors would have been wrong-but-plausible.

Fixed 2026-09-11 by rewriting the file as a Codex TOML: `codex = "…"`,
no `plugin`, no `[[layers]]`, no `[paths]`, and the encoder recipe moved
from `[embed]` to `[encoder]` because on a Codex TOML the bare `embed`
key is the MODE. The 10,212 errors were then cleared with
`db.apply_reset(con, "errors")`, which leaves the 1,454 unprefixed `no
txt file` corpus facts alone. The previous file is kept at
`config/collections/_replaced-2026-09-11/gutenberg.toml.paths-form`.

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
   the legacy manifest reached. **MET 2026-09-11**: 77,866 of 79,320, the
   remaining 1,454 being the `no txt file` entries the origin ships no
   text for.
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

## 2. `pubmed` — MIGRATED; legacy DELETED 2026-09-18

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
* **`<pubmed corpus>/.pack`, ~49 G when full** — the SAME cache, built
  beside the corpus rather than under `data/`, which is where a re-parse
  from the origin puts it. `records-vs-origin` re-parses every record of
  its sampled shards, so a run over pubmed's 0.05 of 1,358 shards touches
  ~2.01 M records spread across most of the 1,334 containers and rebuilds
  a cache for each: measured 2026-09-12 at ~20 s and ~37 MB per
  container, so ~7 hours and ~49 G for a full run. The pool has 7.3 T
  free, so the size is not the constraint — the RUNTIME is, and it is why
  a pubmed `hold check` is an afternoon rather than the 500 s gutenberg
  takes. The cache persists, so a second run is cheap; that is the point
  of it. Same condition as the row above.
* **the archive pool’s `louise-packs/*`, 17 G total** — pre-Codex packs
  (`gutenberg` 7.0 G, `gutenberg-2026-06` 7.9 G, `pubmed` 729 M,
  `wikipedia` 1.4 G). `gutenberg-2026-06` is the intermediate product the
  operator dropped, and its collection is already gone; the packs
  themselves are the only copy of what those builds produced. Cheap to
  keep on a pool with terabytes free.
* **`src.old`, 15 M** — the pre-migration source tree, kept by the deploy
  procedure until the operator is satisfied with the new one. Delete when
  they are.

* **the retired plain-text gutenberg, `data/collections/gutenberg.retired-2026-09-12`, 41 G**
  — the step-8 collection (`sha256:dd528265…`): its manifest, its fts index
  over 8.7 M chunks, and the 1,596 documents embedded on the CPU encoder.
  The operator replaced it with the plates collection on 2026-09-12 and
  asked for it to be deleted. THE CONDITION IS THE REPLACEMENT PASSING
  `hold check`, which needs the epub fetch and a records build first — the
  two collections are in different spaces (`cleaning_version` 1 against 2),
  so nothing here is reusable by the new one, but it is also the only copy
  of what step 8 produced and deleting it before the replacement is
  verified would leave the node with neither.
* **the parser-4 plates attempt, `data/collections/gutenberg.parser4-2026-09-12`**
  — `sha256:9a857d31…`, replaced within the hour when the reading rule
  moved to parser version 5. Manifest and part of an fts index, no drag
  records. Nothing names that id. Safe to delete as soon as the operator
  says so.
* **the first plates attempt, `data/collections/gutenberg.unchecked-figures-2026-09-12`, 11 G**
  — `sha256:88075d0a…`, minted and then superseded the same morning
  because its validation plan did not name the `figures` check. It holds
  no drag records, only the manifest and fts index built from the txt
  editions during the hour it ran. Nothing depends on it and nothing
  can: no collection file names that id. Safe to delete as soon as the
  operator says so.

---

## 6. Found 2026-09-15, while moving origins off the SSD pools

The operator's rule of 2026-09-15: a collection's origin files live on
the archive pool, never on an SSD pool. Looking for origins on the SSDs
turned up these.

| what | size | condition |
|---|---|---|
| ~~the SSD pool's former backup, `tank/backup/ssd4-until-2026-08-28`~~ | 1.87 T | **deleted 2026-09-15** (operator). Its replication had stopped on 2026-08-28: the task excluded datasets it had already sent, so it lost its incremental base. A dry run first: no holds, no clones, no task targeting it. The live collections now have their own replication (`tank/backup/louise-rag`, hourly) |
| ~~the SSD copies of the genome FASTAs~~ | 1.0 G | **deleted 2026-09-15** (operator): each dataset's one file checked equal by SHA-256 to its copy in the archive pool's origins dataset, no container mounting it; destroyed with their daily snapshots. The collections' data (§4) is a different thing and stays |
| ~~the old container paths of the three live origins (`/corpus`, `/corpora/pubmed`, `/corpora/wikipedia`, `/corpora/wikipedia/data`)~~ | — | **removed from both services 2026-09-15** (58a21f2's deploy), after `documents.path` and `fetch_files.local_path` were relocated to `<LOUISE_ROOT>/collections/<name>/origin` and a scan of the live state databases found no other stored path. `CORPUS_DIR` points at gutenberg's `origin/`. `/corpora/wikivec` and `/corpora/genome-*` stay: layer 1 and the boxed genomes still read them |

---

## What "confirmed working" is not

A row count is not a confirmation. `hold check` passing on the DRAG is
not a confirmation that the WORKER is running the collection correctly —
the drag was verified before the switch and did not change. The
confirmation is a person searching the collection and getting the right
answer back, plus the counts agreeing.

## Already deleted, for the record

The legacy wikivec and wikipedia trees and the three retired gutenberg
collections (2026-09-21, operator: "delete legacy data"; run by the
operator, because the classifier refuses an `rm -rf` there). Deleted:
`data/wikivec` (462 G), `data/wikipedia` (30 G), and
`collections/gutenberg.{retired,parser4,unchecked-figures}-2026-09-12`
(63 G). Checked that day:
- No process had a file open, and no live TOML or control table names them.
- The live wikipedia holds more than both legacy trees: 144.1M vectors
  against wikivec's 119.6M, all 1,447 archive shards applied against
  1,199, and 52.9M documents against 22.0M + 19.4M. A search returns the
  right articles in en/ru/it/fr.
- **§5's condition for the retired gutenberg did NOT hold.** The live
  gutenberg had no DRAG snapshot, so `hold check` could not run. It was
  deleted on the operator's word, their second request after 09-12.
- **Kept:** `config/collections-legacy/{wikivec,wikipedia}.toml`.
  `licensing._peer_acknowledged` reads acks from that directory, and the
  live wikipedia has no `state/ack.json` of its own. Move its ack before
  those files go.
- The space is held by the manual snapshot
  `ssd4/apps/louise-rag@pre-rage-b10630-6-2026-09-20`, which no retention
  removes (707 G in snapshots right after the deletion). The space comes
  back only when that snapshot is destroyed.

pubmed's legacy collection (2026-09-18, operator: "check and in case you
can delete them, delete"). Deleted `data/pubmed` (134 G: its manifest, text
index, vector table, genesis state and cursor) and the three parked config
files that pointed at it (`collections-legacy/pubmed.toml` and two under
`collections/_removed-2026-09-10/`). `data/pubmed-pack` stays: it is the
live pipeline's record cache, not the legacy collection.

Every condition in §2 held, checked that day:
- **Full text index (1).** Complete: 39,994,988 of 39,994,988 documents,
  what the legacy manifest reached. The 09-15 re-pin's backlog cleared at
  15:44 UTC.
- **Search (2).** It returns relevant papers. "aspirin platelet
  aggregation" finds PMIDs 3370916, 7379449 and 427005.
- **`hold check` (3).** roots, records (2,023,216 over 68 shards),
  chunks, license and signature pass. `records-vs-origin` is inconclusive
  without `--origin`.
- **Nobody else's (4).** No process had the files open; only the three
  parked config files named the path.
- **Vectors (5).** 35,417,887 held inside the archive's range. Calibration
  passes 16/16 (median 1.0000), and vectors-sample reproduces 1,088 chunks
  over 68 shards at 0.95. Both first needed e8e2751: the check had been
  embedding with `EMBED_URL` (the node's Qwen3-4B, 2560-wide) instead of
  pubmed's MedCPT.
- **The count gap (6).** It no longer exists. Both manifests hold
  39,994,988 documents, and 20,000 random legacy uids are all in the
  migrated one. Why the migrated scan read 35,494,988 on 09-10 was not
  re-derived. What the condition guards against, deleting the only copy of
  documents the new collection lacks, no longer applies.

gutenberg's legacy tree (2026-09-18, operator: "proceed"). Deleted:
`data/fts` (35 G), `data/manifest.db` with its WAL, the empty
`data/lancedb`, `backend_baseline-gutenberg.json`,
`gutenberg_scan_cursor.json` and `config/collections/_replaced-2026-09-10/`.

The conditions in §1, as they stood that day:
- The migrated index was complete (condition 1, met on 2026-09-11).
- A search through the app's own search code returned gutenberg results.
  Moby Dick (2701) is indexed with 433 chunks and is found for "Ahab".
  The operator gave the go-ahead (condition 2).
- `hold check` could not run: gutenberg has had no DRAG snapshot since its
  replacement on 09-12. It checks the DRAG, which these files never
  touched, so it was dropped as a condition (condition 3).
- No code reads `config.DB_PATH`, `LANCE_DIR` or `FTS_DIR` directly, and no
  process had the files open (condition 4).

The ssd4 snapshots held the space for 2 more hours.

A second Gutenberg tree on the archive pool, `louise-corpora/gutenberg`
(2026-09-15, operator: "if none of the files are missing, delete"): a
dataset of 220 GB with one manual snapshot, mounted by no service.
Compared against the live origin (the books dataset) first: of its
606,564 files none was missing there; 484,248 matched in size and the
other 122,316 existed there in a newer revision — the August mirror of
the same files. Destroyed with its snapshot.

`gutenberg-rag` (2026-09-15, operator): the old data directory on the
second SSD pool — a manifest, text index and vector table from before the
collections existed, 37.8 GB with its seven automatic snapshots — and the
stopped app that still referenced it. No container mounted it.

`gutenberg-pack` and its 48 GB (2026-09-10), before this rule was
stated. It sat outside the ferry list so the migration does not need it,
and its 48 GB was a text index over a pack that survives on
the archive pool’s `louise-packs/gutenberg-2026-06`. Recorded here because the
rule arrived afterwards and the deletion should not be invisible.

## 7. The per-model apps and the governor — STOPPED 2026-09-19; DELETED BY THE OPERATOR by 2026-09-20

**As found on 2026-09-20:** none of these apps is on the node any more —
the operator deleted them, ahead of the conditions below, which were
written for whoever would otherwise have decided alone. Their model
folders are untouched and RAGE reads them. What is left of this section's
way back is `config/backup-pre-rage-2026-09-19/` (Louise's settings). The
deleted apps' own definitions went with them: TrueNAS keeps none, and
`/mnt/ssd4/apps/louise-rag/app-config-backups/` holds only an OLDER
generation (`llama-embed`, `embed-bge-m3`, `embed-medcpt`). The servers'
code is in the repository's `deploy/`; re-creating one of those apps would
mean writing its definition again. **`ollama` is the one still standing**:
idle since chat moved, and since 2026-09-20 RAGE reads nothing of its
folder (the chat model has a copy of RAGE's own), so it and its 89 GB
store can go whenever the operator says.

The section as written on 2026-09-19:


RAGE (`louise-rage`, port 30437) replaced them in one cut-over
([`STATUS.md`](STATUS.md), "THE CUT-OVER IS DONE"). They are **stopped,
not deleted**, and they are the way back: start them, restore
`config/backup-pre-rage-2026-09-19/`, stop RAGE.

| app | what it served | its models stay, RAGE reads them |
|---|---|---|
| `embed-gpu-qwen3-4b` | gutenberg's encoder, :30437 | `/mnt/ssd5/apps/llama-embed` |
| `embed-gpu-bge-m3` | wikipedia's encoder :30439, the bge reranker :30447 | `/mnt/ssd5/apps/bge-m3-models` |
| `embed-gpu-medcpt` | MedCPT article :30443, query :30444, cross-encoder :30445 | `/mnt/ssd5/apps/medcpt-models` |
| `translate-gpu-madlad` | MADLAD, :30448 | `/mnt/ssd5/apps/madlad-models` |
| `louise-gpu-governor` | the GPU clock | — (RAGE holds the clock) |

**THE MODEL FOLDERS ARE NEVER DELETABLE ON THESE GROUNDS**: RAGE binds
them read-only. Only the app DEFINITIONS and their images go.

Conditions, all of them, before any is deleted (operator's rule: complete,
confirm, only then delete):
1. RAGE has served the node through at least one full day of ordinary
   work — embedding, archive proofs, searches, translations — with no
   model refused for memory and none restarted by the kernel.
2. The three gates still pass through RAGE after that day.
3. The operator confirms.
4. Then, and separately: `ollama`, once chat has moved
   (`CHAT_API=openai`, `CHAT_URL=…/upstream/chat` — DONE 2026-09-19 night)
   and answered for a day;
   and the CPU apps (`embed-cpu-*`), which were already stopped before the
   cut-over and which nothing references.

Not part of this: `embed-gpu-qwen3-8b` (stopped before, referenced by no
collection) and the raw container `dnabert2-xpu` — DNA is frozen
(operator, 2026-09-19), so it is neither migrated nor deleted.

## 8. RAGE's lifecycle and the old model servers — RETIRED 2026-10-05 (15g)

**Done in the repository, at the operator's word** ("go for 15g", the
day of the cut-over, before the full day this section asked for). The way
back does not depend on the repository: backout.sh uses the images and
src.old kept on the node (`rage:pre-15f`, `louise-rag:pre-15f`), which stay
until the operator removes them. What went:
- `app/rage/admission.py`, `placement.py`, `swap.py`, `swapyaml.py`, the
  llama-swap `front.py` (and with it the `park` route); `model.py` and
  `procfs.py` cut to what the LocalAI front reads; `runs.py` without the argv
  matcher; `RAGE_RUNNER=llama-swap` refuses to start.
- `deploy/rage/Dockerfile`, `deploy/rage/servers/`, `deploy/truenas-rage.yaml`;
  `rage.example.toml` is the cards alone.
- `TRANSLATE_API` and the `/translate` shape: the completion shape is the only
  one.
- The three servers' HTTP handlers, `main`s and MADLAD's parking. Their model
  code stays (the backends import it). Their per-model apps' Dockerfiles and
  `truenas-madlad-xpu-translate.yaml` went with them.
- `tools/load_profile.py` was KEPT and ported: it reads LocalAI's YAML and
  states.

The section as written on 2026-10-03:

Written 2026-10-03, when 15e-2 put the three Python models under LocalAI.
Nothing here goes before the cut-over (15f) has served the node for a full
day of ordinary work, the gates pass through it, and the operator confirms.

| what | why it stays until then |
|---|---|
| `app/rage/admission.py`, `placement.py`, `swap.py`, `swapyaml.py`, `front.py`'s `park` route | llama-swap's RAGE, the way back |
| `deploy/rage/Dockerfile`, `deploy/rage/servers/`, `deploy/truenas-rage.yaml` | the same |
| `TRANSLATE_API="translate"` and its branch in `app/translate.py` | the shape the deployed MADLAD server speaks until 15f |
| the HTTP handlers and `main` of `deploy/madlad-translate/server.py` (`/translate`, `/languages`, `/park`), `deploy/medcpt-rerank/server.py` (TEI `/rerank`) and `deploy/dar-legal-it/server.py` | served by llama-swap's RAGE. **Their model code stays**: the LocalAI backends import it |
| `rage.toml`'s lifecycle keys (`memory`, `priority`, `preload`, `park`, `cmd`) | read by the llama-swap front only |
