#!/usr/bin/env bash
# deploy-louise-3edbabb.sh — stage, prove, promote, restart Louise at main 3edbabb (the merge of
# 2026-10-05 plus the granite YAML). Was the scratchpad's deploy-cb419e7.sh; kept here like deploy/rage/15f/.
# (the merge of 2026-10-05: deposit collections as step 16, key handling,
# validations re-checked, git in the image, the deposit manifest race fix).
#
# Run from the workstation, in this order (cmd.exe: no single quotes):
#
#   scp -i C:\Users\alana\.ssh\truenas_root ^
#     C:\Users\alana\AppData\Local\Temp\claude\c--Users-alana-OneDrive-Documenti-Projects-louise-rag\cd2dd85b-cf97-4652-943f-c8059c4cea09\scratchpad\louise-src.tgz ^
#     root@192.168.178.10:/tmp/louise-src.tgz
#
#   ssh -i C:\Users\alana\.ssh\truenas_root root@192.168.178.10 bash -s < deploy\16f\deploy-louise-3edbabb.sh
#
# (The tarball is `git archive 3edbabb app tests tools docs calibration deploy
# requirements.txt Dockerfile platform.toml.example`; rebuild it from the
# repository with that command if the scratchpad is gone.)
#
# It stops at the first failure. Phases 1–3 touch nothing live: they stage
# beside src/, build louise-rag:staged, and prove it. Phase 4 is the swap.
# Anything wrong after phase 4: see BACKOUT at the end. A stop BETWEEN the
# promote and 4b (seen 2026-10-06: a whole-tree tarball left extras in
# src.new and the old `rmdir` failed) leaves the new src/ with the old image
# running — consistent; finish with phases 4b–6 alone, nothing else.
set -euo pipefail

COMMIT=b4fface                 # the commit the tarball was made from (git archive <COMMIT> …)
ROOT=/mnt/ssd4/apps/louise-rag
TREE="app tests tools docs calibration"
FILES="requirements.txt Dockerfile platform.toml.example"
TGZ=/tmp/louise-src.tgz

say() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------- 1. stage
say "1. stage beside the live tree"
test -s "$TGZ" || { echo "no $TGZ: scp it first"; exit 1; }
rm -rf "$ROOT/src.new"
mkdir -p "$ROOT/src.new"
tar -xzf "$TGZ" -C "$ROOT/src.new"
grep -q "_init_manifest" "$ROOT/src.new/app/hold/deposit.py"           # the race fix is in
grep -q "curl git rsync" "$ROOT/src.new/Dockerfile"                      # git in the image
grep -q "pypdfium2" "$ROOT/src.new/requirements.txt"                     # the PDF reader
test ! -e "$ROOT/src.new/deploy/reference"                              # never shipped
echo "staged: $(find "$ROOT/src.new" -name '*.py' | wc -l) python files"

# --------------------------------------------------------- 2. staged image
say "2. build louise-rag:staged"
docker build -t louise-rag:staged "$ROOT/src.new"
docker run --rm louise-rag:staged git --version
docker run --rm louise-rag:staged python -c "import pypdfium2; print('pypdfium2 ok')"

# ------------------------------------------------------------ 3. prove it
# Host RAM is tight on this node (3.7 GB available on 2026-10-06), so the
# WHOLE suite is not run here: it ran on the workstation from this exact
# commit (3514 passed; tests/deposit three times over). Here: the live
# config must load every collection, and the two directories the merge
# touched most run in the staged image — one directory per invocation.
say "3a. the live config loads every collection"
# The live web container's mounts, as it has them (the drag lives on tank since
# 2026-09-21, so $ROOT/data alone holds no codex.json — found 2026-10-06), with
# /config read-only. Mounts only: the container's env carries the admin token
# and is never dumped.
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
# the three the node has held since September, at least — a deposit
# collection (16f) is a fourth, and LocalAGI's agents will add more
assert {'gutenberg', 'pubmed', 'wikipedia'} <= set(names), names
"

