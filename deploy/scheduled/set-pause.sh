#!/bin/bash
# set-pause.sh <collection> pause|resume — the operator's heavy-work windows
# (22:30-06:00; 2026-10-19) for a collection whose pipeline is hours of CPU
# (wikidata's first scan of the 103 GB dump, 2026-10-20): TrueNAS cron pauses
# it at 06:00 and resumes it at 22:30. A per-collection pause (07 §7.7): the
# worker finishes its unit and the scan resumes where it stopped.
set -eu
C=$1 ACTION=$2
case "$ACTION" in pause|resume) ;; *) echo "pause|resume"; exit 2;; esac
exec >>/root/louise-scheduled/set-pause.log 2>&1
echo "== $(date) $C: $ACTION"
docker exec ix-louise-rag-worker-1 python -m app.louise node "$ACTION" "$C" 2>&1 | grep -v warn:config | tr -d '\n '
echo
