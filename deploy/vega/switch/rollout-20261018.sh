#!/bin/bash
# rollout-20261018.sh — the combined rollout, phase A (operator, 2026-10-18):
# CLAUDE.md invariant 12's determinism env and the window cut
# (docs/notes/char-cut-truncation.md) reach the node as ONE new run per model,
# validated once. Phase B, the Louise image, follows it and never precedes
# it: the wrapper before a2c89e8 refuses a tag carrying a window.
#
# Run from the dev PC:
#   sh deploy/vega/stage/stage_src.sh /tmp/vega-stage-src.tgz a2c89e8
#   scp /tmp/vega-stage-src.tgz root@node:/tmp/vega-stage-src.tgz
#   ssh root@node bash -s < deploy/vega/switch/rollout-20261018.sh
#
# Stops at the first failure. BACKOUT at the end.
set -euo pipefail

PY=python@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
BK=/mnt/tank/apps/localai/backends
SAVED=/mnt/tank/apps/localai/backends-bak-20261018      # outside the backends path
MODELS=/mnt/ssd5/apps/localai/models
B=.bak-20261018-determinism
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
STAGE=/tmp/vega-stage
SYCL="vega-bge-m3 vega-granite vega-medcpt-article vega-medcpt-query vega-qwen3-embedding-4b"
ALL="$SYCL vega-dar-legal-it vega-medcpt-article-ov vega-medcpt-article-ov-cpu"

say() { printf '\n=== %s\n' "$*"; }

say "0. stage the source at its commit"
test -s /tmp/vega-stage-src.tgz || { echo "no /tmp/vega-stage-src.tgz: scp it first"; exit 1; }
rm -rf "$STAGE" && mkdir -p "$STAGE/src" "$STAGE/cache"
tar -xzf /tmp/vega-stage-src.tgz -C "$STAGE/src"
echo "revision $(cat "$STAGE/src/REVISION")"
grep -q "def _cut" "$STAGE/src/deploy/vega/vega_wrapper.py"            # the window cut
grep -q "def determinism_env" "$STAGE/src/deploy/vega/vega_wrapper.py" # invariant 12
grep -q 'WINDOW = "window"' "$STAGE/src/app/vega/tag.py"

say "1. the node paused while the backends change (its mode now: running)"
$L node pause
$L node show | python3 -c "import sys,json; print('mode', json.load(sys.stdin)['mode'])"

say "2. LocalAI's own files the packages carry, fetched and checked"
docker run --rm -v "$STAGE/cache:/cache" -v "$STAGE/src:/src:ro" "$PY" sh /src/deploy/vega/stage/fetch.sh

say "3. invariant 12: GGML_SYCL_ENABLE_DNN=0 in every SYCL vega- model's env"
cd "$MODELS"
for m in $SYCL; do
  f=$m.yaml
  test -e "$f$B" || cp -p "$f" "$f$B"
  python3 - "$f$B" "$f" <<'PY'
import sys
src, dst = sys.argv[1:]
text = open(src).read()
if "GGML_SYCL_ENABLE_DNN" in text:
    raise SystemExit(f"REFUSED {dst}: it names the variable already")
json_line = '    "NEO_CACHE_DIR": "/data/louise-cache/neo-cache"\n'
yaml_line = "    NEO_CACHE_DIR: /data/louise-cache/neo-cache\n"
if text.count(json_line) == 1:
    text = text.replace(json_line, json_line.rstrip("\n") + ',\n    "GGML_SYCL_ENABLE_DNN": "0"\n')
elif text.count(yaml_line) == 1:
    text = text.replace(yaml_line, yaml_line + '    GGML_SYCL_ENABLE_DNN: "0"\n')
else:
    raise SystemExit(f"REFUSED {dst}: its env is not as expected")
open(dst, "w").write(text)
PY
  diff "$f$B" "$f" || true
done
# every edited file parses, and its env holds both keys (PyYAML, in the worker)
for m in $SYCL; do
  docker exec -i "$W" python -c "
import sys, yaml
d = yaml.safe_load(sys.stdin.read())
env = d.get('env') or {}
assert env.get('GGML_SYCL_ENABLE_DNN') == '0' and env.get('NEO_CACHE_DIR'), env
print('ok', d['name'], env)" < "$m.yaml"
done

say "4. the models unloaded, so none loads a half-built package"
for m in $ALL; do
  printf "%s: " "$m"
  curl -s -X POST localhost:30286/backend/shutdown -H "Content-Type: application/json" \
       -d "{\"model\":\"$m\"}" || true
  echo
done

say "5. the two wrapper packages saved, then rebuilt at the staged commit"
mkdir -p "$SAVED"
for p in vega-llama-cpp vega-louise-python; do
  test -e "$SAVED/$p" || cp -a "$BK/$p" "$SAVED/$p"
done
docker run --rm -e PKG=vega-llama-cpp -e BACKEND=intel-sycl-f16-llama-cpp \
  -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
  -v "$BK/vega-llama-cpp:/backends/vega-llama-cpp" \
  -v "$STAGE/src:/src:ro" -v "$STAGE/cache:/cache:ro" \
  "$PY" sh /src/deploy/vega/stage/build_vega_package.sh
# the receipts service binds vega-llama-cpp's run/ alone: both packages'
# receipts go through that one socket, as vega-louise-python's did before
docker run --rm -e PKG=vega-louise-python -e BACKEND=louise-python -e CUTS=context_size \
  -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
  -v "$BK/vega-louise-python:/backends/vega-louise-python" \
  -v "$STAGE/src:/src:ro" -v "$STAGE/cache:/cache:ro" \
  "$PY" sh /src/deploy/vega/stage/build_vega_package.sh
for p in vega-llama-cpp vega-louise-python; do
  echo "$p: $(cat "$BK/$p/REVISION") $(cat "$BK/$p/vega.json" | tr -d '\n ')"
done
grep -q '"cuts": "context_size"' "$BK/vega-louise-python/vega.json"
grep -q '"cuts": "tokenizer"' "$BK/vega-llama-cpp/vega.json"
grep -q 'vega-llama-cpp/run/receipts.sock' "$BK/vega-louise-python/vega.json"

say "6. LocalAI reads the models again"
curl -s -X POST localhost:30286/models/reload; echo

say "7. the calibration gate on every new run (RULE 1: nothing re-embedded)"
# Louise is still the image before a2c89e8: it sends the calibration texts as
# the references were made, so the gate measures the new runs, not the cut.
for c in gutenberg normattiva pubmed wikipedia; do
  echo "--- $c"
  $L hold embedder check "$c"
done

say "8. the node back to its mode"
$L node resume
$L node show | python3 -c "import sys,json; print('mode', json.load(sys.stdin)['mode'])"
echo "phase A done; phase B (the Louise image at the same commit) may follow"

# BACKOUT (the runs before this rollout come back):
#   cd /mnt/ssd5/apps/localai/models && for f in *.yaml.bak-20261018-determinism; do
#     cp -p "$f" "${f%.bak-20261018-determinism}"; done
#   # run/ (the receipts socket, bind-mounted by inode) and neo-cache/ stay
#   for p in vega-llama-cpp vega-louise-python; do
#     rsync -a --delete --exclude /run --exclude /neo-cache \
#       /mnt/tank/apps/localai/backends-bak-20261018/$p/ /mnt/tank/apps/localai/backends/$p/; done
#   curl -s -X POST localhost:30286/models/reload   (then shutdown each vega- model)
