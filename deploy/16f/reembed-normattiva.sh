#!/usr/bin/env bash
# reembed-normattiva.sh — normattiva's vectors made again by DAR-legal-it on
# the deterministic torch tree (operator, 2026-10-21: "Then reembed it"; the
# stored ones came from a run that gave back its own bytes in 1 repeat of 4,
# docs/notes/backend-consolidation.md). After determinism-switch.sh and the
# deploy of 77148be, on the node:
#   1. DAR-legal-it measured bit-exact on normattiva's own texts: 3 fresh loads
#      x 2 shuffled passes, every text alone, as LocalAI hands it to the task
#   2. the calibration gate for the run that answers now (hold embedder check)
#   3. every document marked (hold vectors reembed), the worker restarted so its
#      version walk starts at once
#   4. normattiva resumed inside the quiet-hours window (22:30-06:00); TrueNAS
#      cron pauses it at 06:00 and resumes it at 22:30 until the walk is empty
# Stops at the first failure: nothing is marked before 1 and 2 pass.
#   bash /root/reembed-normattiva.sh 2>&1 | tee /root/reembed-normattiva.log
set -euo pipefail
LOCALAI=http://127.0.0.1:30286
W=ix-louise-rag-worker-1
WEB=ix-louise-rag-web-1
SRC=/mnt/ssd4/apps/louise-rag/src
M=/mnt/ssd5/apps/localai/models
PAUSE=/root/louise-scheduled/set-pause.sh
say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
L() { docker exec "$1" python -m app.louise "${@:2}" 2>&1 | { grep -v warn:config || true; }; }

say "0. in place: the deterministic tree, 77148be, normattiva paused"
grep -q LOUISE_TORCH_DETERMINISTIC $M/vega-dar-legal-it.yaml
grep -q vega-louise-python-20261021a $M/vega-dar-legal-it.yaml
[ "$(cat $SRC/.deployed-commit)" = 77148be ]
docker exec $W python -c "from app import embeddings as e; assert e.REPLACED_VERSION == -1"
L $W node show | python3 -c "import json,sys; d=json.load(sys.stdin); \
assert 'normattiva' in d['overrides']['paused_collections'], 'normattiva must be paused for the gate'"

say "1. bit-exact on normattiva's own texts (24 spread + the 8 longest)"
docker exec -i -w /srv -e PYTHONPATH=/srv $WEB python - > /root/nm-repro-texts.jsonl 2> /root/nm-repro-texts.err <<'PY'
import json, sys
import lancedb
from app import collections as cm, embeddings
c = cm.by_name("normattiva")
r = cm.recipe_for(c)
ds = lancedb.connect(str(c.lance_dir)).open_table(embeddings.TABLE).to_lance()
lens = []
for b in ds.to_batches(columns=["text"], batch_size=65536):
    lens.extend(len(s) for s in b.column(0).to_pylist())
n = len(lens)
longest = sorted(range(n), key=lens.__getitem__)[-8:]
idx = sorted(set([i * n // 24 for i in range(24)] + longest))
texts = ds.take(idx, columns=["text"]).column(0).to_pylist()
for t in sorted(texts, key=len):
    print(json.dumps({"text": t}, ensure_ascii=False))
print(f"window {r.ctx if r.cut == embeddings.TOKEN_CUT else 0}", file=sys.stderr)
PY
WIN=$(grep -o '^window [0-9]*' /root/nm-repro-texts.err | awk '{print $2}')
echo "$(wc -l < /root/nm-repro-texts.jsonl) texts; window ${WIN:-none}"
[ "${WIN:-0}" = 0 ] && WIN=""
docker run --rm --network host -v $SRC:/srv:ro -v /root/nm-repro-texts.jsonl:/t.jsonl:ro \
  -w /srv -e PYTHONPATH=/srv -e PYTHONDONTWRITEBYTECODE=1 louise-rag:local \
  python -m tools.vega_repro_check --url $LOCALAI --model vega-dar-legal-it \
    --n 32 --loads 3 --repeats 2 ${WIN:+--window $WIN} --texts /t.jsonl

say "2. the calibration gate for the run that answers now"
L $W hold embedder check normattiva

say "3. every document marked; the worker restarted so its version walk starts now"
L $W hold vectors reembed normattiva
docker restart -t 300 $W >/dev/null     # SIGTERM: it finishes its unit (07 §7.7)
for i in $(seq 1 60); do docker exec $W true 2>/dev/null && break; sleep 2; done

say "4. quiet hours: cron pauses normattiva at 06:00 and resumes it at 22:30"
for spec in "0 6 pause" "30 22 resume"; do
  set -- $spec
  cmd="/bin/bash $PAUSE normattiva $3"
  if midclt call cronjob.query "[[\"command\",\"=\",\"$cmd\"]]" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin) else 1)"; then
    echo "cron already: $cmd"
  else
    midclt call cronjob.create "{\"command\": \"$cmd\", \"user\": \"root\", \"enabled\": true,
      \"description\": \"normattiva re-embed: quiet hours (2026-10-21)\",
      \"schedule\": {\"minute\": \"$1\", \"hour\": \"$2\", \"dom\": \"*\", \"month\": \"*\", \"dow\": \"*\"}}" \
      | python3 -c "import json,sys; j=json.load(sys.stdin); print('cron', j['id'], j['command'])"
  fi
done
HM=$(date +%H%M)
if [ "$HM" -ge 2230 ] || [ "$HM" -lt 0600 ]; then
  bash $PAUSE normattiva resume; tail -2 /root/louise-scheduled/set-pause.log
else
  echo "outside 22:30-06:00: normattiva stays paused; cron resumes it at 22:30"
fi
echo
echo "watch: docker logs -f $W 2>&1 | grep 'normattiva info:embed'"
echo "check: docker exec -i -w /srv -e PYTHONPATH=/srv $WEB python - < /root/reembed-verify.py"
