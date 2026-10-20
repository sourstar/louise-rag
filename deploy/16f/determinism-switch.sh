#!/usr/bin/env bash
# determinism-switch.sh — every model Louise uses behind VeGa's fail-closed
# wrapper (671359b), the torch tasks on a louise-python tree that switches
# torch to its deterministic algorithms (operator, 2026-10-20: "a backend that
# does not produce bit exact results upon reprocessing the same data cannot be
# used under any circumstances"; "VeGa wrapper MUST ensure determinism for ANY
# backend"). docs/notes/louise-python-rebuild.md is the procedure; this is its
# window for the 20261021a tree.
#   1. the wrapper packages rebuilt with 671359b's wrapper: vega-llama-cpp and
#      vega-louise-python-20261020c in place, vega-louise-python-20261021a new
#   2. louise-python-20261021a installed BESIDE the old tree (ocifile)
#   3. the YAMLs switched, a .bak-20261021-det beside each:
#        torch tasks (dar-legal-it, jina-v3, madlad) -> vega-louise-python-20261021a,
#          with LOUISE_TORCH_DETERMINISTIC=1
#        OpenVINO tasks (medcpt-article-ov, medcpt-rerank) -> the same wrapper,
#          which refuses them (no deterministic mode)
#        louise-rerank-bge, louise-chat-qwen3-8b, index-translate-9b -> vega-llama-cpp
#   4. LocalAI restarted (it reads model YAMLs at startup: vega.md §11)
#   5. every model loaded once: each must load, or be refused, as its contract says
# Run on the node with nothing measuring:  bash deploy/16f/determinism-switch.sh
set -euo pipefail

B=/mnt/tank/apps/localai/backends
M=/mnt/ssd5/apps/localai/models
SRC=/tmp/vega-stage/src-671359b
CACHE=/tmp/vega-stage/cache
PY=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
LOCALAI=http://127.0.0.1:30286
TREE=louise-python-20261021a
TAR=.louise-python-intel-20261021a.tar
SOCK=/backends/vega-llama-cpp/run/receipts.sock
TAG=bak-20261021-det
say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }

say "0. nothing measuring, the sources in place"
! docker ps --format '{{.Names}}' | grep -q -E '^rv-|^cat-capture|^instr-probe' || { echo "a measurement runs"; exit 1; }
grep -q determinism_lacks $SRC/deploy/vega/vega_wrapper.py
test -f $B/$TAR

say "1. the models unloaded"
for m in vega-dar-legal-it vega-jina-v3 vega-jina-v3-query louise-translate-madlad louise-rerank-medcpt \
         vega-medcpt-article-ov louise-rerank-bge louise-chat-qwen3-8b index-translate-9b \
         vega-qwen3-embedding-4b vega-bge-m3 vega-medcpt-query vega-granite; do
  curl -s -m 30 -X POST $LOCALAI/backend/shutdown -H "Content-Type: application/json" -d "{\"model\": \"$m\"}" -o /dev/null || true
done

say "2. the new louise-python tree beside the old"
if [ ! -f $B/$TREE/run.sh ]; then
  curl -sf -X POST $LOCALAI/api/backends/install-external -H "Content-Type: application/json" \
    -d "{\"uri\":\"ocifile:///backends/$TAR\",\"name\":\"$TREE\"}"; echo
  for i in $(seq 1 120); do [ -f $B/$TREE/run.sh ] && break; sleep 5; done
fi
test -f $B/$TREE/run.sh && grep -q determinism_refusal $B/$TREE/backend.py && echo "$TREE: the fail-closed dispatcher"

say "3. the wrapper packages, 671359b's wrapper"
build() {   # PKG BACKEND [CUTS]
  mkdir -p $B/$1
  docker run --rm -e PKG=$1 -e BACKEND=$2 -e SOCKET=$SOCK ${3:+-e CUTS=$3} \
    -v $B/$1:/backends/$1 -v $SRC:/src:ro -v $CACHE:/cache:ro $PY sh /src/deploy/vega/stage/build_vega_package.sh
  grep -q determinism_lacks $B/$1/vega_wrapper.py && echo "$1 -> $2: fail-closed"
}
build vega-llama-cpp louise-sycl-f16-llama-cpp
build vega-louise-python-20261020c louise-python-20261020c context_size
build vega-$TREE $TREE context_size

say "4. the YAMLs, a .$TAG beside each"
python3 - "$M" "$TAG" "vega-$TREE" <<'PY'
import json, re, shutil, sys
M, TAG, PYW = sys.argv[1], sys.argv[2], sys.argv[3]
TORCH = {"LOUISE_TORCH_DETERMINISTIC": "1"}
plan = {"vega-dar-legal-it": (PYW, TORCH), "vega-jina-v3": (PYW, TORCH), "vega-jina-v3-query": (PYW, TORCH),
        "louise-translate-madlad": (PYW, TORCH), "louise-rerank-medcpt": (PYW, {}),
        "vega-medcpt-article-ov": (PYW, {}), "louise-rerank-bge": ("vega-llama-cpp", {}),
        "louise-chat-qwen3-8b": ("vega-llama-cpp", {}), "index-translate-9b": ("vega-llama-cpp", {})}
