#!/bin/bash
# Deploy a gated commit to the node (the pattern of deploy-louise-bundles-root.sh,
# without its one-off steps: the bundles are on tank already, contribute.sh is
# retired). /tmp/louise-src.tgz = git archive <sha> app tests tools docs
# calibration deploy requirements.txt Dockerfile platform.toml.example;
# /tmp/louise-commit = <sha>.
#   ssh root@<node> bash -s < deploy-louise-tip.sh
# deploy-louise-888a9be.sh — 888a9be: the possession index cache's own temp file, explain_part (29398cf); then a0c43b6: possession part entries without a shared temp file, a failed count no figure (f386934, 5863f81); then 50030dc: query processing first, corpus embedding last, corpus work stands aside on the card while a query step holds the claim (7cc6975, the claim per step); a reranker 5xx asked again once, the fallback carrying the server message, a person's requests read /system fresh (b573be4, ec26d80); on c3a7612; then c3a7612: annotation updates (next snapshot of the same collection; newer snapshots pulled), the BM25F knob, the network origin check, deploy/agent, the restated rule; then a fetched SHARD's content hashes fill the manifest's empty column (267a212 cherry-picked); then a record held in an accepted bundle file is carried, not embedded (2ef7b18, f2525db, 229b105 cherry-picked); then a refresh writes a delta, deltas merge past four (af5fd62), every mode write recorded (644c1f1), measuring.md clone lesson; then seed-only stands down settle-runs (8f2c394) and the publisher (979bdfe), the worker logs the mode it acts on (94964dd); then nothing statically pinned (11505cf), pins by use on any LocalAI model, summaries hold the chat model (5ace2dc); then a pin holder in flight is never dropped (abe1cf4), refresh/compaction 'started' lines (1069478); then scoped model pins with restore (1f59e16, 51a52c2), staged encoders unpinned (a819c6b), LocalAI threshold 0.85 in the repo (1b08113), staged YAMLs = deployed (29583b8), rewrite drains writers (3be0cf2), annotations cached snapshot (aa8edff), catalog fresh start (9c984c4); then every package pinned to the node's freeze (e264ae6, constraints.txt); tantivy==0.26.2 pinned (68a1835); fragment_count in the Lance layer (9ab13d8); the keep's Lance deletes sized from the fragment count, night steps capped (701c2d2); phase B review, predecessors.json locked, possession rebuilds logged (07b4a08); OpenAI vector stores and files over the collections (61d3be3); the search side reads the cached snapshot (f4ab0bf); one question, one search (484de1b); the Open WebUI pipe and /v1 sources (22a95fe); users and tokens, Ask open until the first user (a0edc9c); then coverage by possession across a same-space re-mint, phase B (dcdc0cf); plates by part root (9032303); then edition facts by part root (aca74d4); one passage one translation (52b1433), reader plates (3ea07f5); coverage by possession, withdraw-covered (a718a6c), the changed-record rule (33ff40b); provenance across a same-space carry (067a36f, 06547c8), edition facts by three columns (844923e), scan signature with the record's files (9ed5ed2); unindexable withdrawal (9a4d53c), the refusal in every reader (1652337); a cursor is valid only for its pair (c34c86e); gutenberg parser 10 (f9bdf66), annotations_from_titles v2 (db990c0), sort-merge keep (7b0f3e0), edition choice (0b3d245); on b561ef8
set -euo pipefail
ROOT=/mnt/ssd4/apps/louise-rag
TREE="app tests tools docs calibration"
FILES="requirements.txt requirements-dev.txt constraints.txt Dockerfile platform.toml.example"
TGZ=/tmp/louise-src.tgz
COMMIT=$(cat /tmp/louise-commit)
say() { printf '\n=== %s %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

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
grep -q "PARSER_VERSION = 10" "$ROOT/src.new/app/plugins/gutenberg/source.py"   # f9bdf66
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
    tests/hold/test_carry.py tests/ask/ tests/test_annotations_from_titles.py tests/test_gutenberg_broken_encoding.py tests/test_unindexable_stability.py tests/test_catalog_index.py tests/share/test_contribute.py tests/share/test_remint_end_to_end.py tests/share/test_accept.py tests/test_gutenberg_scan_signature.py tests/test_users.py tests/deposit/test_openai_stores.py tests/share/test_predecessors_lock.py tests/test_invariant_guards.py tests/test_image_constraints.py tests/test_model_pins.py tests/test_rewrite_drain.py tests/test_model_load.py tests/test_rerank_reasons.py tests/test_dispatch.py tests/test_louise_level.py tests/test_rerank_per_collection.py tests/share/test_possession_part_cache.py
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
  v=$(docker exec "$c" printenv LOUISE_CACHE_DIR || true); echo "$c LOUISE_CACHE_DIR $v"; [ "$v" = "/data/cache" ]
done
say "7. Ask still open (no user exists yet: a0edc9c)"
for i in $(seq 1 30); do c=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:30042/ask); [ "$c" = 200 ] && break; sleep 2; done
echo "GET /ask -> $c"; [ "$c" = 200 ]
test ! -e "$ROOT/config/users.toml" && echo "no users.toml: Ask open, as before" || echo "users.toml present"
echo; echo "DEPLOYED $COMMIT. src.old/ and louise-rag:pre-$COMMIT are the way back."
