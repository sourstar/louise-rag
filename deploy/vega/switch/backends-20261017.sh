#!/bin/bash
# The five models onto louise-python (docs/notes/backend-consolidation.md), by
# the operator: in place, backed up, only the backend line and one task: each.
#   ssh root@<node> bash -s < deploy/vega/switch/backends-20261017.sh
set -eu
cd /mnt/ssd5/apps/localai/models
B=.bak-20261017-backends
edit() {  # file  old-backend-line  new-backend-line  anchor-option-line  task-line
  f=$1
  cp -p "$f" "$f$B"
  grep -qxF -- "$2" "$f" && grep -qxF -- "$4" "$f" || { echo "REFUSED $f: a line is not as expected"; exit 2; }
  test "$(grep -cxF -- "$4" "$f")" = 1 || { echo "REFUSED $f: the anchor is not unique"; exit 2; }
  python3 - "$f" "$2" "$3" "$4" "$5" <<'PY'
import sys
f, old, new, anchor, task = sys.argv[1:]
lines = open(f).read().split("\n")
out = []
for l in lines:
    if l == old:
        out.append(new)
    elif l == anchor:
        out += [task, l]
    else:
        out.append(l)
open(f, "w").write("\n".join(out))
PY
  n=$(diff "$f$B" "$f" | grep -c '^[<>]' || true)
  test "$n" = 3 || { echo "REFUSED $f: $n changed lines"; cp -p "$f$B" "$f"; exit 2; }
  echo "ok $f"; diff "$f$B" "$f" || true
}
edit vega-dar-legal-it.yaml "backend: vega-louise-dar-legal-it" "backend: vega-louise-python" \
     "    - device:xpu" "    - task:dar-legal-it"
edit vega-medcpt-article-ov.yaml "backend: vega-louise-medcpt-article-ov" "backend: vega-louise-python" \
     "    - device:xpu" "    - task:medcpt-article"
edit vega-medcpt-article-ov-cpu.yaml '  "backend": "vega-louise-medcpt-article-ov",' '  "backend": "vega-louise-python",' \
     '    "device:cpu",' '    "task:medcpt-article",'
edit louise-translate-madlad.yaml '  "backend": "louise-madlad",' '  "backend": "louise-python",' \
     '    "device:xpu",' '    "task:madlad",'
edit louise-rerank-medcpt.yaml '  "backend": "louise-medcpt-rerank",' '  "backend": "louise-python",' \
     '    "device:xpu"' '    "task:medcpt-rerank",'
curl -s -X POST localhost:30286/models/reload; echo
for m in vega-dar-legal-it vega-medcpt-article-ov vega-medcpt-article-ov-cpu louise-translate-madlad louise-rerank-medcpt; do
  printf "%s: " $m; curl -s -X POST localhost:30286/backend/shutdown -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
# the calibration gate on the new runs (RULE 1: no re-embedding). Until it
# passes the worker DEFERS these collections' writes (run_check.check_write)
# and gates the run itself at its next embed_allowed; this does it now.
for c in normattiva pubmed; do
  docker exec ix-louise-rag-worker-1 python -m app.louise hold embedder check $c
done
# undo: for f in *.yaml.bak-20261017-backends; do cp -p "$f" "${f%.bak-20261017-backends}"; done; reload; shutdown
