#!/bin/bash
# at17.sh — 17:00 of 2026-10-19: contribute stops (the quiet hours) and the
# operator's upload rule takes effect, every day from now on: 20 % of the
# 100 Mbit upload from 07:00 to 23:00, the whole of it at night — `[budget]`
# (07 §7.8), its schedule read in the node's time, so the containers get
# TZ=Europe/Rome (they ran in UTC). One app stop/start.
set -u
T=/mnt/ssd4/apps/louise-rag/config/platform.toml
exec >>/root/at17.log 2>&1
while [ "$(date +%H%M)" -lt 1700 ]; do sleep 30; done
echo "== $(date '+%F %T')"
/root/louise-scheduled/contribute.sh stop gutenberg
if ! grep -q '^\[budget\]' "$T"; then
  cp -p "$T" "$T.bak-$(date +%Y%m%d-%H%M)-budget"
  printf '\n# the operator, 2026-10-19: 20 %% of the 100 Mbit upload from 07:00 to 23:00 keeps the line\n# reactive; the whole of it at night. Read in the node'"'"'s time (TZ=Europe/Rome in the app).\n[budget]\nup_max = "100Mbit"\nschedule = "23:00-07:00 full; else 20%%"\n' >> "$T"
fi
sed -n '/^\[budget\]/,/^schedule/p' "$T"
job() { midclt call core.get_jobs "[[\"id\",\"=\",$1]]" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])"; }
J=$(midclt call app.stop louise-rag); for i in $(seq 1 180); do s=$(job $J); case $s in SUCCESS|FAILED|ABORTED) break;; esac; sleep 5; done
echo "stop: $s"; [ "$s" = SUCCESS ] || exit 1
python3 /root/set_timezone.py Europe/Rome
s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
[ "$s" = RUNNING ] || midclt call app.start louise-rag >/dev/null
for i in $(seq 1 90); do s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])"); [ "$s" = RUNNING ] && break; sleep 2; done
echo "state: $s"
sleep 120
for c in ix-louise-rag-web-1 ix-louise-rag-worker-1; do echo "$c TZ=$(docker exec $c printenv TZ) $(docker exec $c date '+%H:%M %Z')"; done
docker exec ix-louise-rag-worker-1 python -c "
from app.transfer.manager import get_manager, Direction
m = get_manager(); m.maybe_refresh(); print('UP ceiling now (B/s):', m.bucket(Direction.UP).core.rate)"
