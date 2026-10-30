#!/bin/bash
# rollout-110-20261030.sh — #110 (d33de91): the VeGa wrapper sends a text whose
# UTF-8 bytes keep it under the window whole, without asking TokenizeString
# (docs/notes/char-cut-truncation.md). Only the packages whose wrapper cuts by
# the tokenizer take anything from it: vega-llama-cpp and vega-llama-cpp-cpu
# (vega.json `cuts: tokenizer`); the context_size packages (louise-python,
# llama-cpp-gen) are not touched and their runs stay.
#
# RULE 1, the shortcut, named: a rebuild by build_vega_package.sh would
# reinstall every file; the files a rebuild at this commit changes against
# the installed REVISION (8cbb8ad) are vega_wrapper.py and app/hold/codex.py
# (a cached collection_id the wrapper does not use) and REVISION itself — so
# those three are replaced, compiled with the package's own python as the
# build compiles everything, and the rest is carried byte for byte. Each
# model on these packages is a new run: measured bit-identical (invariant 12),
# then gated once per collection that holds its vectors.
#
# Prepared on the dev PC:
#   git archive <commit> deploy/vega/vega_wrapper.py app/hold/codex.py > /tmp/vega-110.tar
#   + REVISION, copied to the node as /root/vega-110.tar
# Run on the node:
#   bash /root/rollout-110-20261030.sh 2>&1 | tee /root/rollout-110-20261030.log
# Stops at the first failure. BACKOUT at the end.
set -euo pipefail

BK=/mnt/ssd5/apps/localai/backends
PKGS="vega-llama-cpp vega-llama-cpp-cpu"
FILES="vega_wrapper.py app/hold/codex.py REVISION"
SRC=/root/vega-110.tar
SAVED=/mnt/ssd5/apps/localai/backends-bak-20261030-110     # outside the backends path
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
MODELS="vega-bge-m3 vega-granite vega-medcpt-article vega-medcpt-query vega-qwen3-embedding-4b vega-bge-m3-cpu"
GATED="gutenberg wikipedia pubmed"        # the collections whose document or query encoder runs here

say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
mode() { $L node show | python3 -c "import sys,json; print(json.load(sys.stdin)['mode'])"; }
paused_collections() { $L node show | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)['overrides'].get('paused_collections') or []))"; }

say "0. the new files, and the ones they replace saved"
test -s "$SRC" || { echo "no $SRC"; exit 1; }
STAGE=$(mktemp -d)
tar -xf "$SRC" -C "$STAGE"
for f in $FILES; do test -s "$STAGE/$f" || { echo "the tar lacks $f"; exit 1; }; done
echo "revision $(cat "$STAGE/REVISION")"
for p in $PKGS; do
  test -d "$SAVED/$p" || {
    mkdir -p "$SAVED/$p/app/hold" "$SAVED/$p/__pycache__" "$SAVED/$p/app/hold/__pycache__"
    for f in $FILES; do cp -a "$BK/$p/$f" "$SAVED/$p/$f"; done
    cp -a "$BK/$p"/__pycache__/vega_wrapper.*.pyc "$SAVED/$p/__pycache__/" 2>/dev/null || true
    cp -a "$BK/$p"/app/hold/__pycache__/codex.*.pyc "$SAVED/$p/app/hold/__pycache__/" 2>/dev/null || true
  }
  echo "$p: was $(cat "$SAVED/$p/REVISION" | cut -c1-10), wrapper $(sha256sum "$SAVED/$p/vega_wrapper.py" | cut -c1-12); saved"
done
MODE=$(mode); BEFORE_PAUSED=$(paused_collections)
echo "mode $MODE; paused collections: ${BEFORE_PAUSED:-none}"
case $MODE in running|paused) ;; *) echo "mode $MODE: run this from running or paused"; exit 1 ;; esac

say "1. the node paused while the wrapper changes"
$L node pause

say "2. the models on these packages unloaded"
for m in $MODELS; do
  printf "%s: " "$m"
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}" || true
  echo
done

say "3. the three files replaced, compiled with each package's own python"
for p in $PKGS; do
  for f in $FILES; do install -m 0644 -o 568 -g 0 "$STAGE/$f" "$BK/$p/$f"; done
  (cd "$BK/$p" && ./venv/bin/python -m compileall -q vega_wrapper.py app/hold/codex.py >/dev/null)
  chown -R 568:0 "$BK/$p/__pycache__" "$BK/$p/app/hold/__pycache__"
  (cd "$BK/$p" && ./venv/bin/python -c "import vega_wrapper; print('$p: imports ok')")
  echo "$p: now $(cut -c1-10 "$BK/$p/REVISION"), wrapper $(sha256sum "$BK/$p/vega_wrapper.py" | cut -c1-12)"
done
rm -rf "$STAGE"

say "4. invariant 12: each new run bit-identical over fresh loads and orders"
for m in vega-granite vega-medcpt-query vega-qwen3-embedding-4b; do
  docker exec $W python -m tools.vega_repro_check --url http://192.168.178.10:30286 --model "$m" --n 16 --window 511
done
docker exec $W python -m tools.vega_repro_check --url http://192.168.178.10:30286 --model vega-bge-m3 --n 16 --window 1024
docker exec $W python -m tools.vega_cut_scan --url http://192.168.178.10:30286 --model vega-bge-m3 --window 1024

say "5. the node back to its mode, then the gate on every collection these runs serve"
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
for c in $GATED; do
  echo "--- $c"
  was_paused=; case " $BEFORE_PAUSED " in *" $c "*) was_paused=1 ;; esac
  [ -n "$was_paused" ] || $L node pause "$c" >/dev/null
  if [ -n "$was_paused" ] && $L hold embedder check "$c" 2>&1 | grep -q "is held by"; then
    echo "$c: held by another writer and paused; gated when it next embeds"; continue
  fi
  check "$c" || { echo "THE GATE FAILED on $c: it stays paused; BACKOUT below"; exit 1; }
  [ -n "$was_paused" ] || $L node resume "$c" >/dev/null
done
echo "mode $(mode); paused collections: $(paused_collections) (before: ${BEFORE_PAUSED:-none})"
echo "rolled out: #110's wrapper in $PKGS"

# BACKOUT (the runs before this rollout come back, gated already):
#   node pause; shutdown the models above; for p in vega-llama-cpp vega-llama-cpp-cpu:
#     cp -a /mnt/ssd5/apps/localai/backends-bak-20261030-110/$p/. /mnt/ssd5/apps/localai/backends/$p/
#   node resume
