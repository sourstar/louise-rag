#!/bin/bash
# stage-fanan-20261031.sh — NaN with flash attention on, read-only on TEMPORARY
# models (the architecture session, 2026-10-31). Two cases nobody root-caused:
# louise-rerank-bge gave NaN scores on 2026-10-21 in batches of 40–72, and
# gutenberg's qwen3-4b on the P5 engine gave a NaN vector on a long sweep prefix
# (qwen-p5fa run 5, 2026-10-31). The lead: a fully masked FA tile (-inf − -inf)
# — a packed rerank batch and a causal model's long sequence both have them,
# bge-m3 (bidirectional, one sequence) does not and is clean.
# Nothing live moves: eight model names that nothing in Louise asks for.
#   reranker  louise-rerank-bge-fanan       /root/louise-rerank-bge.yaml.fast-NaN-20261021 but for its name
#             louise-rerank-bge-fanan-cold  + NEO_CACHE_DIR of its own, emptied before every load
#             louise-rerank-bge-fanan-diag0 on the diag build
#             louise-rerank-bge-fanan-diag  on the diag build + GGML_SYCL_CPU_OPS=FLASH_ATTN_EXT
#   qwen3     vega-qwen3-embedding-4b-fanan (-cold, -diag0, -diag): the live YAML (P5, FA off)
#             with flash_attention "on", and the same three variants
# The diag build is localai-bugs.md P4's instrument (03c771b88d03: GGML_SYCL_CPU_OPS
# makes the device decline an op class, so the CPU runs it), installed as LocalAI
# installs any (install-external, ocifile://) under a name the wrapper reads its
# vendor in. Its two packages are copies of the ones the models run on (#117's
# wrapper: run AFTER rollout-116): vega-llama-cpp-gen-diag (context_size cuts)
# and vega-llama-cpp-diag (tokenizer cuts, from vega-llama-cpp-p5). A hand-made
# package loads only after LocalAI restarts: the restart is the last step, and
# it is the second origin of the llama.cpp children's age (the ageing measurement).
#   bash /root/stage-fanan-20261031.sh 2>&1 | tee /root/stage-fanan-20261031.log
set -euo pipefail
. /root/backend_files.sh

BK=/mnt/ssd5/apps/localai/backends
M=/mnt/ssd5/apps/localai/models
SRC_TAR=/root/louise-llama-cpp-sycl_f16-03c771b88d03.tar
TAR=.louise-llama-cpp-sycl_f16-diag.tar
REAL=louise-sycl-f16-llama-cpp-diag
NAN=/root/louise-rerank-bge.yaml.fast-NaN-20261021
QY=$M/vega-qwen3-embedding-4b.yaml
CACHE_HOST=/mnt/.ix-apps/app_mounts/localai/config/louise-cache
LOCALAI=http://localhost:30286
SAVED=/root/stage-fanan-20261031
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
put() { chown 568:0 "$1"; chmod 644 "$1"; }
mkdir -p $SAVED

say "0. after rollout-116 (the packages carry #117's wrapper); nothing of these names yet"
for p in vega-llama-cpp-gen vega-llama-cpp-p5; do
  grep -q '^WRAPPER_ENTRY = "run.sh"$' $BK/$p/vega_wrapper.py || { echo "$p: run rollout-116 first"; exit 1; }
done
test -s $SRC_TAR && test -s $NAN
grep -q '^name: louise-rerank-bge$' $NAN && grep -q '^flash_attention: "on"$' $NAN
grep -q '^backend: vega-llama-cpp-p5$' $QY && grep -q '^flash_attention: "off"$' $QY
for d in vega-llama-cpp-gen-diag vega-llama-cpp-diag; do test ! -e $BK/$d || { echo "$BK/$d exists"; exit 1; }; done
for m in louise-rerank-bge vega-qwen3-embedding-4b; do
  for v in fanan fanan-cold fanan-diag0 fanan-diag; do test ! -e $M/$m-$v.yaml || { echo "$m-$v exists"; exit 1; }; done
done

say "1. $REAL: the diag build installed under a name the wrapper reads"
test -s $BK/$TAR || cp $SRC_TAR $BK/$TAR
if [ ! -x $BK/$REAL/run.sh ]; then
  curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$TAR\",\"name\":\"$REAL\"}"; echo
  for _ in $(seq 120); do test -x $BK/$REAL/run.sh && break; sleep 5; done
fi
test -x $BK/$REAL/run.sh || { echo "LocalAI did not install $REAL in 10 min"; exit 1; }
cat $BK/$REAL/metadata.json; echo