say "3b. tests/deposit in the staged image"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 600 python -m pytest tests/deposit -q -p no:cacheprovider

say "3c. tests/hold in the staged image"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest tests/hold -q -p no:cacheprovider

say "3d. tests/share in the staged image (step 9: members, seeder, peer messages, fetch)"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 900 python -m pytest tests/share -q -p no:cacheprovider

say "3e. tests/ask and the verify-on-use tests in the staged image (step 11: the tools, filters, as_of, chat, T4)"
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 600 python -m pytest tests/ask -q -p no:cacheprovider
docker run --rm -v "$ROOT/src.new:/srv" -w /srv -e PYTHONPATH=/srv \
  louise-rag:staged timeout -s KILL 600 python -m pytest tests/bundles/test_verify_on_use.py -q -p no:cacheprovider

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
cp -a src.new/deploy/. src/deploy/     # adds and replaces; this merge deletes nothing under deploy/
# whatever is left is the tarball's extras (a whole-tree `git archive` carries CLAUDE.md,
# .gitignore, …): never promoted, gone with the staging dir. 2026-10-06: `rmdir` here
# killed the run between promote and build — the node sat with new src/ and the old image.
rm -rf src.new

say "4b. build louise-rag:local from the promoted tree"
docker build -t louise-rag:local "$ROOT/src"
echo "$COMMIT" > "$ROOT/src/.deployed-commit"   # what 15f's script wrote; DEPLOYMENT.md reads it
NEW_ID=$(docker inspect louise-rag:local --format '{{.Id}}')
echo "louise-rag:local = $NEW_ID"

# ------------------------------------------------------------- 5. restart
say "5. restart the app (stop, then start; both are async jobs)"
# WAIT FOR THE STOP JOB ITSELF, not for a state poll with a short budget.
# 2026-10-07: the poll gave up after 120 s while the stop was still running
# (the worker finishes its unit on SIGTERM, and that day it could not be
# killed at all), `app.start` was called on top of it, and the node was
# left with the web container removed and the old worker running. A stop
# that fails is a stop to look at, never one to start over.
STOP_JOB=$(midclt call app.stop louise-rag)
echo "stop job $STOP_JOB"
for i in $(seq 1 180); do
  js=$(midclt call core.get_jobs "[[\"id\",\"=\",$STOP_JOB]]" | python3 -c "import sys,json; j=json.load(sys.stdin)[0]; print(j['state'], (j.get('error') or '').replace(chr(10),' ')[:300])")
  case "$js" in SUCCESS*|FAILED*|ABORTED*) break;; esac
  sleep 5
done
echo "stop job: $js"
case "$js" in SUCCESS*) ;; *) echo "the stop did not succeed; NOT starting on top of it — see /var/log/app_lifecycle.log"; exit 1;; esac
s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
echo "state after stop: $s"
midclt call app.start louise-rag >/dev/null
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
echo
echo "DEPLOYED $COMMIT. src.old/ and louise-rag:pre-$COMMIT are the way back;"
echo "remove them when satisfied. Watch the worker's first minutes:"
echo "  docker logs --since 5m ix-louise-rag-worker-1 | grep -i -E 'calibration|validation|re-check|error' | head"
echo "(each kept validation is re-embedded once at start — a few seconds per run)."

# ------------------------------------------------------------- BACKOUT
# Anything wrong after phase 4, from the node's shell:
#   cd /mnt/ssd4/apps/louise-rag
#   midclt call app.stop louise-rag          # poll app.query for STOPPED
#   for d in app tests tools docs calibration; do rm -rf src/$d; mv src.old/$d src/; done
#   for f in requirements.txt Dockerfile platform.toml.example; do rm -f src/$f; mv src.old/$f src/; done
#   rm -rf src/deploy && mv src.old/deploy src/deploy
#   docker tag louise-rag:pre-<COMMIT> louise-rag:local
#   midclt call app.start louise-rag         # poll app.query for RUNNING
# The app definition (env, volumes) was not touched by this deploy.
