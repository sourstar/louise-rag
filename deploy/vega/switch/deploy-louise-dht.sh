#!/usr/bin/env bash
# deploy-louise-dht.sh — member torrents off the DHT and the swarm key
# (04 §4.2 amended 2026-10-19; docs/notes/dht-flood.md), with everything
# since bc3c00f, which never deployed (the build could not reach the
# internet while the flood held the router). The DHT stays OFF here:
# `[dht] enabled` goes back on as its own step, watched (dht-flood.md).
# deploy-louise-cut.sh's runbook otherwise.
#
# Prepared on the dev PC (the agent copies the two files to /tmp):
#   git archive <COMMIT> app tests tools docs calibration deploy \
#       requirements.txt Dockerfile platform.toml.example | gzip > louise-src.tgz
#   echo <COMMIT> > louise-commit
# Run on the node:
#   bash /root/deploy-louise-dht.sh 2>&1 | tee /root/deploy-louise-dht.log
#
# It stops at the first failure. Phases 1–3 touch nothing live. Phase 4 is
# the swap; anything wrong after it: src.old/ and louise-rag:pre-<COMMIT>.
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
TREE="app tests tools docs calibration"
FILES="requirements.txt Dockerfile platform.toml.example"
TGZ=/tmp/louise-src.tgz
COMMIT=$(cat /tmp/louise-commit)

say() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------- 1. stage
say "1. stage $COMMIT beside the live tree"
test -s "$TGZ" || { echo "no $TGZ"; exit 1; }
rm -rf "$ROOT/src.new"
mkdir -p "$ROOT/src.new"
tar -xzf "$TGZ" -C "$ROOT/src.new"
grep -q "^def swarm_key" "$ROOT/src.new/app/share/members.py"           # the swarm key
grep -q "announce=\"member\" not in entry" "$ROOT/src.new/app/acquire/seeding.py"
grep -q "^CUT_VERSION = 8" "$ROOT/src.new/app/hold/codex.py"            # bc3c00f's, still undeployed
test ! -e "$ROOT/src.new/deploy/reference"                              # never shipped (gitignored)
echo "staged: $(find "$ROOT/src.new" -name '*.py' | wc -l) python files"

# --------------------------------------------------------- 2. staged image
say "2. build louise-rag:staged"
docker build -q -t louise-rag:staged "$ROOT/src.new"
docker run --rm louise-rag:staged python -c "import app.web, app.worker, app.hold.carry; print('imports ok')"

# ------------------------------------------------------------ 3. prove it
# The whole suite ran on the workstation; host RAM is tight here. On the node:
# the live config must load every collection under the staged code, and the
# directories this deploy changes run in the staged image, one per invocation.
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
assert {'gutenberg', 'pubmed-v8', 'wikipedia', 'normattiva'} <= set(names), names
assert not c.refusals(), c.refusals()
"
say "3b. the seeding, the swarm key and the carry, in the staged image"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/share/test_seeder.py tests/share/test_single_survivor.py
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/acquire/test_seeding_peer.py
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest -q -p no:cacheprovider \
    tests/hold/test_cut.py tests/hold/test_carry.py tests/hold/test_codex.py
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
# 5b. LOUISE_PLATFORM_IMAGE = the image web and worker will start in (07 §7.3),
# through TrueNAS's API client: the app config carries the admin token and
# never reaches a command line or this log.
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
