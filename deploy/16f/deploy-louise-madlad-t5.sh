#!/bin/bash
# Deploy a gated commit to the node (the pattern of deploy-louise-bundles-root.sh,
# without its one-off steps: the bundles are on tank already, contribute.sh is
# retired). /tmp/louise-src.tgz = git archive <sha> app tests tools docs
# calibration deploy requirements.txt Dockerfile platform.toml.example;
# /tmp/louise-commit = <sha>.
#   ssh root@<node> bash -s < deploy-louise-tip.sh
# Run each deploy from a PRIVATE COPY (cp … /root/deploy-run-<sha>.sh; bash that copy):
# bash reads a script as it runs, and a copy over the running one broke a deploy between
# its stop and its start (lessons-learned.md, 2026-10-31).
# A deploy that dies between app.stop and a verified start STARTS THE APP ITSELF (the
# trap after the stop job) — UNLESS LOUISE IS HELD: an operator who wants Louise to stay
# down does `touch /root/supervise/louise.hold` (the convention of supervise-pass.sh's
# passes). While that file exists no deploy begins, and the trap does not start the app
# (operator, 2026-10-31: "if I stop it from freenas, likely because I want to fix
# something ... it shouldn't restart by itself"). `rm` it to deploy again.
# deploy-louise-madlad-t5.sh — main 931887e (2026-10-28): MADLAD on llama.cpp, the t5 shape of
# app/translate.py (3c376ca: one raw "<2xx> segment" completion per segment, the shape read off
# the server), the Library recount wording (7c969a6). Run with UNTIL=3 first; the backend
# `vega-llama-cpp-t5` and the model swap (louise-translate-madlad = the GGUF with an f32 KV
# cache, the torch one kept as louise-translate-madlad-torch) go in between, then the full run
# promotes and restarts, so the old client meets the new model for minutes, not longer.
# Second run, 2c7ab36 (24d41b9): the t5 shape told by the vocabulary (the real /v1/tokenize
# makes "<2en>" two pieces, so 931887e's client took the batch shape: a 92 s warm), and every
# batch-shape request bounded by max_tokens.
# Third run, b7a0324: the answer's [n] kept out of MADLAD's sight (it dropped and renumbered
# them, so the Ask page refused the answer), a missed answer said on the page, the stream
# logged; pubmed's facets pass marked durably (index/facets/snapshot.json), paid once more here.
# Before it, deploy-louise-profiles.sh — main (2026-10-27): node profiles (platform.toml [schedule]/[profiles]/
# [gpu.levels]; app/node_profile.py, app/cpu_limit.py, app/gpu_level.py — power1_cap where the
# card has no power1_max), rename carries the fast drive caches (b50d844), a plain create
# refuses --dry-run (33243a5), model pins exit release (43f1556). The secrets moved in the
# previous deploy (deploy-louise-secrets.sh): here they are only checked. The GPU governor
# app is updated from deploy/truenas-gpu-governor.yaml after the start (hwmon, /config, TZ).
# Before it, deploy-louise-secrets.sh (16a4349); before that, deploy-louise-888a9be.sh — 888a9be: the possession index cache's own temp file, explain_part (29398cf); then a0c43b6: possession part entries without a shared temp file, a failed count no figure (f386934, 5863f81); then 50030dc: query processing first, corpus embedding last, corpus work stands aside on the card while a query step holds the claim (7cc6975, the claim per step); a reranker 5xx asked again once, the fallback carrying the server message, a person's requests read /system fresh (b573be4, ec26d80); on c3a7612; then c3a7612: annotation updates (next snapshot of the same collection; newer snapshots pulled), the BM25F knob, the network origin check, deploy/agent, the restated rule; then a fetched SHARD's content hashes fill the manifest's empty column (267a212 cherry-picked); then a record held in an accepted bundle file is carried, not embedded (2ef7b18, f2525db, 229b105 cherry-picked); then a refresh writes a delta, deltas merge past four (af5fd62), every mode write recorded (644c1f1), measuring.md clone lesson; then seed-only stands down settle-runs (8f2c394) and the publisher (979bdfe), the worker logs the mode it acts on (94964dd); then nothing statically pinned (11505cf), pins by use on any LocalAI model, summaries hold the chat model (5ace2dc); then a pin holder in flight is never dropped (abe1cf4), refresh/compaction 'started' lines (1069478); then scoped model pins with restore (1f59e16, 51a52c2), staged encoders unpinned (a819c6b), LocalAI threshold 0.85 in the repo (1b08113), staged YAMLs = deployed (29583b8), rewrite drains writers (3be0cf2), annotations cached snapshot (aa8edff), catalog fresh start (9c984c4); then every package pinned to the node's freeze (e264ae6, constraints.txt); tantivy==0.26.2 pinned (68a1835); fragment_count in the Lance layer (9ab13d8); the keep's Lance deletes sized from the fragment count, night steps capped (701c2d2); phase B review, predecessors.json locked, possession rebuilds logged (07b4a08); OpenAI vector stores and files over the collections (61d3be3); the search side reads the cached snapshot (f4ab0bf); one question, one search (484de1b); the Open WebUI pipe and /v1 sources (22a95fe); users and tokens, Ask open until the first user (a0edc9c); then coverage by possession across a same-space re-mint, phase B (dcdc0cf); plates by part root (9032303); then edition facts by part root (aca74d4); one passage one translation (52b1433), reader plates (3ea07f5); coverage by possession, withdraw-covered (a718a6c), the changed-record rule (33ff40b); provenance across a same-space carry (067a36f, 06547c8), edition facts by three columns (844923e), scan signature with the record's files (9ed5ed2); unindexable withdrawal (9a4d53c), the refusal in every reader (1652337); a cursor is valid only for its pair (c34c86e); gutenberg parser 10 (f9bdf66), annotations_from_titles v2 (db990c0), sort-merge keep (7b0f3e0), edition choice (0b3d245); on b561ef8
set -euo pipefail
ROOT=/mnt/ssd4/apps/louise-rag
TREE="app tests tools docs calibration"
FILES="requirements.txt requirements-dev.txt constraints.txt Dockerfile platform.toml.example"
TGZ=/tmp/louise-src.tgz
COMMIT=$(cat /tmp/louise-commit)
HOLD=/root/supervise/louise.hold
say() { printf '\n=== %s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
[ ! -e "$HOLD" ] || { echo "$HOLD exists: Louise is held by the operator; no deploy begins (rm it to deploy)"; exit 1; }

say "1. stage $COMMIT beside the live tree (live: $(cat $ROOT/src/.deployed-commit))"
test -s "$TGZ" || { echo "no $TGZ"; exit 1; }
rm -rf "$ROOT/src.new"; mkdir -p "$ROOT/src.new"
tar -xzf "$TGZ" -C "$ROOT/src.new"
grep -q "^REPLACED_VERSION = -1" "$ROOT/src.new/app/embeddings.py"            # 77148be
grep -q "def _current_holders" "$ROOT/src.new/app/embeddings.py"              # 77148be
grep -q "^def fit_to_window" "$ROOT/src.new/app/retrieval.py"                # 6f00cfa
grep -q "_tell_tree" "$ROOT/src.new/deploy/vega/vega_wrapper.py"             # 8cbb8ad
grep -q "def seed_holder" "$ROOT/src.new/app/hold/seed.py"                    # 44c4a4f
grep -q "PACK_DIRNAME in p.relative_to" "$ROOT/src.new/app/plugins/wikidata/source.py"   # 9c36a98
grep -q "recut with --rebuilt needs --keep" "$ROOT/src.new/app/louise.py"     # 0a3118f
grep -q "^MEASURED_ENV" "$ROOT/src.new/deploy/vega/vega_wrapper.py"           # 23c7506
grep -q "def determinism_lacks" "$ROOT/src.new/deploy/vega/vega_wrapper.py"   # 671359b
grep -q "def keep_producer_scan" "$ROOT/src.new/app/hold/carry.py"           # e3f4687
grep -q "embeddings.scan_columns(coll" "$ROOT/src.new/app/hold/carry.py"     # b561ef8
grep -q "^def mentions" "$ROOT/src.new/app/ask/figures.py"                   # de64164
grep -q "PARSER_VERSION = 11" "$ROOT/src.new/app/plugins/gutenberg/source.py"   # 693ffcc (10: f9bdf66)
grep -q "gh-115634" "$ROOT/src.new/app/parallel.py"                              # c3dcb4c
grep -qE "^(PAGES|ROWS)_AHEAD = " "$ROOT/src.new/app/parallel.py"                 # 21da0e3 (PAGES_AHEAD = 16; in rows since 4cafea4)
grep -q "CREATE TABLE IF NOT EXISTS issues" "$ROOT/src.new/app/db.py"            # 787ebad
grep -q 'env.get("ADMIN_TOKEN_FILE")' "$ROOT/src.new/app/config.py"              # invariant 8
test -s "$ROOT/src.new/deploy/16f/move_secrets.py"                               # 23bde8c
test -s "$ROOT/src.new/app/node_profile.py"                                       # cfad93d
test -s "$ROOT/src.new/app/cpu_limit.py"                                          # b1995ed
grep -q '"power1_max", "power1_cap"' "$ROOT/src.new/app/gpu_level.py"              # this deploy
grep -q "^def _cache_moves\|derived.held" -r "$ROOT/src.new/app/hold/rename.py"   # b50d844
grep -q "belongs to \`create annotations\`" "$ROOT/src.new/app/louise.py"          # 33243a5
grep -q "gpuhwmon" "$ROOT/src.new/deploy/truenas-gpu-governor.yaml"               # fcafd3a
grep -q "carry-keep-runs" "$ROOT/src.new/app/hold/carry.py"                  # 7b0f3e0
grep -q "gutenberg-same-title" "$ROOT/src.new/tools/annotations_from_titles.py"   # db990c0
grep -q "^def edition_facts" "$ROOT/src.new/app/ask/works.py"                 # 0b3d245
grep -q "^def _cursor" "$ROOT/src.new/app/hold/carry.py"                       # c34c86e
grep -q "^def withdraw" "$ROOT/src.new/app/unindexable.py"                     # 9a4d53c
grep -q "_refuse_if_broken(rdf.parse" "$ROOT/src.new/app/plugins/gutenberg/source.py"   # 1652337
grep -q "^def predecessors" "$ROOT/src.new/app/share/contribute.py"            # 067a36f
grep -q "own_work" "$ROOT/src.new/app/share/contribute.py"                     # 06547c8
grep -q "^MEMBERS = " "$ROOT/src.new/app/plugins/cursor.py"                   # 9ed5ed2
grep -q "^def covered_by_others" "$ROOT/src.new/app/share/possession.py"        # a718a6c
grep -q "_reader_plates" "$ROOT/src.new/app/web.py"                          # 3ea07f5
grep -q "facts" "$ROOT/src.new/app/ask/works.py"                                # aca74d4
grep -q "^def records_by_uid" "$ROOT/src.new/app/hold/land.py"                  # dcdc0cf
grep -q "^def add_predecessor" "$ROOT/src.new/app/share/contribute.py"          # dcdc0cf
grep -q "^def _row_shard" "$ROOT/src.new/app/share/accept.py"                    # dcdc0cf
grep -q "^class PartCache" "$ROOT/src.new/app/ask/part_cache.py"                # 9032303
grep -q "^def cached_snapshot" "$ROOT/src.new/app/share/seeder.py"              # f4ab0bf
grep -q "web.kept_search(body.search_id" "$ROOT/src.new/app/api.py"             # 484de1b
test -s "$ROOT/src.new/deploy/openwebui/louise_pipe.py"                          # 22a95fe
grep -q "^class UserError" "$ROOT/src.new/app/users.py"                          # a0edc9c
test -s "$ROOT/src.new/app/openai_stores.py"                                      # 61d3be3
grep -q "^PREDECESSORS_LOCK_S" "$ROOT/src.new/app/share/contribute.py"           # 07b4a08
grep -q "possession index rebuilt" "$ROOT/src.new/app/share/possession.py"         # 07b4a08
grep -q "^def _delete_batch" "$ROOT/src.new/app/hold/carry.py"                  # 701c2d2
grep -q "^def fragment_count" "$ROOT/src.new/app/embeddings.py"                 # 9ab13d8
grep -q "^tantivy==0.26.2" "$ROOT/src.new/requirements.txt"                     # 68a1835
grep -q "^RUN pip install --no-cache-dir -c constraints.txt -r requirements.txt" "$ROOT/src.new/Dockerfile"   # e264ae6
grep -qx "lancedb==0.40.0" "$ROOT/src.new/constraints.txt"                       # e264ae6
test -s "$ROOT/src.new/app/model_pins.py"                                          # 1f59e16
grep -q "def using" "$ROOT/src.new/app/model_pins.py"                              # abe1cf4
grep -q "speaks_localai" "$ROOT/src.new/app/model_pins.py"                          # 5ace2dc
grep -q "^def newer_from_digests" "$ROOT/src.new/app/share/accept.py"             # annotation updates
grep -q "REWRITE_DRAIN_S" -r "$ROOT/src.new/app"                                  # 3be0cf2
grep -q "^RERANK_RETRY_PAUSE_S = 0.25" "$ROOT/src.new/app/retrieval.py"             # e9cf8d9 (b573be4)
grep -q "^def _rerank_call" "$ROOT/src.new/app/retrieval.py"                        # e9cf8d9 (b573be4)
grep -q "^def stand_aside" "$ROOT/src.new/app/dispatch.py"                           # 50030dc (7cc6975)
grep -q "mkstemp" "$ROOT/src.new/app/share/possession.py"                              # a0c43b6 (f386934)
grep -q '^SHAPE_T5 = "t5"' "$ROOT/src.new/app/translate.py"                              # 3c376ca
grep -q "^def _batch_bound" "$ROOT/src.new/app/translate.py"                             # 24d41b9: the shape by vocabulary, max_tokens
grep -q "^def _strip_cites" "$ROOT/src.new/app/translate.py"                             # b7a0324: the [n] out of the model's sight
grep -q '^MARKER = "snapshot.json"' "$ROOT/src.new/app/ask/facets.py"                    # b7a0324: the facets pass marked durably
grep -q "^def _unglue" "$ROOT/src.new/app/translate.py"                                  # ce64954: a space back between glued sentences
grep -q "^def _first_delta" "$ROOT/src.new/app/hold/snapshot.py"                         # 06ddecd: a first snapshot's delta from the uids alone (~11 GB)
grep -q "os.replace(tmp, out)" "$ROOT/src.new/app/hold/snapshot.py"                       # de3ca8f: the snapshot file written whole or not at all
grep -q '@ask_router.post("/ask/relay")' "$ROOT/src.new/app/web.py"                       # 32e1a01: the Ask relay
grep -q "^def release_idle" "$ROOT/src.new/app/parallel.py"                               # 32e1a01: an idle parse pool released
grep -q 'run `hold records validation`' "$ROOT/src.new/app/hold/snapshot.py"              # 5d01178: the members checked before the read
grep -q "^class OriginOutsideBase" "$ROOT/src.new/app/plugin_io.py"                       # 3e9b198: an origin reaches the base through a link, or the build stops
test -s "$ROOT/src.new/app/hold/relocate.py"                                              # 3e9b198: `hold records relocate`
grep -q "interrupted: bool = False" "$ROOT/src.new/app/hold/relocate.py"                  # 6893b8a: relocate's row before its file; a stop exits 130, a re-run continues
grep -q "replaced version(s) of" "$ROOT/src.new/app/hold/snapshot.py"                      # c96add6 (0710ee4): a later snapshot's delta from the versions held
grep -q 'add_argument("--api", choices=("openai", "vega")' "$ROOT/src.new/tools/encoder_bakeoff.py"   # 09e61a5: the bake-off tags vega- inputs
grep -q "^def paired(" "$ROOT/src.new/tools/encoder_bakeoff.py"                            # 05adb54: the paired randomization test
grep -q "queries = Queries(texts=queries.texts\[:limit\]" "$ROOT/src.new/tools/encoder_bakeoff.py"  # fd74d32: a smoke cuts the questions too
grep -q 'p.add_argument("--from-collection"' "$ROOT/src.new/tools/encoder_bakeoff.py"        # 454c35c: the served query side
grep -q "^def files(root" "$ROOT/src.new/app/plugins/walk.py"                              # e7c8732 (3051ad1): every corpus walk follows links
test -s "$ROOT/src.new/tools/wikipedia_drop_twins.py"                                      # e7c8732 (2f20c8e): the 21.9 M dump twins
grep -q "^def discard(" "$ROOT/src.new/app/hold/discard.py"                                # e7c8732 (9b099ae): hold snapshot discard
grep -q "token = body.token.strip()" "$ROOT/src.new/app/web.py"                             # e5abaa9 (c321771): /admin/login strips the pasted token
grep -q '^DISCARDED = ".discarded"' "$ROOT/src.new/app/hold/discard.py"                    # f1492ad (15428cd): discarded parts stay inside the SHARD (EXDEV)
grep -q '<div class="brand">' "$ROOT/src.new/app/templates/base.html"                      # db3a5c1 (d3fdc91): the name centred above the menu
grep -q '<div class="brand">Project Louise</div>' "$ROOT/src.new/app/templates/base.html"  # bc066c4 (f4095ac): the header says Project Louise
grep -q 'name="how" value="search"' "$ROOT/src.new/app/templates/ask.html"                  # 1fc7880 (f485cbe): Ask — one button, the Search only / AI summary switch
grep -q "^def _map_parts(fn, tasks" "$ROOT/src.new/app/hold/check.py"                       # 8f3cf19 (5dd76db): hold check's parts in processes
grep -q "^def each_part(fn, tasks" "$ROOT/src.new/app/parallel.py"                          # 981e3f1 (1776cc1): later delta and vectors-sample in processes; whole quota
grep -q "since the last line" "$ROOT/src.new/app/worker.py"                                  # 512d99e (7233fe3): the throttled batch line carries every batch
grep -q "^_codes_by_dump: dict" "$ROOT/src.new/app/plugins/wikidata/pack.py"                # 3785520 (dc2df1c): the pack's place asked once a build
grep -q "^PLAN_WINDOW = 50_000" "$ROOT/src.new/app/pack/build.py"                            # a599f65 (235d13b): the plan walked in rowid windows by the pool
grep -q "^def write_tags(con, uid" "$ROOT/src.new/app/entities.py"                          # a599f65: fts tags computed in the children, the worker writes them
grep -q '^PLAN_INDEX = "CREATE INDEX IF NOT EXISTS idx_plan_slice' "$ROOT/src.new/app/pack/build.py"   # 5a399c6 (5fc83ff): the plan's index built once the plan is done
grep -q "cpu.stat" "$ROOT/src.new/tools/sched_measure.sh"                                    # 1a031e3 (#83): the harness reads the container's cgroup
grep -q "^def record_hash_of(root: Path, name: str" "$ROOT/src.new/app/share/possession.py" # b844137 (09e747b): a held record's hash from its bundle's part
grep -q "_build_locks.setdefault(key" "$ROOT/src.new/app/share/possession.py"               # 6554842 (fa21777): the possession index built by one thread at a time
grep -q "errored, not waiting" "$ROOT/src.new/app/health.py"                                 # ca907a8 (1728a75): node show counts errored documents apart
grep -q "^def place_kept(kind: str, origin: Path)" "$ROOT/src.new/app/derived.py"            # d915a4d (7f47a68): the pack's place once per process; a row group per task
test -s "$ROOT/src.new/deploy/supervise-pass.sh"                                           # 772abed (55d37eb): the pass supervisor
grep -q 'if \[ "$code" -eq 130 \] || \[ "$code" -eq 143 \]; then' "$ROOT/src.new/deploy/supervise-pass.sh"   # #125 (4dcdd0b): a pass stopped on purpose is not restarted
grep -q "mem_limit: 24g" "$ROOT/src.new/deploy/truenas-app.yaml"                           # 772abed: the caps set_mem_limits.py applies live
test -s "$ROOT/src.new/deploy/16f/set_mem_limits.py"
grep -q "^ROW_GROUP = 64" "$ROOT/src.new/app/plugins/wikidata/pack.py"                      # 009eeaf (c3f7d62): the wikidata pack in row groups of 64
grep -q "^def cmd_dev_wikidata_regroup" "$ROOT/src.new/app/louise.py"                       # 009eeaf: dev wikidata-regroup
grep -q "bundle_genesis.forget_residuals(collection, stale)" "$ROOT/src.new/app/worker.py"  # ddfc36f (8734c2e): the embed backfill fills its limit across windows
grep -q "^ROWS_AHEAD = 64_000" "$ROOT/src.new/app/parallel.py"                              # 4cafea4 (a840121): the page stream's look-ahead in rows
grep -q '^WRITER = "writer.lock"' "$ROOT/src.new/app/plugins/wikidata/pack.py"               # a4acc13 (7476af6): one wikidata pack writer across processes
test -s "$ROOT/src.new/app/backend.py"                                                       # a4acc13: every collection declares its backend template
grep -q "^_groups = GroupCache(256 << 20)" "$ROOT/src.new/app/plugins/wikidata/pack.py"      # #97: the line cache in bytes, one GroupCache (app/backend.py); was _MAX_GROUP_BYTES
grep -q "^def rechecked(" "$ROOT/src.new/app/backend.py"                                     # #98 (d75cde9): the files asked once a second per process
test -s "$ROOT/src.new/app/container_pack.py"                                                # #99 (97b2833): one CONTAINER_PACK for pubmed's pack and page_props
grep -q 'replace({column}, ?, ?)' "$ROOT/src.new/app/hold/rename.py"                         # 954ed47: a rename re-roots every path a locator names
grep -q "confirmLogout" "$ROOT/src.new/app/templates/base.html"                              # #100 (3fecd20): "Log out" with a confirmation; Ask's report above the answer
grep -q '^PREFIX = "origin missing: "' "$ROOT/src.new/app/origin_missing.py"                 # #101 (9afeed9): a missing file, the self-healing error
grep -q "THE FAST PATH, a str and a dict lookup" "$ROOT/src.new/app/plugins/wikidata/pack.py" # #101: wikidata's pack_dir fast path
grep -q "THE SWEEP RESTARTS WHEN THE COVERAGE CHANGES" "$ROOT/src.new/app/worker.py"         # #102 (3960c64): the backfill cursor keyed on coverage, not the epoch
grep -q "^def light_compact_due(" "$ROOT/src.new/app/vector_index.py"                        # #103 (270ff0c): the light compaction (small fragments, book_id index delta)
grep -q "THE NEW WALK'S WINDOW READS THE CANDIDATES" "$ROOT/src.new/app/worker.py"           # #104 (086ef34): the new walk seeks the scope index plus the rowid range
grep -q "def settle_empty(group_rows, held)" "$ROOT/src.new/app/worker.py"                   # #105 (122da4e): a document cut to nothing is marked with its group, never sent
grep -q "^REWRITE_WAIT_S = 60.0" "$ROOT/src.new/app/vector_index.py"                         # #106 (528b2af): writes wait out a short rewrite; the light compaction first
grep -q "with embeddings._write_lock(con, collection):" "$ROOT/src.new/app/bundles/genesis.py"  # #106: genesis's flush through the writer registration
grep -q "# THE LIGHT COMPACTION BEFORE EVERYTHING" "$ROOT/src.new/app/vector_index.py"        # #107 (e9f345b): the light compaction first in the maintainer's pass
grep -q '"gate-batching.json"' "$ROOT/src.new/app/hold/runs.py"                              # #108 (ede18a2): a run's batch verdict counts only at the batching it was measured at
grep -q "^def small_fragments(collection)" "$ROOT/src.new/app/vector_index.py"               # #109 (5e0d5fa): the light compaction on its own one-minute cadence
grep -q "^VECTOR_INDEX_LIGHT_CHECK_S = " "$ROOT/src.new/app/config.py"                       # #109
grep -q "^def _as_candidate(recipe, a):" "$ROOT/src.new/app/louise.py"                         # 5388ade: `hold embedder check --model` gates a candidate model name
grep -q '^TREE_TYPES = ("file", "dir", "link", "setting")$' "$ROOT/src.new/app/vega/service.py"   # #116 (f1fd164): the receipts service takes the wrapper's `setting` entry, BEFORE the wrappers move
grep -q "^def find(root: Path, name: str, role: str, \*, weights: str, set_id: str," "$ROOT/src.new/app/hold/cpu_reference.py"   # #119 (bbf6628): the stored CPU reference, statement version 5, own bytes enforced
grep -q "^def fresh_loads(texts: Sequence, embed_one, reload: " "$ROOT/src.new/app/hold/embedder.py"   # #120 (5f49e24): the multi-load gate, `exactness = "near"` opt-in
grep -q '^IDLE, RUNNING, HELD, BLOCKED = "idle", "running", "held", "blocked"$' "$ROOT/src.new/app/health.py"   # #126 (2c765fa): a stage the profile pauses says so
grep -q "^def _merge_by_relevance(lists: list\[tuple\[list\[dict\], Any\]\]) -> list\[dict\]:" "$ROOT/src.new/app/retrieval.py"   # #128 (a46fe9b): lists merged on calibrated relevance (off until a calibration)
test -s "$ROOT/src.new/tools/measure/fusion_calibration.py"                                  # #128
grep -q "^    batch: int = 0$" "$ROOT/src.new/app/collections.py"                               # #129 (93b435e): [encoder] batch, a collection's own, gated at it
grep -q '"weight": "catalog_first"' "$ROOT/src.new/app/retrieval.py"                        # #130 (4ee000d): invariant 4's weights say what moved a result
grep -q '^ONE_ORIGIN = ("wikidata",)$' "$ROOT/src.new/app/slice_scope.py"                    # #131 (90b44fa): what a node holds is what it processes (wikidata's families)
test -s "$ROOT/src.new/tools/measure/wikidata_encoder_ab.py"                                 # #132 (572357c): wikidata's encoder A/B, jina-v3 against bge-m3
grep -q "^class StaleUnit(ValueError):" "$ROOT/src.new/app/plugin_io.py"                            # #135 (5d95bbd): the records build stops on a stale unit (wikipedia's QID-less archive records)
grep -q "^def _hints(collection, rows, context, slice_hint, pooled: bool)" "$ROOT/src.new/app/slice_scope.py"   # #136 (125aaaa): the slice-scope hints in the parse pool
grep -q "^DECLINED = -1$" "$ROOT/src.new/app/slice_scope.py"                                  # #137 (f830784): hold all, embed some (embed_slices, embed_done = -1)
grep -q "^JINA_INSTRUCTIONS = " "$ROOT/src.new/tools/measure/wikidata_encoder_ab.py"                         # #138 (60c74e3): jina-v3 on llama.cpp joins the wikidata encoder comparison
grep -q "A HELD LINE IS TAKEN UNLESS THE MANIFEST PROVES ITS UNIT CHANGED" "$ROOT/src.new/app/pack/build.py"     # #139 (6db02bd, 7812979): wikipedia re-cut from held lines; index-drift bounded by a part
grep -q "^def unembedded_why_not" "$ROOT/src.new/app/hold/carry.py"                                      # A1h: dev carry --unembedded (wikidata into bge-m3, nothing embedded)
grep -q '^NEW_TABLE_STORAGE = {"new_table_enable_stable_row_ids": "true"}' "$ROOT/src.new/app/embeddings.py"        # #145 (388fff5): vector tables created with stable row ids
grep -q '^STATE = "rewrite-stable.json"' "$ROOT/src.new/app/vector_rewrite.py"                       # #146 (40c2127): dev rewrite-vectors, an existing table onto stable row ids
grep -q "^def build_lang_table(tbl, \*, log=None) -> dict:" "$ROOT/src.new/app/vector_index.py"           # #147 (d5f6c11): a BITMAP on lang
grep -q "^def _ship(line: bytes) -> tuple\[bytes, int\]:" "$ROOT/src.new/app/pack/build.py"              # #148 (65b6261): a line crosses compressed from child to part writer
grep -q "^def prefetch_held(collection, rows: list\[dict\]) -> None:" "$ROOT/src.new/app/pack/build.py"   # #149 (e8e4025): a part queued behind each child; held lookups per part
grep -q "^def _columns_of(buf: list\[dict\], lines: list) -> tuple | None:" "$ROOT/src.new/app/pack/build.py"   # #150 (5487524): a part to its writer as columns and one bytes object
grep -q '^def set_paused(name: str, paused: bool, \*, by: str = "") -> set\[str\]:' "$ROOT/src.new/app/node_state.py"   # #122 (4ef5c49): a pause records who and when
grep -q "^def _non_finite(vector: bytes) -> bool:" "$ROOT/src.new/tools/vega_repro_check.py"   # #123 (e9ca1da): a NaN fails the repro check and the gate
grep -q "^def _stdout_widowed() -> bool:" "$ROOT/src.new/app/process_exit.py"               # SIGPIPE ignored: a killed pool child no longer kills its parent
test -x "$ROOT/src.new/deploy/docker-memory-ceiling.sh"                                     # the ceiling on every container together
grep -q "^def building(dest: Path)" "$ROOT/src.new/app/derived.py"                          # one pack builder across processes (the plan's pool children)
grep -q "derived = root / name / INDEX / facets_mod.CACHE" "$ROOT/src.new/app/hold/discard.py"  # c1b6156 (0fc6451): discard removes only index/facets
grep -q '"held-record"' "$ROOT/src.new/app/parallel.py"                                     # 7faa83b (39d0086): held record lines through the parse pool (the kind's test reshaped in 5cec41d)
grep -q "^def with_line(collection" "$ROOT/src.new/app/pack/build.py"                       # 5cec41d (060b4c7): the pool's children encode each record line
grep -q "^def part_writers() -> int:" "$ROOT/src.new/app/pack/build.py"                     # 6a36ff7 (f616b6c): parts written by writer threads
grep -q "^def _slim(record: dict) -> dict:" "$ROOT/src.new/app/pack/build.py"               # 8cfc602 (055d6eb): children find their documents, return slim records
grep -q "^def part_writer_pool():" "$ROOT/src.new/app/pack/build.py"                        # 9797782 (325aa08): parts written by processes
grep -q "^FIRST_DELTA_READERS = " "$ROOT/src.new/app/hold/snapshot.py"                      # 6f7232b (478e160): a first snapshot's delta from the parts' indexes, by threads
grep -q ':root\[data-theme="kelsch"\]' "$ROOT/src.new/app/static/admin.css"                 # 474fa3e (9abeeb3): four colour themes
test -s "$ROOT/src.new/deploy/vega/stage/models/louise-translate-madlad-torch.yaml"        # 931887e
! grep -q 'cache_type_' "$ROOT/src.new/deploy/vega/stage/models/louise-translate-madlad.yaml"   # d372ba5: the F16 cache (P5); b68820c had f32
test ! -e "$ROOT/src.new/deploy/reference"
echo "staged: $(find "$ROOT/src.new" -name '*.py' | wc -l) python files"

say "2. build louise-rag:staged"
docker build -q -t louise-rag:staged "$ROOT/src.new"
docker run --rm louise-rag:staged python -c "import app.web, app.worker, app.hold.carry; print('imports ok')"
docker run --rm louise-rag:staged pip freeze > /tmp/staged-freeze.txt
grep -v "^#" "$ROOT/src.new/constraints.txt" | grep -v "^$" | diff - /tmp/staged-freeze.txt && echo "staged freeze == constraints.txt" || { echo "the staged image moved off constraints.txt; NOT promoting"; exit 1; }
docker run --rm louise-rag:staged lbzip2 --version 2>&1 | head -1

say "3. the live config loads every collection in the staged code"
LIVE_MOUNTS=$(docker inspect ix-louise-rag-web-1 \
  --format '{{range .Mounts}}-v {{.Source}}:{{.Destination}}{{if eq .Destination "/config"}}:ro{{else if not .RW}}:ro{{end}} {{end}}')
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv $LIVE_MOUNTS \
  -e CONFIG_DIR=/config -e DATA_DIR=/data louise-rag:staged python -c "
from app import collections as c
names = sorted(x.name for x in c.load_all())
print('load_all:', names)
print('refusals:', [str(r)[:200] for r in c.refusals()])
assert {'gutenberg', 'normattiva', 'pubmed', 'wikidata', 'wikipedia'} <= set(names), names   # annotations collections are not pipeline collections
assert not c.refusals(), c.refusals()
"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/hold/test_carry.py tests/ask/ tests/test_annotations_from_titles.py tests/test_gutenberg_broken_encoding.py tests/test_unindexable_stability.py tests/test_catalog_index.py tests/share/test_contribute.py tests/share/test_remint_end_to_end.py tests/share/test_accept.py tests/test_gutenberg_scan_signature.py tests/test_users.py tests/deposit/test_openai_stores.py tests/share/test_predecessors_lock.py tests/test_invariant_guards.py tests/test_image_constraints.py tests/test_model_pins.py tests/test_rewrite_drain.py tests/test_model_load.py tests/test_rerank_reasons.py tests/test_dispatch.py tests/test_louise_level.py tests/test_rerank_per_collection.py tests/share/test_possession_part_cache.py \
    tests/test_invariant_8_secrets.py tests/test_deploy_move_secrets.py tests/test_identity.py tests/test_node_init.py \
    tests/test_parallel.py tests/test_parallel_exit.py tests/test_issues.py tests/ask/test_figures.py \
    tests/test_gpu_level.py tests/test_gpu_governor.py tests/test_node_start_stop.py tests/test_worker_hot_reload.py \
    tests/test_translate_t5.py tests/test_translate_model.py tests/test_query_translation.py tests/test_madlad_translate_server.py \
    tests/test_encoder_bakeoff.py tests/test_web_pages.py tests/test_gpu_models.py \
    tests/test_batch_log.py tests/test_plugins_wikidata_families.py tests/pack/test_build.py tests/test_stage_locking.py tests/test_embed_window.py tests/test_possessed_vectors.py tests/test_refs_normattiva.py tests/test_health.py tests/test_invariant_6_storage.py tests/test_deploy_set_mem_limits.py tests/test_deploy_set_platform_image.py tests/test_worker_selection_cost.py tests/test_process_exit.py tests/test_deploy_supervise_pass.py tests/test_derived_cache.py tests/test_plugins_pubmed_pack.py tests/test_backend_templates.py tests/test_wikipedia_page_props.py tests/test_plugins_wikipedia_source.py tests/test_origin_missing.py tests/test_vector_index.py tests/test_embed_producers.py tests/test_run_check.py \
    tests/hold/test_discard.py tests/hold/test_check.py tests/hold/test_embedder_cli.py tests/hold/test_cpu_reference.py tests/hold/test_near_exact.py tests/test_encoder_batch.py tests/test_slice_scope.py tests/hold/test_relocate.py tests/test_toml_roundtrip.py tests/test_measure_wikidata_encoder_ab.py tests/test_merge_by_relevance.py tests/test_record_rerank.py tests/test_measure_fusion_calibration.py tests/test_node_show.py tests/test_vega_repro_check.py tests/test_worker_boot_node_state.py tests/hold/test_embedder.py tests/hold/test_backend_engine.py tests/hold/test_engine_options.py tests/hold/test_pair_calibration.py tests/hold/test_vectors_check.py tests/hold/test_later_delta.py tests/test_discovery_moved_corpus.py tests/test_plugin_walk.py tests/test_wikipedia_drop_twins.py tests/test_wikipedia_two_sources.py tests/hold/test_drift.py tests/test_record_source.py tests/test_lance_stable_row_ids.py tests/test_vector_rewrite.py \
    $(cd "$ROOT/src.new" && ls tests/test_node_profile*.py tests/test_cpu_limit*.py 2>/dev/null)
# test_rename.py in its own run: in one invocation with tests/ask/ its directory's
# conftest `codex` fixture is not found (2026-10-27; alone and in the full suite it passes)
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider tests/hold/test_rename.py
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv -v "$ROOT/config:/config:ro" \
  -e CONFIG_DIR=/config louise-rag:staged python -c "
from app import node_profile as np
print('the live platform.toml, read by the staged code: profile now', np.active())"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/vega/ tests/test_localai_backends.py
find "$ROOT/src.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$ROOT/src.new/tests/fixtures/pubmed-mini/.pack"

if [ "${UNTIL:-}" = 3 ]; then
  find "$ROOT/src.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
  echo "PROVED $COMMIT; stopped before the swap (UNTIL=3)"; exit 0
fi

say "4. promote (src -> src.old, src.new -> src; deploy/ overlaid)"
docker tag louise-rag:local "louise-rag:pre-$COMMIT"
cd "$ROOT"
rm -rf src.old && mkdir -p src.old
for d in $TREE; do chmod -R u+w "src/$d" 2>/dev/null || true; mv "src/$d" src.old/ 2>/dev/null || true; mv "src.new/$d" src/; done
for f in $FILES; do mv "src/$f" src.old/ 2>/dev/null || true; mv "src.new/$f" src/; done
cp -a src/deploy src.old/deploy
cp -a src.new/deploy/. src/deploy/
rm -rf src.new
docker build -q -t louise-rag:local "$ROOT/src"
echo "$COMMIT" > "$ROOT/src/.deployed-commit"
NEW_ID=$(docker inspect louise-rag:local --format '{{.Id}}')
echo "louise-rag:local = $NEW_ID"

say "5. restart: stop (wait for the job), LOUISE_PLATFORM_IMAGE, start"
# the containers are recreated: their logs go with them unless kept (2026-10-23: a
# mode write between 12:25 and 12:34 could no longer be looked for after the 13:49 deploy)
# the identity BEFORE the key moves: its public text and a deterministic ed25519 signature
# of one fixed message (neither is secret); the same two must come back after
IDPY='from app.bundles import keys; from app import identity; k = keys.load_signing_key(); print(identity.public_text(k), k.sign(b"louise: the node key moved, 2026-10-27").signature.hex())'
ID_BEFORE=$(docker exec ix-louise-rag-worker-1 python -c "$IDPY")
echo "identity before: ${ID_BEFORE%% *}"
mkdir -p /root/louise-logs
for c in ix-louise-rag-web-1 ix-louise-rag-worker-1; do docker logs "$c" > "/root/louise-logs/$c-$(date +%Y%m%d-%H%M)-before-$COMMIT.log" 2>&1; done
ls -la /root/louise-logs | tail -2
STOP_JOB=$(midclt call app.stop louise-rag); echo "stop job $STOP_JOB"
js=""
for i in $(seq 1 180); do
  js=$(midclt call core.get_jobs "[[\"id\",\"=\",$STOP_JOB]]" | python3 -c "import sys,json; j=json.load(sys.stdin)[0]; print(j['state'], (j.get('error') or '').replace(chr(10),' ')[:300])")
  case "$js" in SUCCESS*|FAILED*|ABORTED*) break;; esac; sleep 5