for name, (backend, env) in plan.items():
    p = f"{M}/{name}.yaml"
    s = open(p, encoding="utf-8").read()
    bak = f"{p}.{TAG}"
    try:
        open(bak, "x", encoding="utf-8").write(s)
    except FileExistsError:
        pass
    body = "\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#"))
    if body.lstrip().startswith("{"):
        s = re.sub(r'("backend"\s*:\s*")[^"]*(")', rf"\g<1>{backend}\g<2>", s, count=1)
        for k, v in env.items():
            if f'"{k}"' not in s:
                s = re.sub(r'("env"\s*:\s*\{)', rf'\1\n    "{k}": "{v}",', s, count=1)
        json.loads("\n".join(l for l in s.splitlines() if not l.lstrip().startswith("#")))
    else:
        s = re.sub(r"^backend:.*$", f"backend: {backend}", s, count=1, flags=re.M)
        for k, v in env.items():
            if k not in s:
                s = re.sub(r"^env:\s*\n", f'env:\n    {k}: "{v}"\n', s, count=1, flags=re.M)
    assert backend in s and all(k in s for k in env), name
    open(p, "w", encoding="utf-8").write(s)
    shutil.copymode(bak, p)
    print(f"{name}: backend {backend}" + (f", env {env}" if env else ""))
PY

say "5. LocalAI restarted: it reads the YAMLs at startup"
docker restart ix-localai-localai-1 >/dev/null
for i in $(seq 1 90); do curl -s -m 5 -o /dev/null -w "%{http_code}" $LOCALAI/readyz | grep -q 200 && break; sleep 5; done

say "6. every model once: loaded, or refused, as its contract says"
python3 - "$LOCALAI" <<'PY'
import json, subprocess, sys, urllib.request
L = sys.argv[1]
def stated(model):
    """An embedder is loaded the way Louise loads it: `hold embedder run`
    states its run through VeGa, or says why it cannot."""
    r = subprocess.run(["docker", "exec", "ix-louise-rag-worker-1", "python", "-m", "app.louise", "hold",
                        "embedder", "run", "--encoder", "http://192.168.178.10:30286", "--api", "vega",
                        "--model", model], capture_output=True, text=True, timeout=900)
    lines = [l for l in (r.stdout + r.stderr).splitlines() if "warn:config" not in l]
    return r.returncode == 0, " ".join(lines)[-200:]
def call(path, body):
    try:
        r = urllib.request.urlopen(urllib.request.Request(L + path, json.dumps(body).encode(),
                                   {"Content-Type": "application/json"}), timeout=600)
        return True, r.read()[:120]
    except Exception as e:   # noqa: BLE001
        return False, str(getattr(e, "read", lambda: b"")()[:200] or e)
want = {
    "vega-qwen3-embedding-4b": ("embed", True), "vega-bge-m3": ("embed", True),
    "vega-medcpt-query": ("embed", True), "vega-granite": ("embed", True),
    "vega-jina-v3": ("embed", True), "vega-jina-v3-query": ("embed", True),
    "vega-dar-legal-it": ("embed", True), "louise-translate-madlad": ("madlad", True),
    "louise-rerank-bge": ("rerank", True), "louise-chat-qwen3-8b": ("chat", True),
    "index-translate-9b": ("chat", True),
    "vega-medcpt-article-ov": ("embed", False), "louise-rerank-medcpt": ("rerank", False)}
bad = 0
for m, (kind, ok_wanted) in want.items():
    if kind == "embed":
        ok, out = stated(m)
    elif kind == "rerank":
        ok, out = call("/v1/rerank", {"model": m, "query": "q", "documents": ["d"], "top_n": 1})
    elif kind == "madlad":
        ok, out = call("/v1/completions", {"model": m, "prompt": json.dumps({"items": [{"text": "ave", "to": "it"}]})})
    else:
        ok, out = call("/v1/chat/completions", {"model": m, "messages": [{"role": "user", "content": "ciao /no_think"}],
                                                "max_tokens": 8, "temperature": 0, "top_k": 1})
    verdict = "as the contract says" if ok == ok_wanted else "WRONG"
    bad += ok != ok_wanted
    print(f"{m:26} {'loaded' if ok else 'refused':8} {verdict}  {out if not ok else ''}"[:240])
sys.exit(1 if bad else 0)
PY
echo "next: the bit-exact measurements (det_check.py, tools/vega_repro_check.py), the gates (hold embedder check)"
