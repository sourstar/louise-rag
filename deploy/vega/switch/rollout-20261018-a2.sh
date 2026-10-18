#!/bin/bash
# rollout-20261018-a2.sh — phase A's last two steps, as they had to be run
# (2026-10-18): `hold embedder check` is a writer, and the worker holds each
# collection's lock (07 §7.7) while the NODE is paused; a COLLECTION's pause
# makes it let go within a round (normattiva's re-creation did the same).
# So the node goes back to its mode first, then each collection is paused,
# checked once its lock is free, and resumed. Louise is still the image
# before a2c89e8, so the gate measures the new runs on the inputs the
# references were made from. Phase B only after every check below passed.
#   bash /root/rollout-20261018-a2.sh 2>&1 | tee /root/rollout-20261018-a2.log
set -uo pipefail
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"

say() { printf '\n=== %s\n' "$*"; }
run() {   # a writer, retried while the worker still holds the collection
  local out=""
  for _ in $(seq 1 40); do
    out=$($L "$@" 2>&1)
    echo "$out" | grep -q "held by the node" || break
    sleep 15
  done
  echo "$out"
}

say "8 (first). the node back to its mode"
$L node resume
$L node show | python3 -c "import sys,json; print('mode', json.load(sys.stdin)['mode'])"

say "7. the calibration gate on every new run, one collection at a time"
for c in gutenberg normattiva pubmed wikipedia; do
  echo "--- $c"
  $L node pause "$c" >/dev/null
  run hold embedder check "$c"
  $L node resume "$c" >/dev/null
done
$L node show | python3 -c "import sys,json; d=json.load(sys.stdin); print('mode', d['mode'], 'paused', d['overrides'].get('paused_collections'))"
echo "phase A done if every check above passed; then phase B"