done
echo "stop job: $js"
case "$js" in SUCCESS*) ;; *) echo "the stop did not succeed; NOT starting on top of it"; exit 1;; esac
# A DEPLOY THAT DIES BETWEEN app.stop AND A VERIFIED START STARTS THE APP ITSELF
# (2026-10-31: the runbook was copied over itself mid-run, bash read on into the new
# text and stopped at "syntax error" after the stop — Louise down until an agent
# noticed; restart: unless-stopped does not apply to a deliberate stop. CLAUDE.md:
# Louise survives a crash without an agent). Any exit from here on — an error, a
# signal, a check that fails — starts louise-rag on the image its config names
# (LOUISE_PLATFORM_IMAGE: the new one once set_platform_image.py ran, the one
# before otherwise; louise-rag:local is retagged to it so the two agree), says so
# loudly and exits non-zero. Cleared only once the start is verified (step 6).
STARTED=""
configured_image() {
  python3 - <<'PY'
from truenas_api_client import Client
with Client() as c:
    cfg = c.call("app.config", "louise-rag")
    print(((cfg.get("services") or {}).get("worker") or {}).get("environment", {}).get("LOUISE_PLATFORM_IMAGE") or "")
PY
}
heal() {
  local rc=$? want s i w
  [ -n "$STARTED" ] && return 0
  trap - EXIT INT TERM
  set +e
  if [ -e "$HOLD" ]; then
    echo "!!!!!! the deploy of $COMMIT ended (exit $rc) between app.stop and a verified start; $HOLD exists: held, NOT started" >&2
    exit $(( rc == 0 ? 1 : rc ))
  fi
  echo "!!!!!! the deploy of $COMMIT ended (exit $rc) between app.stop and a verified start: STARTING louise-rag" >&2
  want=$(configured_image)
  if [[ "$want" =~ ^sha256:[0-9a-f]{64}$ ]] && docker image inspect "$want" >/dev/null 2>&1; then
    docker tag "$want" louise-rag:local && echo "!!!!!! louise-rag:local -> $want (LOUISE_PLATFORM_IMAGE)" >&2
  else
    echo "!!!!!! LOUISE_PLATFORM_IMAGE '$want' is no image here: starting on louise-rag:local as it is" >&2
  fi
  s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = "RUNNING" ] || midclt call app.start louise-rag >/dev/null
  for i in $(seq 1 120); do
    s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
    w=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:30042/healthz)
    [ "$s" = "RUNNING" ] && [ "$w" = 200 ] && break; sleep 3
  done
  echo "!!!!!! louise-rag $s, web /healthz $w, worker $(docker inspect -f '{{.State.Status}} {{.Image}}' ix-louise-rag-worker-1 2>&1 | cut -c1-90); the deploy of $COMMIT did NOT complete" >&2
  exit $(( rc == 0 ? 1 : rc ))
}
trap heal EXIT
trap 'exit 130' INT TERM
if [ "${DEPLOY_TEST_DIE_AFTER_STOP:-}" = 1 ]; then echo "TEST: the deploy dies here, after the stop"; kill -TERM $$; sleep 10; fi
# the fast drive for derived data (07 §7.8; operator 2026-10-20): /data is on NVMe (ssd4)
python3 - "$ROOT/src/deploy/16f" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import flatten_mounts as fm
from truenas_api_client import Client
with Client() as c:
    cfg = c.call("app.config", fm.APP)
    said = fm.apply(cfg, shared=(), env={"LOUISE_CACHE_DIR": "/data/cache"})
    print("\n".join(said) or "LOUISE_CACHE_DIR already set")
    if said:
        c.call("app.update", fm.APP, {"custom_compose_config": cfg}, job=True)
