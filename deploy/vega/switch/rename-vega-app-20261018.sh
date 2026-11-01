#!/bin/bash
# rename-vega-app-20261018.sh — the `louise-rage` app becomes `louise-vega`
# (operator, 2026-10-18: no the model pool nomenclature left). TrueNAS cannot rename an
# app, and we never delete one: the old app is STOPPED, a new one is created
# with the SAME compose — image vega:bb9bf61, port 30439, the receipts
# socket's and the state's host paths — and checked end to end. The operator
# deletes the old app afterwards (the app only: its volumes are host paths the
# new app uses).
#   bash /root/rename-vega-app-20261018.sh 2>&1 | tee /root/rename-vega-app-20261018.log
set -euo pipefail
OLD=louise-rage
NEW=louise-vega
CFG=/root/louise-rage-app-config-20261018.json
W=ix-louise-rag-worker-1

say() { printf '\n=== %s\n' "$*"; }
app_state() {
  midclt call app.query "[[\"name\",\"=\",\"$1\"]]" |
    python3 -c "import sys,json; a=json.load(sys.stdin); print(a[0]['state'] if a else 'ABSENT')"
}
wait_job() {   # a TrueNAS job id: its end, or the reason it failed
  local js=""
  for _ in $(seq 1 180); do
    js=$(midclt call core.get_jobs "[[\"id\",\"=\",$1]]" | python3 -c "import sys,json; j=json.load(sys.stdin)[0]; print(j['state'], (j.get('error') or '').replace(chr(10),' ')[:300])")
    case "$js" in SUCCESS*) echo "job $1: $js"; return 0;; FAILED*|ABORTED*) echo "job $1: $js"; return 1;; esac
    sleep 5
  done
  echo "job $1: still $js after 15 minutes"; return 1
}

say "0. the new name is free, and the old app's compose is the one this expects"
[ "$(app_state "$NEW")" = ABSENT ] || { echo "$NEW exists already: not touching anything"; exit 1; }
test -s "$CFG" || midclt call app.config "$OLD" > "$CFG"
python3 - "$CFG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
assert list(c["services"]) == ["vega"], c["services"].keys()
s = c["services"]["vega"]
assert s["image"] == "vega:bb9bf61" and s["ports"] == ["30439:8090"], s
assert s["command"] == ["python", "-m", "app.vega"], s["command"]
print("compose:", s["image"], s["ports"], s["volumes"])
PY

say "1. $OLD stopped (kept; the operator deletes it)"
wait_job "$(midclt call app.stop "$OLD")"
echo "$OLD: $(app_state "$OLD")"

say "2. $NEW created with the same compose"
payload=$(python3 -c "import json,sys; print(json.dumps({'app_name': '$NEW', 'custom_app': True, 'custom_compose_config': json.load(open(sys.argv[1]))}))" "$CFG")
wait_job "$(midclt call app.create "$payload")"
s=""
for _ in $(seq 1 60); do s=$(app_state "$NEW"); [ "$s" = RUNNING ] && break; sleep 2; done
echo "$NEW: $s"
[ "$s" = RUNNING ]

say "3. it answers, and a receipt goes through it end to end"
for _ in $(seq 1 30); do curl -sf localhost:30439/health && break; sleep 2; done; echo
docker exec "$W" python -c "
from app import collections as c, embeddings
r = c.recipe_for(c.by_name('wikipedia'))
runs = set()
v = embeddings.embed_calibration(r, ['louise'], runs=runs)
print('a vector of', len(v[0]), 'from run', sorted(runs), 'with its receipt')" 2>&1 | grep -v "warn:config"
curl -s localhost:30439/health; echo
echo
echo "RENAMED: $NEW serves VeGa's receipts. Delete $OLD from the Apps page when satisfied —"
echo "the app only; its host paths (/mnt/ssd5/apps/louise-rage/state/vega, the socket's) are $NEW's."
echo "BACKOUT: midclt call app.stop $NEW; midclt call app.start $OLD"
