#!/usr/bin/env bash
# rollout-20261018-b.sh — the combined rollout, phase B (operator, 2026-10-18):
# the Louise image at the wrappers' commit. It sends every input to a VeGa
# encoder whole with `ctx` as its window (docs/notes/char-cut-truncation.md),
# and carries wikipedia parser 5 (safe before its switch: no page_props on
# disk, nothing moves; docs/notes/wikipedia-qids.md §3) and the disk guard.
# deploy-louise-figures.sh's runbook otherwise. REFUSES before phase A: the
# wrapper before it refuses a tag that names a window.
#
# Run from the workstation:
#   git archive <COMMIT> app tests tools docs calibration deploy \
#       requirements.txt Dockerfile platform.toml.example | gzip > louise-src.tgz
#   scp louise-src.tgz root@node:/tmp/louise-src.tgz
#   ssh root@node "echo <COMMIT> > /tmp/louise-commit"
#   ssh root@node bash -s < deploy/16f/deploy-louise-figures.sh
#
# It stops at the first failure. Phases 1–3 touch nothing live. Phase 4 is
# the swap; anything wrong after it: BACKOUT in deploy-louise-vega.sh (the
# mounts are untouched here, so only its first block applies).
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
TREE="app tests tools docs calibration"
FILES="requirements.txt Dockerfile platform.toml.example"
TGZ=/tmp/louise-src.tgz
COMMIT=$(cat /tmp/louise-commit)

say() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------- 1. stage
say "0. phase A is on the node: both wrappers cut to a window"
for p in vega-llama-cpp vega-louise-python; do
  grep -q '"cuts"' /mnt/tank/apps/localai/backends/$p/vega.json || {
    echo "$p has no cuts key: run rollout-20261018.sh (phase A) first"; exit 1; }
done

say "1. stage $COMMIT beside the live tree"
test -s "$TGZ" || { echo "no $TGZ: scp it first"; exit 1; }
rm -rf "$ROOT/src.new"
mkdir -p "$ROOT/src.new"
tar -xzf "$TGZ" -C "$ROOT/src.new"
grep -q 'WINDOW = "window"' "$ROOT/src.new/app/vega/tag.py"                  # the window
grep -q "TOKEN_CUT" "$ROOT/src.new/app/embeddings.py"                     # the cut is the space's
grep -q "def vectors_short" "$ROOT/src.new/app/disk_guard.py"           # the disk guard
grep -q "PARSER_VERSION = 5" "$ROOT/src.new/app/plugins/wikipedia/source.py"
test ! -e "$ROOT/src.new/deploy/reference"                              # never shipped (gitignored)
echo "staged: $(find "$ROOT/src.new" -name '*.py' | wc -l) python files"

# --------------------------------------------------------- 2. staged image
say "2. build louise-rag:staged"
docker build -q -t louise-rag:staged "$ROOT/src.new"
docker run --rm louise-rag:staged python -c "import app.web, app.worker, app.disk_guard, app.plugins.wikipedia.page_props; print('imports ok')"

# ------------------------------------------------------------ 3. prove it
# The whole suite ran on the workstation (4692 passed at fb5b6b8; 405 VeGa,
# the model pool and guard tests at a2c89e8, 2026-10-18); host RAM is tight here. On the node: the live config must
# load every collection under the staged code, and the directories this
# deploy changes most run in the staged image, one per invocation.
say "3a. the live config loads every collection"
LIVE_MOUNTS=$(docker inspect ix-louise-rag-web-1 \
  --format '{{range .Mounts}}-v {{.Source}}:{{.Destination}}{{if eq .Destination "/config"}}:ro{{else if not .RW}}:ro{{end}} {{end}}')
echo "live mounts: $LIVE_MOUNTS"
# shellcheck disable=SC2086
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv $LIVE_MOUNTS \
  -e CONFIG_DIR=/config -e DATA_DIR=/data \
  louise-rag:staged python -c "
from app import collections as c
names = sorted(x.name for x in c.load_all())
print('load_all:', names)
print('refusals:', [str(r)[:200] for r in c.refusals()])
assert {'gutenberg', 'pubmed', 'wikipedia'} <= set(names), names
assert not c.refusals(), c.refusals()
"
say "3b. the encoder path, the disk guard and parser 5, in the staged image"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/vega tests/test_disk_guard.py tests/test_wikipedia_page_props.py \
    tests/test_plugins_wikipedia_source.py tests/test_localai_backends.py