PY
test ! -e "$ROOT/config/node.key"
# #89's memory caps on the live app (operator 2026-10-30: a kernel kill recovers by itself): idempotent
python3 "$ROOT/src/deploy/16f/set_mem_limits.py"
# the containers' sum under 90 % of the host: an overlap of caps is an OOM inside Docker, never global
bash "$ROOT/src/deploy/docker-memory-ceiling.sh" || echo "the docker memory ceiling NOT set (above); the deploy goes on"
python3 "$ROOT/src/deploy/16f/set_platform_image.py" "$NEW_ID"
s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
[ "$s" = "RUNNING" ] || midclt call app.start louise-rag >/dev/null
for i in $(seq 1 90); do
  s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = "RUNNING" ] && break; sleep 2
done
echo "state after start: $s"; [ "$s" = "RUNNING" ]

say "6. web and worker run the same, new image"
sleep 5
WEB=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}'); WRK=$(docker inspect ix-louise-rag-worker-1 --format '{{.Image}}')
echo "web $WEB"; echo "worker $WRK"
[ "$WEB" = "$WRK" ] && [ "$WEB" = "$NEW_ID" ]
for c in ix-louise-rag-web-1 ix-louise-rag-worker-1; do
  v=$(docker exec "$c" printenv LOUISE_PLATFORM_IMAGE || true); echo "$c LOUISE_PLATFORM_IMAGE $v"; [ "$v" = "$NEW_ID" ]
  m=$(docker inspect "$c" --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'); echo "$c memory cap (bytes, with swap) $m"
  case "$c" in *web*) [ "$m" = "12884901888 12884901888" ];; *worker*) [ "$m" = "25769803776 25769803776" ];; esac
  v=$(docker exec "$c" printenv LOUISE_CACHE_DIR || true); echo "$c LOUISE_CACHE_DIR $v"; [ "$v" = "/data/cache" ]
