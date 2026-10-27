#!/bin/bash
# rollout-p2-20261027.sh — OUR llama.cpp backend rebuilt on LocalAI v4.12.1
# (deploy/localai/patches, base ec92740: P2 rebased, llama.cpp a868c3e ->
# c0fd786), replacing louise-sycl-f16-llama-cpp IN PLACE under the same name:
# the vega- packages in front of it (vega-llama-cpp, vega-llama-cpp-gen) are
# not rebuilt, their vega.json already names it. Every SYCL model on it is a
# new run: measured bit-identical (invariant 12), then gated once per
# collection; RULE 1: nothing re-embedded. Chat, translate and rerank models
# (vega-llama-cpp-gen) move with it and are smoke-tested by Ask afterwards.
# Follows rollout-p2.sh (2026-10-18) with today's paths.
#
# Prepared on the dev PC: sh deploy/localai/llama-cpp/build.sh sycl_f16 <out>,
# the tar copied to /root. Run on the node:
#   bash /root/rollout-p2-20261027.sh 2>&1 | tee /root/rollout-p2-20261027.log
# Resumable: an install from this tar is kept. Stops at the first failure.
# BACKOUT at the end.
set -euo pipefail

BK=/mnt/ssd5/apps/localai/backends
TAR=$BK/.louise-llama-cpp-sycl_f16.tar                 # dot-named: LocalAI lists no such backend
SRC_TAR=$(ls /root/louise-llama-cpp-sycl_f16-*.tar | tail -1)
NAME=louise-sycl-f16-llama-cpp
SAVED=/mnt/ssd5/apps/localai/backends-bak-20261027     # outside the backends path
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
SYCL="vega-bge-m3 vega-granite vega-medcpt-query vega-qwen3-embedding-4b"
GEN="louise-chat-qwen3-8b index-translate-9b louise-rerank-bge louise-rerank-bge-strict louise-rerank-lamar"

say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
mode() { $L node show | python3 -c "import sys,json; print(json.load(sys.stdin)['mode'])"; }
paused_collections() { $L node show | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)['overrides'].get('paused_collections') or []))"; }

say "0. the tar, and the backend it replaces saved"
test -s "$SRC_TAR" || { echo "no /root/louise-llama-cpp-sycl_f16-*.tar"; exit 1; }
echo "new: $(basename "$SRC_TAR") $(sha256sum "$SRC_TAR" | cut -c1-16)…"
INSTALLED=
if cmp -s "$SRC_TAR" "$TAR"; then
  INSTALLED=1; echo "$NAME installed already from this tar"
else
  test -e "$SAVED/$NAME" || { mkdir -p "$SAVED" && cp -a "$BK/$NAME" "$SAVED/" && cp -a "$TAR" "$SAVED/"; }
  echo "saved: $(du -sh "$SAVED" | cut -f1) in $SAVED"
fi
MODE=$(mode); BEFORE_PAUSED=$(paused_collections)
echo "mode $MODE; paused collections: ${BEFORE_PAUSED:-none}"
case $MODE in running|paused) ;; *) echo "mode $MODE: run this from running or paused"; exit 1 ;; esac

say "1. the node paused while the backend changes"
$L node pause

say "2. the models on $NAME unloaded"
for m in $SYCL $GEN; do
  printf "%s: " "$m"
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}" || true
  echo
done

say "3. $NAME replaced, as LocalAI installs any backend (From URL, ocifile://)"
if [ -z "$INSTALLED" ]; then
  curl -sf -X POST "$LOCALAI/backends/delete/$NAME"; echo
  for _ in $(seq 60); do test -e "$BK/$NAME" || break; sleep 2; done
  test ! -e "$BK/$NAME" || { echo "LocalAI did not delete $NAME"; exit 1; }
  cp "$SRC_TAR" "$TAR"
  curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$(basename "$TAR")\",\"name\":\"$NAME\"}"; echo
fi
for _ in $(seq 120); do test -x "$BK/$NAME/run.sh" && break; sleep 5; done
test -x "$BK/$NAME/run.sh" || { echo "LocalAI did not install $NAME in 10 min"; exit 1; }
cat "$BK/$NAME/metadata.json"; echo
curl -s -X POST "$LOCALAI/models/reload"; echo

say "4. invariant 12: each new run bit-identical over fresh loads and orders"
for m in $SYCL; do
  docker exec $W python -m tools.vega_repro_check --url http://192.168.178.10:30286 --model "$m" --n 16 --window 511
done
docker exec $W python -m tools.vega_repro_check --url http://192.168.178.10:30286 --model vega-bge-m3 --n 16 --window 1024
docker exec $W python -m tools.vega_cut_scan --url http://192.168.178.10:30286 --model vega-bge-m3 --window 1024

say "5. the node back to its mode, then the gate on every collection that holds vectors"
[ "$MODE" = running ] && $L node resume
check() {   # retried while the worker still holds the collection
  local out="" rc=0
  for _ in $(seq 1 40); do
    out=$($L hold embedder check "$1" 2>&1) && rc=0 || rc=$?
    echo "$out" | grep -q "held by the node" || break
    sleep 15
  done
  echo "$out"
  return $rc
}
for c in gutenberg pubmed wikipedia wikipedia-v8 wikidata normattiva; do
  echo "--- $c"
  was_paused=; case " $BEFORE_PAUSED " in *" $c "*) was_paused=1 ;; esac
  [ -n "$was_paused" ] || $L node pause "$c" >/dev/null
  # a paused collection another writer holds (wikipedia-v8's records build,
  # 2026-10-27) is gated by its next embed, not here
  if [ -n "$was_paused" ] && $L hold embedder check "$c" 2>&1 | grep -q "is held by"; then
    echo "$c: held by another writer and paused; gated when it next embeds"; continue
  fi
  check "$c" || { echo "THE GATE FAILED on $c: it stays paused; BACKOUT below"; exit 1; }
  [ -n "$was_paused" ] || $L node resume "$c" >/dev/null
done
echo "mode $(mode); paused collections: $(paused_collections) (before: ${BEFORE_PAUSED:-none})"
echo "rolled out: $NAME from $(basename "$SRC_TAR")"

# BACKOUT (the runs before this rollout come back, gated already):
#   node pause; shutdown the models above; POST $LOCALAI/backends/delete/louise-sycl-f16-llama-cpp
#   cp -a /mnt/ssd5/apps/localai/backends-bak-20261027/.louise-llama-cpp-sycl_f16.tar $BK/
#   install-external as in step 3 (or cp -a the saved directory back), models/reload, node resume
