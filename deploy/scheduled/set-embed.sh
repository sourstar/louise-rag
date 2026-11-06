#!/bin/bash
# set-embed.sh <collection> <from> <to> — the operator's quiet hours
# (2026-10-18): no sustained back-to-back embedding between 07:00 and 23:00.
# TrueNAS cron runs it at 07:00 (all -> on-demand) and 23:00 (on-demand -> all).
# In place, backed up once a day, exactly the one line; then the node applies
# the file (07 §7.8 pickup). A file that says anything else is left alone.
set -eu
C=$1 FROM=$2 TO=$3
F=/mnt/ssd4/apps/louise-rag/config/collections/$C.toml
B=$F.bak-quiet-hours-$(date +%Y%m%d-%H%M)
exec >>/root/louise-scheduled/set-embed.log 2>&1
echo "== $(date) $C: $FROM -> $TO"
if grep -qx "embed = \"$TO\"" "$F"; then echo "already $TO"; exit 0; fi
grep -qx "embed = \"$FROM\"" "$F" || { echo "LEFT ALONE: the embed line is not \"$FROM\""; grep -n '^embed' "$F"; exit 2; }
cp -p "$F" "$B"
sed -i "s/^embed = \"$FROM\"\$/embed = \"$TO\"/" "$F"
n=$(diff "$B" "$F" | grep -c '^[<>]' || true)
if [ "$n" != 2 ]; then echo "REFUSED: $n changed lines; restored"; cp -p "$B" "$F"; exit 2; fi
rm -f "$B"
docker exec ix-louise-rag-worker-1 python -m app.louise hold config apply >/dev/null 2>&1 || echo "config apply exit $?"
docker exec ix-louise-rag-worker-1 python -m app.louise hold config show 2>/dev/null | python3 -c '
import json, sys
for c in json.load(sys.stdin)["collections"]:
    if c["name"] == sys.argv[1]:
        print(c["name"], "embed_mode:", c["values"]["embed_mode"]["value"])' "$C"
