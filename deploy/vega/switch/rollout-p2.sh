#!/bin/bash
# rollout-p2.sh — the node's llama.cpp models onto OUR llama.cpp backend, P2
# (docs/notes/localai-bugs.md B14/P2: it embeds token ids, so a space that cuts
# by tokens is cut exactly), with the wrapper that sends a cut as its tokens.
# One new run per SYCL vega- model, measured bit-identical (invariant 12) and
# gated once; RULE 1: nothing re-embedded.
#
# Prepared on the dev PC (the agent copies the three files to /root):
#   sh deploy/localai/llama-cpp/build.sh sycl_f16 <out>     # louise-llama-cpp-sycl_f16-<d>.tar
#   sh deploy/vega/stage/stage_src.sh vega-stage-src.tgz <commit>
# Run on the node by the operator, present:
#   bash /root/rollout-p2.sh 2>&1 | tee /root/rollout-p2.log
#
# vega-louise-python is NOT rebuilt: its tasks cut at their context_size and
# take nothing from this change, so their runs stay. Stops at the first
# failure. BACKOUT at the end.
set -euo pipefail

PY=python@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
BK=/mnt/tank/apps/localai/backends
TAR=$BK/.louise-llama-cpp-sycl_f16.tar           # dot-named: LocalAI lists no such backend
SRC_TAR=$(ls /root/louise-llama-cpp-sycl_f16-*.tar | tail -1)
SRC_STAGE=/root/vega-stage-src.tgz
NAME=louise-sycl-f16-llama-cpp
SAVED=/mnt/tank/apps/localai/backends-bak-p2      # outside the backends path
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
STAGE=/tmp/vega-stage
LOCALAI=http://localhost:30286
SYCL="vega-bge-m3 vega-granite vega-medcpt-article vega-medcpt-query vega-qwen3-embedding-4b"

say() { printf '\n=== %s\n' "$*"; }

say "0. the staged source and the backend tar"
test -s "$SRC_STAGE" || { echo "no $SRC_STAGE"; exit 1; }
test -s "$SRC_TAR" || { echo "no /root/louise-llama-cpp-sycl_f16-*.tar"; exit 1; }
echo "backend $(basename "$SRC_TAR") $(sha256sum "$SRC_TAR" | cut -c1-16)…"
rm -rf "$STAGE" && mkdir -p "$STAGE/src" "$STAGE/cache"
tar -xzf "$SRC_STAGE" -C "$STAGE/src"
echo "revision $(cat "$STAGE/src/REVISION")"
grep -q "def _probe" "$STAGE/src/deploy/vega/vega_wrapper.py"              # the token path
grep -q "0002-llama-cpp-embed-token-ids.patch" "$STAGE/src/deploy/localai/patches/series"
# RESUMABLE (2026-10-18: the first run installed and rebuilt, then stopped at
# step 7): an install of OUR tar is kept, any other is refused
INSTALLED=
if [ -e "$BK/$NAME" ]; then
  grep -q "\"uri\": \"ocifile:///backends/$(basename "$TAR")\"" "$BK/$NAME/metadata.json" \
    || { echo "$BK/$NAME exists and is not ours: delete it in LocalAI first"; exit 1; }
  cmp -s "$SRC_TAR" "$TAR" || { echo "$TAR is not $SRC_TAR: delete $NAME in LocalAI first"; exit 1; }
  INSTALLED=1
  echo "$NAME installed already, from this tar"
else
  cp "$SRC_TAR" "$TAR"                            # where LocalAI's container sees it
fi

say "1. the node paused while the backends change (its mode now)"
$L node show | python3 -c "import sys,json; print('mode', json.load(sys.stdin)['mode'])"
$L node pause

say "2. LocalAI's own files the package carries, fetched and checked"
docker run --rm -v "$STAGE/cache:/cache" -v "$STAGE/src:/src:ro" "$PY" sh /src/deploy/vega/stage/fetch.sh

say "3. our llama.cpp backend installed as LocalAI installs any (From URL, ocifile://)"
if [ -z "$INSTALLED" ]; then
  curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$(basename "$TAR")\",\"name\":\"$NAME\"}"; echo
fi
for _ in $(seq 120); do test -x "$BK/$NAME/run.sh" && break; sleep 5; done
test -x "$BK/$NAME/run.sh" || { echo "LocalAI did not install $NAME in 10 min"; exit 1; }
cat "$BK/$NAME/metadata.json"; echo

