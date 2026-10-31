#!/bin/bash
# stage-fanan-20261031.sh — the reranker NaN with flash attention on, read-only on
# TEMPORARY models (the architecture session, 2026-10-31; louise-rerank-bge gave NaN
# scores on 2026-10-21 in batches of 40–72 with FA on, nobody root-caused it).
# Nothing live moves: four model names that nothing in Louise asks for.
#   - louise-rerank-bge-fanan: /root/louise-rerank-bge.yaml.fast-NaN-20261021
#     unchanged but for its name;
#   - louise-rerank-bge-fanan-cold: the same, its NEO_CACHE_DIR an empty directory
#     of its own (the harness empties it before every load: a fresh kernel compile);
#   - louise-rerank-bge-fanan-diag0 / -diag: the same on the diag build
#     (localai-bugs.md P4's instrument, 03c771b88d03: GGML_SYCL_CPU_OPS makes the
#     device decline an op class), without and with GGML_SYCL_CPU_OPS=FLASH_ATTN_EXT.
# The diag backend is installed as LocalAI installs any (install-external,
# ocifile://) under a name the wrapper reads its vendor in; its package,
# vega-llama-cpp-gen-diag, is vega-llama-cpp-gen copied (#117's wrapper: run AFTER
# rollout-116), naming that backend. A hand-made package loads only after LocalAI
# restarts: the restart is the last step, and it is the second origin of the
# llama.cpp children's age (the architecture session's ageing measurement).
#   bash /root/stage-fanan-20261031.sh 2>&1 | tee /root/stage-fanan-20261031.log
set -euo pipefail
. /root/backend_files.sh

BK=/mnt/ssd5/apps/localai/backends
M=/mnt/ssd5/apps/localai/models
SRC_TAR=/root/louise-llama-cpp-sycl_f16-03c771b88d03.tar
TAR=.louise-llama-cpp-sycl_f16-diag.tar
REAL=louise-sycl-f16-llama-cpp-diag
PKG=vega-llama-cpp-gen-diag
NAN=/root/louise-rerank-bge.yaml.fast-NaN-20261021
COLD_HOST=/mnt/.ix-apps/app_mounts/localai/config/louise-cache/neo-cache-fanan-cold
COLD=/data/louise-cache/neo-cache-fanan-cold
LOCALAI=http://localhost:30286
SAVED=/root/stage-fanan-20261031
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
mkdir -p $SAVED

say "0. after rollout-116 (the -gen package carries #117's wrapper); nothing of these names yet"
grep -q '^WRAPPER_ENTRY = "run.sh"$' $BK/vega-llama-cpp-gen/vega_wrapper.py || { echo "run rollout-116 first"; exit 1; }
test -s $SRC_TAR && test -s $NAN
grep -q '^name: louise-rerank-bge$' $NAN && grep -q '^flash_attention: "on"$' $NAN
test ! -e $BK/$PKG || { echo "$BK/$PKG exists"; exit 1; }
for m in fanan fanan-cold fanan-diag0 fanan-diag; do test ! -e $M/louise-rerank-bge-$m.yaml || { echo "louise-rerank-bge-$m exists"; exit 1; }; done

say "1. $REAL: the diag build installed under a name the wrapper reads"
test -s $BK/$TAR || cp $SRC_TAR $BK/$TAR
if [ ! -x $BK/$REAL/run.sh ]; then
  curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$TAR\",\"name\":\"$REAL\"}"; echo
  for _ in $(seq 120); do test -x $BK/$REAL/run.sh && break; sleep 5; done
fi
test -x $BK/$REAL/run.sh || { echo "LocalAI did not install $REAL in 10 min"; exit 1; }
cat $BK/$REAL/metadata.json; echo

