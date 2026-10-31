#!/bin/bash
# stage-p5-embedder-20261031.sh — candidate (iii) for gutenberg's encoder
# (the architecture session, 2026-10-31): qwen3-embedding-4b with flash
# attention OFF and the F16 KV cache on the llama.cpp engine that carries
# P1–P5 (the tar MADLAD runs on, .louise-llama-cpp-sycl_f16-p5.tar, gated
# green for MADLAD). FA off on the shared engine was bit-exact and WRONG (gate
# cosine 0.13: P5's bug, the KQ product over the F16 K cache); P5 is that
# bug's fix, not its workaround. Nothing live moves: a candidate model name only.
#
# Two backend directories, each named so that the wrapper reads its vendor
# (determinism_lacks: a llama backend whose name holds no "sycl" is refused on
# a card, and MADLAD's install is named vega-llama-cpp-t5):
#   - louise-sycl-f16-llama-cpp-p5: the P5 tar, installed as LocalAI installs
#     any backend (install-external, ocifile://);
#   - vega-llama-cpp-p5: the vega-llama-cpp package copied (RULE 1, named:
#     the package is not part of a run's identity, as for
#     vega-louise-python-20261030), its vega.json naming the P5 backend, its
#     metadata.json its own name; modes and ACLs as the source's
#     (backend_files.sh). A hand-made directory loads only after LocalAI
#     restarts (rollout-111): the restart is the last step, and every model is
#     reloaded on demand afterwards (the worker defers meanwhile).
#   - the candidate model vega-qwen3-embedding-4b-p5: the live YAML but for
#     its name, its backend and flash_attention "off".
#   bash /root/stage-p5-embedder-20261031.sh 2>&1 | tee /root/stage-p5-embedder-20261031.log
set -euo pipefail
. /root/backend_files.sh

BK=/mnt/ssd5/apps/localai/backends
M=/mnt/ssd5/apps/localai/models
TAR=.louise-llama-cpp-sycl_f16-p5.tar
REAL=louise-sycl-f16-llama-cpp-p5
PKG=vega-llama-cpp-p5
Y=$M/vega-qwen3-embedding-4b.yaml
CAND=$M/vega-qwen3-embedding-4b-p5.yaml
LOCALAI=http://localhost:30286
SAVED=/root/stage-p5-20261031
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
mkdir -p $SAVED

say "0. the P5 tar is MADLAD's (vega-llama-cpp-t5 was installed from it); nothing of these names yet"
grep -q "\"uri\": \"ocifile:///backends/$TAR\"" $BK/vega-llama-cpp-t5/metadata.json
test -s $BK/$TAR
test ! -e $BK/$PKG || { echo "$BK/$PKG exists"; exit 1; }
test ! -e $CAND || { echo "$CAND exists"; exit 1; }

say "1. $REAL: the P5 tar installed under a name the wrapper reads"
if [ ! -x $BK/$REAL/run.sh ]; then
  curl -sf -X POST "$LOCALAI/api/backends/install-external" -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$TAR\",\"name\":\"$REAL\"}"; echo
  for _ in $(seq 120); do test -x $BK/$REAL/run.sh && break; sleep 5; done
fi
test -x $BK/$REAL/run.sh || { echo "LocalAI did not install $REAL in 10 min"; exit 1; }
cat $BK/$REAL/metadata.json; echo
for f in llama-cpp-fallback llama-cpp-grpc llama-cpp-rpc-server run.sh; do
  cmp $BK/vega-llama-cpp-t5/$f $BK/$REAL/$f
done
echo "the same binaries as MADLAD's"

say "2. $PKG: the vega-llama-cpp package copied, modes and ACLs as the source's"
# files only: a directory keeps its inheriting ACE (setfacl -b refuses to
# strip one), and a tree's digest counts files' modes, not directories'
(cd $BK/vega-llama-cpp && find . -type f ! -path './run/*' | sed 's|^\./||' | sort) > $SAVED/paths
(cd $BK/vega-llama-cpp && bf_record . $(cat $SAVED/paths)) > $SAVED/$PKG.modes
mkdir $BK/$PKG
bf_copy $BK/vega-llama-cpp $BK/$PKG
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

say "3. the candidate model: the live YAML but for name, backend and flash attention"
sed -e 's/^name: vega-qwen3-embedding-4b$/name: vega-qwen3-embedding-4b-p5/' \
    -e "s/^backend: vega-llama-cpp$/backend: $PKG/" \
    -e 's/^flash_attention: "on"$/flash_attention: "off"/' $Y > $CAND
chown 568:0 $CAND; chmod 644 $CAND
diff $Y $CAND || true

say "4. LocalAI restarted, so the hand-made package loads"
docker restart ix-localai-localai-1 >/dev/null
for _ in $(seq 60); do curl -sf -m 5 "$LOCALAI/readyz" >/dev/null 2>&1 && break; sleep 3; done
curl -s "$LOCALAI/backends" | python3 -c "import sys,json; n=[b.get('name') or b.get('Name') for b in json.load(sys.stdin)]; print([x for x in n if x and 'p5' in x])"
echo "staged: model vega-qwen3-embedding-4b-p5 on $PKG -> $REAL"

# BACKOUT: rm -f $CAND; rm -rf $BK/$PKG; POST /api/backends/delete/$REAL (or
#   /backends/delete/$REAL); docker restart ix-localai-localai-1.