done
for i in $(seq 1 60); do w=$(curl -s -m 5 -o /dev/null -w "%{http_code}" http://127.0.0.1:30042/healthz); [ "$w" = 200 ] && break; sleep 2; done
echo "web /healthz $w, worker $(docker inspect -f '{{.State.Status}}' ix-louise-rag-worker-1)"; [ "$w" = 200 ]
[ "$(docker inspect -f '{{.State.Status}}' ix-louise-rag-worker-1)" = running ]
STARTED=1; trap - EXIT INT TERM          # the start is verified: no self-healing past here
say "6b. the secrets by path, the same identity"
for c in ix-louise-rag-web-1 ix-louise-rag-worker-1; do
  docker exec "$c" sh -c 'test -z "${ADMIN_TOKEN+x}"' && echo "$c: no ADMIN_TOKEN value in the env" || { echo "$c: ADMIN_TOKEN still in the env"; exit 1; }
  echo "$c ADMIN_TOKEN_FILE $(docker exec "$c" printenv ADMIN_TOKEN_FILE) NODE_KEY_PATH $(docker exec "$c" printenv NODE_KEY_PATH || echo unset)"
done
docker exec ix-louise-rag-web-1 sh -c 'test ! -e /run/louise/private && test ! -e /config/node.key' \
  && echo "web: no private key reachable" || { echo "web: a private key is reachable"; exit 1; }
ID_AFTER=$(docker exec ix-louise-rag-worker-1 python -c "$IDPY")
echo "identity after:  ${ID_AFTER%% *}"
[ "$ID_BEFORE" = "$ID_AFTER" ] && echo "same key text, same signature" || { echo "THE IDENTITY CHANGED"; exit 1; }
docker exec ix-louise-rag-worker-1 python -c "from app import identity; print('worker check_at_start:', identity.check_at_start())"
docker exec ix-louise-rag-web-1 python -c "
from app import identity, config
from app.bundles import keys
sig = bytes.fromhex('${ID_AFTER##* }')
pub = identity.read_public(config.NODE_PUBLIC_KEY_PATH)
keys.verify_key_from_hex(pub.split(':', 1)[1]).verify(b'louise: the node key moved, 2026-10-27', sig)
print('web: the signature verifies under', pub[:24] + '…')"
# the admin token from its file: a header file (0600) so the token reaches no command line
H=$(mktemp); chmod 600 "$H"; trap 'rm -f "$H"' EXIT
printf 'X-Admin-Token: %s\n' "$(cat "$ROOT/secrets/admin/admin-token")" > "$H"
for i in $(seq 1 30); do a=$(curl -s -o /dev/null -w "%{http_code}" -H @"$H" http://127.0.0.1:30042/api/node/show); [ "$a" = 200 ] && break; sleep 2; done
n=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:30042/api/node/show)
rm -f "$H"
echo "GET /api/node/show with the file's token -> $a, without -> $n"; [ "$a" = 200 ] && [ "$n" = 401 ]

# open while no user exists (a0edc9c); closed once users.toml holds one —
# the operator added users on 2026-10-29 19:49, and e7c8732's run stopped here
if test -e "$ROOT/config/users.toml"; then want=401; else want=200; fi
say "7. Ask answers $want (users.toml $(test -e "$ROOT/config/users.toml" && echo present || echo absent))"
for i in $(seq 1 30); do c=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:30042/ask); [ "$c" = "$want" ] && break; sleep 2; done
echo "GET /ask -> $c"; [ "$c" = "$want" ]