say "2. $PKG: vega-llama-cpp-gen copied, modes and ACLs as the source's, naming $REAL"
(cd $BK/vega-llama-cpp-gen && find . -type f ! -path './run/*' | sed 's|^\./||' | sort) > $SAVED/paths
(cd $BK/vega-llama-cpp-gen && bf_record . $(cat $SAVED/paths)) > $SAVED/$PKG.modes
mkdir $BK/$PKG
bf_copy $BK/vega-llama-cpp-gen $BK/$PKG
python3 - "$BK/$PKG" "$REAL" "$PKG" <<'PY'
import json, os, sys
d, real, pkg = sys.argv[1:4]
for name, change in (("vega.json", {"backend": real}), ("metadata.json", {"name": pkg})):
    p = os.path.join(d, name)
    doc = json.load(open(p))
    doc.update(change)
    open(p, "w").write(json.dumps(doc, indent=2) + "\n")
PY
chown -R 568:0 $BK/$PKG
bf_restore $BK/$PKG $SAVED/$PKG.modes
cat $BK/$PKG/vega.json $BK/$PKG/metadata.json

say "3. the four temporary models"
mkdir -p $COLD_HOST; chown 568:568 $COLD_HOST
put() { chown 568:0 "$1"; chmod 644 "$1"; }
sed 's/^name: louise-rerank-bge$/name: louise-rerank-bge-fanan/' $NAN > $M/louise-rerank-bge-fanan.yaml
sed -e 's/^name: louise-rerank-bge$/name: louise-rerank-bge-fanan-cold/' \
    -e "s#^    NEO_CACHE_DIR: /data/louise-cache/neo-cache\$#    NEO_CACHE_DIR: $COLD#" $NAN > $M/louise-rerank-bge-fanan-cold.yaml
sed -e 's/^name: louise-rerank-bge$/name: louise-rerank-bge-fanan-diag0/' \
    -e "s/^backend: vega-llama-cpp-gen\$/backend: $PKG/" $NAN > $M/louise-rerank-bge-fanan-diag0.yaml
sed -e 's/^name: louise-rerank-bge$/name: louise-rerank-bge-fanan-diag/' \
    -e "s/^backend: vega-llama-cpp-gen\$/backend: $PKG/" \
    -e 's/^    GGML_SYCL_ENABLE_DNN: "0"$/    GGML_SYCL_ENABLE_DNN: "0"\n    GGML_SYCL_CPU_OPS: FLASH_ATTN_EXT/' $NAN > $M/louise-rerank-bge-fanan-diag.yaml
for m in fanan fanan-cold fanan-diag0 fanan-diag; do put $M/louise-rerank-bge-$m.yaml; echo "--- $m"; diff $NAN $M/louise-rerank-bge-$m.yaml || true; done
[ "$(diff $NAN $M/louise-rerank-bge-fanan.yaml | grep -c '^[<>]')" -eq 2 ] || { echo "fanan differs in more than its name"; exit 1; }
grep -q "NEO_CACHE_DIR: $COLD\$" $M/louise-rerank-bge-fanan-cold.yaml
grep -q "^backend: $PKG\$" $M/louise-rerank-bge-fanan-diag0.yaml
grep -q '^    GGML_SYCL_CPU_OPS: FLASH_ATTN_EXT$' $M/louise-rerank-bge-fanan-diag.yaml

say "4. LocalAI restarted, so the hand-made package loads (the second age origin)"
docker restart ix-localai-localai-1 >/dev/null
for _ in $(seq 60); do curl -sf -m 5 "$LOCALAI/readyz" >/dev/null 2>&1 && break; sleep 3; done
echo "LocalAI back $(date +%T)"
echo "staged: louise-rerank-bge-fanan, -cold, -diag0, -diag"

# BACKOUT / CLEANUP after the study: rm -f $M/louise-rerank-bge-fanan*.yaml; rm -rf $BK/$PKG $COLD_HOST;
#   POST /api/backends/delete/$REAL; rm -f $BK/$TAR; docker restart ix-localai-localai-1 (or leave it to
#   the next restart: an unused package and backend change nothing).