# the suite writes beside itself: clean the staging tree before promoting it
find "$ROOT/src.new" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$ROOT/src.new/tests/fixtures/pubmed-mini/.pack"

# ------------------------------------------------------------- 4. promote
say "4. promote (src -> src.old, src.new -> src; deploy/ overlaid, never swapped)"
docker tag louise-rag:local "louise-rag:pre-$COMMIT"      # the way back
cd "$ROOT"
rm -rf src.old && mkdir -p src.old
for d in $TREE; do
  chmod -R u+w "src/$d" 2>/dev/null || true
  mv "src/$d" src.old/ 2>/dev/null || true
  mv "src.new/$d" src/
done
for f in $FILES; do
  mv "src/$f" src.old/ 2>/dev/null || true
  mv "src.new/$f" src/
done
cp -a src/deploy src.old/deploy
cp -a src.new/deploy/. src/deploy/     # adds and replaces; deletes nothing under deploy/
rm -rf src.new

say "4b. build louise-rag:local from the promoted tree"
docker build -q -t louise-rag:local "$ROOT/src"
echo "$COMMIT" > "$ROOT/src/.deployed-commit"
NEW_ID=$(docker inspect louise-rag:local --format '{{.Id}}')
echo "louise-rag:local = $NEW_ID"

# ------------------------------------------------------------- 5. restart
say "5. restart the app (stop; LOUISE_PLATFORM_IMAGE; start — async jobs)"
# Wait for the stop JOB itself (2026-10-07: a state poll gave up while the
# worker was still finishing its unit, and a start was called on top of it).
STOP_JOB=$(midclt call app.stop louise-rag)
echo "stop job $STOP_JOB"
js=""
for i in $(seq 1 180); do
  js=$(midclt call core.get_jobs "[[\"id\",\"=\",$STOP_JOB]]" | python3 -c "import sys,json; j=json.load(sys.stdin)[0]; print(j['state'], (j.get('error') or '').replace(chr(10),' ')[:300])")
  case "$js" in SUCCESS*|FAILED*|ABORTED*) break;; esac
  sleep 5
done
echo "stop job: $js"
case "$js" in SUCCESS*) ;; *) echo "the stop did not succeed; NOT starting on top of it — see /var/log/app_lifecycle.log"; exit 1;; esac
s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
echo "state after stop: $s"
# 5b. LOUISE_PLATFORM_IMAGE = the image web and worker will start in (07 §7.3).
# Through TrueNAS's API client: the app config carries the admin token and
# never reaches a command line or this log. The mounts are already the
# shared ones (5d of the previous runbook), so no rename rides along.
python3 "$ROOT/src/deploy/16f/set_platform_image.py" "$NEW_ID"
s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
echo "state after the update: $s"
[ "$s" = "RUNNING" ] || midclt call app.start louise-rag >/dev/null
for i in $(seq 1 90); do
  s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = "RUNNING" ] && break; sleep 2
done
echo "state after start: $s"
[ "$s" = "RUNNING" ]

say "6. web and worker run the SAME image, and it is the new one"
sleep 5
WEB=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}')
WRK=$(docker inspect ix-louise-rag-worker-1 --format '{{.Image}}')
echo "web    $WEB"; echo "worker $WRK"
[ "$WEB" = "$WRK" ] && [ "$WEB" = "$NEW_ID" ]
for c in ix-louise-rag-web-1 ix-louise-rag-worker-1; do
  v=$(docker exec "$c" printenv LOUISE_PLATFORM_IMAGE || true)
  echo "$c LOUISE_PLATFORM_IMAGE $v"
  [ "$v" = "$NEW_ID" ]
done
echo
echo "DEPLOYED $COMMIT. src.old/ and louise-rag:pre-$COMMIT are the way back."
echo "Watch the worker's first minutes:"
echo "  docker logs --since 5m ix-louise-rag-worker-1 | grep -i -E 'calibration|validation|re-check|error' | head"