say "8. the GPU governor app from deploy/truenas-gpu-governor.yaml (hwmon writable, /config ro, TZ)"
python3 - "$ROOT/src/deploy/truenas-gpu-governor.yaml" <<'PY'
import sys, yaml
from truenas_api_client import Client
want = yaml.safe_load(open(sys.argv[1]))
with Client() as c:
    have = c.call("app.config", "louise-gpu-governor")
    s = want["services"]["gpu-governor"]
    print("volumes:", have["services"]["gpu-governor"].get("volumes"), "->", s["volumes"])
    c.call("app.update", "louise-gpu-governor", {"custom_compose_config": want}, job=True)
PY
for i in $(seq 1 60); do
  s=$(midclt call app.query '[["name","=","louise-gpu-governor"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = "RUNNING" ] && break; sleep 2
done
echo "governor: $s"; [ "$s" = "RUNNING" ]
GOV=$(docker ps --filter "name=louise-gpu-governor" --format '{{.Names}}' | head -1)
echo "$GOV image $(docker inspect "$GOV" --format '{{.Image}}')"; [ "$(docker inspect "$GOV" --format '{{.Image}}')" = "$NEW_ID" ]
sleep 10; docker logs --tail 20 "$GOV" 2>&1

echo; echo "DEPLOYED $COMMIT. src.old/ and louise-rag:pre-$COMMIT are the way back."