say "4. the SYCL models unloaded, so none loads a half-built package"
for m in $SYCL; do
  printf "%s: " "$m"
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" \
       -d "{\"model\":\"$m\"}" || true
  echo
done

say "5. vega-llama-cpp saved, then rebuilt in front of $NAME at the staged commit"
test -e "$SAVED/vega-llama-cpp" || { mkdir -p "$SAVED" && cp -a "$BK/vega-llama-cpp" "$SAVED/"; }
# built already at this commit in front of $NAME: kept — another build is another
# tree, so another run to gate
if [ "$(cat "$BK/vega-llama-cpp/REVISION" 2>/dev/null)" = "$(cat "$STAGE/src/REVISION")" ] \
   && grep -q "\"backend\": \"$NAME\"" "$BK/vega-llama-cpp/vega.json"; then
  echo "vega-llama-cpp is built already at $(cat "$STAGE/src/REVISION") in front of $NAME"
else
  docker run --rm -e PKG=vega-llama-cpp -e BACKEND="$NAME" \
    -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
    -v "$BK/vega-llama-cpp:/backends/vega-llama-cpp" \
    -v "$STAGE/src:/src:ro" -v "$STAGE/cache:/cache:ro" \
    "$PY" sh /src/deploy/vega/stage/build_vega_package.sh
fi
echo "vega-llama-cpp: $(cat "$BK/vega-llama-cpp/REVISION") $(tr -d '\n ' < "$BK/vega-llama-cpp/vega.json")"
grep -q "\"backend\": \"$NAME\"" "$BK/vega-llama-cpp/vega.json"

say "6. LocalAI reads the models again"
curl -s -X POST "$LOCALAI/models/reload"; echo

say "7. invariant 12: each new run bit-identical over fresh loads and orders"
# a window below every model's context (granite's and MedCPT's are 512): a long
# text sent whole overflows a 512-token model (the first run stopped there),
# and the window takes the token-id path P2 adds
for m in $SYCL; do
  docker run --rm --network host -v "$STAGE/src:/srv:ro" -w /srv -e PYTHONPATH=/srv \
    louise-rag:local python -m tools.vega_repro_check --url "$LOCALAI" --model "$m" --n 16 \
    --window 511
done
# the token path on the model whose context is above the window: bit-identical,
# and the 512 calibration texts all cut exactly (3 were refused before P2)
docker run --rm --network host -v "$STAGE/src:/srv:ro" -w /srv -e PYTHONPATH=/srv \
  louise-rag:local python -m tools.vega_repro_check --url "$LOCALAI" --model vega-bge-m3 \
  --n 16 --window 1024
docker run --rm --network host -v "$STAGE/src:/srv:ro" -w /srv -e PYTHONPATH=/srv \
  louise-rag:local python -m tools.vega_cut_scan --url "$LOCALAI" --model vega-bge-m3 \
  --window 1024

say "8. the node back to its mode, then the gate on every new run, one collection at a time"
# `hold embedder check` is a writer, and the worker holds each collection's lock
# while the NODE is paused (rollout-20261018-a2.sh): a collection's own pause
# lets go within a round. RULE 1: nothing re-embedded.
$L node resume
$L node show | python3 -c "import sys,json; print('mode', json.load(sys.stdin)['mode'])"
check() {   # retried while the worker still holds the collection; its own status
  local out="" rc=0
  for _ in $(seq 1 40); do
    out=$($L hold embedder check "$1" 2>&1) && rc=0 || rc=$?
    echo "$out" | grep -q "held by the node" || break
    sleep 15
  done
  echo "$out"
  return $rc
}
for c in gutenberg normattiva pubmed wikipedia; do
  echo "--- $c"
  $L node pause "$c" >/dev/null
  check "$c" || { echo "THE GATE FAILED on $c: it stays paused; BACKOUT below"; exit 1; }
  $L node resume "$c" >/dev/null
done
$L node show | python3 -c "import sys,json; d=json.load(sys.stdin); print('mode', d['mode'], 'paused', d['overrides'].get('paused_collections'))"
echo "P2 rolled out: the SYCL vega- models run on $NAME"

# BACKOUT (the runs before this rollout come back; $NAME stays installed, unused):
#   rsync -a --delete --exclude /run --exclude /neo-cache \
#     /mnt/tank/apps/localai/backends-bak-p2/vega-llama-cpp/ /mnt/tank/apps/localai/backends/vega-llama-cpp/
#   curl -s -X POST localhost:30286/models/reload   (then shutdown each SYCL vega- model)