say "2. the two diag packages: copies, modes and ACLs as the source's, naming $REAL"
copy_pkg() {   # SRC PKG
  local src=$1 pkg=$2
  (cd $BK/$src && find . -type f ! -path './run/*' | sed 's|^\./||' | sort) > $SAVED/$pkg.paths
  (cd $BK/$src && bf_record . $(cat $SAVED/$pkg.paths)) > $SAVED/$pkg.modes
  mkdir $BK/$pkg
  bf_copy $BK/$src $BK/$pkg
  python3 - "$BK/$pkg" "$REAL" "$pkg" <<'PY'
import json, os, sys
d, real, pkg = sys.argv[1:4]
for name, change in (("vega.json", {"backend": real}), ("metadata.json", {"name": pkg})):
    p = os.path.join(d, name)
    doc = json.load(open(p))
    doc.update(change)
    open(p, "w").write(json.dumps(doc, indent=2) + "\n")
PY
  chown -R 568:0 $BK/$pkg
  bf_restore $BK/$pkg $SAVED/$pkg.modes
  echo "$pkg: $(tr -d '\n ' < $BK/$pkg/vega.json | cut -c1-120)"
}
copy_pkg vega-llama-cpp-gen vega-llama-cpp-gen-diag
copy_pkg vega-llama-cpp-p5 vega-llama-cpp-diag

say "3. the eight temporary models"
variants() {   # SRC MODEL DIAG_PKG: writes MODEL-fanan(-cold, -diag0, -diag) from SRC, already named MODEL-fanan
  local src=$1 m=$2 dpkg=$3 cold=neo-cache-$2-cold
  mkdir -p $CACHE_HOST/$cold; chown 568:568 $CACHE_HOST/$cold
  cp $src $M/$m-fanan.yaml
  sed -e "s/^name: $m-fanan\$/name: $m-fanan-cold/" \
      -e "s#^    NEO_CACHE_DIR: /data/louise-cache/neo-cache\$#    NEO_CACHE_DIR: /data/louise-cache/$cold#" $src > $M/$m-fanan-cold.yaml
  sed -e "s/^name: $m-fanan\$/name: $m-fanan-diag0/" \
      -e "s/^backend: .*\$/backend: $dpkg/" $src > $M/$m-fanan-diag0.yaml
  sed -e "s/^name: $m-fanan\$/name: $m-fanan-diag/" \
      -e "s/^backend: .*\$/backend: $dpkg/" \
      -e 's/^    GGML_SYCL_ENABLE_DNN: "0"$/    GGML_SYCL_ENABLE_DNN: "0"\n    GGML_SYCL_CPU_OPS: FLASH_ATTN_EXT/' $src > $M/$m-fanan-diag.yaml
  for v in fanan fanan-cold fanan-diag0 fanan-diag; do put $M/$m-$v.yaml; done
  grep -q "^    NEO_CACHE_DIR: /data/louise-cache/$cold\$" $M/$m-fanan-cold.yaml
  grep -q "^backend: $dpkg\$" $M/$m-fanan-diag0.yaml
  grep -q '^    GGML_SYCL_CPU_OPS: FLASH_ATTN_EXT$' $M/$m-fanan-diag.yaml
  for v in fanan-cold fanan-diag0 fanan-diag; do echo "--- $m-$v"; diff $M/$m-fanan.yaml $M/$m-$v.yaml || true; done
}
sed 's/^name: louise-rerank-bge$/name: louise-rerank-bge-fanan/' $NAN > $SAVED/rerank-fanan.yaml
[ "$(diff $NAN $SAVED/rerank-fanan.yaml | grep -c '^[<>]')" -eq 2 ] || { echo "the reranker's fanan differs in more than its name"; exit 1; }
variants $SAVED/rerank-fanan.yaml louise-rerank-bge vega-llama-cpp-gen-diag
sed -e 's/^name: vega-qwen3-embedding-4b$/name: vega-qwen3-embedding-4b-fanan/' \
    -e 's/^flash_attention: "off"$/flash_attention: "on"/' $QY > $SAVED/qwen-fanan.yaml
[ "$(diff $QY $SAVED/qwen-fanan.yaml | grep -c '^[<>]')" -eq 4 ] || { echo "qwen's fanan differs in more than name and FA"; exit 1; }
variants $SAVED/qwen-fanan.yaml vega-qwen3-embedding-4b vega-llama-cpp-diag

say "4. LocalAI restarted, so the hand-made packages load (the second age origin)"
docker restart ix-localai-localai-1 >/dev/null
for _ in $(seq 60); do curl -sf -m 5 "$LOCALAI/readyz" >/dev/null 2>&1 && break; sleep 3; done
echo "LocalAI back $(date +%T)"
echo "staged: louise-rerank-bge-fanan* and vega-qwen3-embedding-4b-fanan* (fanan, -cold, -diag0, -diag)"

# CLEANUP after the study: rm -f $M/louise-rerank-bge-fanan*.yaml $M/vega-qwen3-embedding-4b-fanan*.yaml;
#   rm -rf $BK/vega-llama-cpp-gen-diag $BK/vega-llama-cpp-diag $CACHE_HOST/neo-cache-*-cold;
#   POST /api/backends/delete/$REAL; rm -f $BK/$TAR; models/reload (the packages unload at the next restart).
