# The version-5/6 migration of the live collections

*2026-10-09. Operator: "Do them all" (input-forms.md's migration). A
read-only study of HEAD (1df5314) found the route and the gaps below; the
gaps are closed first, then gutenberg goes as the pilot, then wikipedia,
then pubmed.*

## What a migration is

A version-5/6 Codex is always a **new collection**: every layer gains
`input`, `layers` is inside `space_id`, and the live Codexes are version 2
(whose `space_id` also holds the engine image). Under VeGa the engine is a
backend engine, so the new Codexes are written at **version 6**.

RULE 1 route: `hold collection create` a root whose recipe restates every
record-deciding field of the old Codex; carry the old collection's
byte-identical RECORD PARTS (hard links on tank) and its VECTORS and local
state; prove equivalence with HEAD's own checks; switch the collection file.
The old collection is not superseded (nothing faults it): it stops being
held here, and its SHARD stays on tank as the record (it shares inodes with
the new one). **Never `forget` an old collection.**

## Amendment A1 (RULE 1, carry)

02 §2.4/§2.7, 03 §3.5: *a vector table crosses into a collection with the
same records whose space differs only by engine, or by a declared input form
equal to the one its vectors were produced in, once `vectors-sample` under a
validated run passes at the target's tolerance before use.* Evidence already
measured: VeGa vs RAGE per model (vega.md §12: qwen3 ≥ 0.9999991, bge-m3 ≥
0.9999971); wikipedia's archive layer 0.99994–0.99999; NCBI's pair vectors
vs pair encoding on the OpenVINO GPU 1.00000. PubMed's ~4.58 M vectors made
locally in TEXT form do not qualify and are dropped and re-embedded in pair
form.

## Gaps, each closed by the smallest change

- **G1 carry** — `louise dev carry <from> <to>`: refuses unless the
  record-deciding fields, layer chunkers/params/rules, dim and dtype agree
  and the target's parts equal the source's; moves (or reflinks) manifest,
  index and lance; re-pins the space row.
- **G2 drop by provenance** — `carry --keep producer:<archive>`: keeps only
  rows an archive produced (imported/verified), deletes the rest through
  `embeddings.delete_chunks` and resets their `embed_done`, in one unit.
- **G3 seed a SHARD from identical parts** — `hold snapshot create <to>
  --shards-from <from>[:<snapshot>]`: takes the part entries from the source
  snapshot after checking each linked file's pieces root, links the parts,
  writes the target's staging claim and build.db.
- **G4 `derive`** — wire the encoder, key, clock, pairs and collection-file
  writing into `cmd_derive` (a sister in a new space always failed).
- **G5 create checks its engine** — `create` compares the run that embedded
  the reference (`stated_run`) with the recipe's engine and weights and
  refuses a mismatch; `hold embedder run` prints an endpoint's stated run.
- **G6 archive** — `tools/archive_backend.py <dir> <dest>`: `<tree hex>.tar.zst`,
  re-verified by `tree_digest` on unpack; also the OpenVINO IR.
- **G7 a second pair endpoint** — the same IR on OpenVINO's CPU device (f32)
  as `calibrate --pairs --against`, so pubmed's tolerance is MEASURED (it
  must sit well above 0.98926, the flattened median: at 0.95 a gate in pairs
  would pass a backend that flattens).
- **G8** (precondition) — each plugin's `PARSER_VERSION` must equal its old
  Codex's parser, or the records cannot be reused under the running image.
- **G9** — the new file names the old collection's origin (`corpus_dir`,
  deprecated): allowed, or an origin mount per new name.

## Per collection (outline; the commands live in the runbook script)

1. Facts: `hold collection show`, the Codex, `hold config show`; G8.
2. Runs from VeGa (G5's `hold embedder run`), archive (G6).
3. Recipe from the old Codex (record fields verbatim, `layers[].input`,
   space dims/tolerance/instruction, engine and weights from the run,
   calibration set and quorum); `create` into a scratch root.
4. Window (app stopped): SHARD seeded (G3), local state carried (G1, G2 for
   pubmed), collection file switched, mount added; node paused for it.
5. After: `records validation`, `snapshot create`, roots compared with the
   old snapshot's, `embedder check`, `hold check` (its `vectors-sample` is
   the A1 proof), embed resumed, sharing moved, search checked.
6. PubMed: pair calibration set (tools/build_pair_calibration_set.py),
   tolerance measured (G7), drop of the text-form rows (G2), throughput
   measured before the re-embed (34 h at 38 chunks/s … 4.3 h at 300/s),
   `vega-medcpt-article-ov` pinned for the run.

Followers: none left (the gutenberg minis were deleted 2026-10-09).

## PubMed's re-embed, as it ran (2026-10-10/11)

- Started 2026-10-10 11:59 UTC on run `fec33531…`; paused for the
  translator benchmark and the room's heat; resumed 22:23 UTC.
- **Hung 22:37 UTC for 7 h.** `louise-medcpt-article-ov` had four gRPC threads
  stuck in OpenVINO's `infer_new_request` (py-spy), one spinning a core; each
  of the worker's 5-minute retries timed out at 300 s, and LocalAI's "deleting
  the process to recreate it" never killed it (killed by pid). Fixed in
  `f880ce0`: one inference per compiled model at a time (both MedCPT
  backends). The new backend is another engine: run `165bac06…` passed the
  gate by itself (16/16, batch-invariant True, which the old run was not) and
  is archived (`louise-platform-images/backends/90f77652….tar.zst`).
  Throughput unchanged: ~40 docs/s at concurrency 4, i.e. ~31 h for the
  4.5 M left.
- **Hung again 06:08 UTC, and the lock was not the fix.** One thread in
  `infer`, three waiting on the lock: a single inference never returned.
  Both hangs began within seconds of the host fan script's `gpu_wake`
  (debugfs `forcewake_all`, 22:27:07 and 06:08:50 UTC), which samples
  `gtidle/idle_status` once and so catches the card in gt-c6 between two
  requests. Gutenberg's llama.cpp encoder stalled too (06:28). Both backends
  killed by pid and reloaded (06:35); the warm-up inference completed.
  STATUS.md "Open after the migration" holds the rest.
