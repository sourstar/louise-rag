#!/bin/bash
# contribute.sh start|stop <collection> — rendezvous instead of embedding
# everything (04 §4.5; option 1 of 2026-10-19): the node embeds only the
# parts it leads, through `share contribute`, and only in the quiet-hours
# window (23:00-07:00; the operator's rule of 2026-10-18). TrueNAS cron:
# start at 23:00, stop at 07:00. The worker holds a collection's writer lock
# while it runs it (07 §7.7): start pauses the collection in the node first
# (`node pause`, still seeded), stop resumes it. Stop is SIGTERM: the batch in flight
# finishes, then exit 130 (07 §7.7).
set -eu
ACT=$1 C=$2
W=ix-louise-rag-worker-1
exec >>/root/louise-scheduled/contribute.log 2>&1
echo "== $(date) $ACT $C"
# the container has no pgrep; from the host, the process itself (argv starts
# with python), never the docker exec client that started it
running() { pgrep -f "^python -m app.louise share contribute $C" >/dev/null 2>&1; }
case "$ACT" in
  start)
    if running; then echo "already running"; exit 0; fi
    docker exec "$W" python -m app.louise node pause "$C" || { echo "pause failed"; exit 2; }
    tmux kill-session -t "contribute-$C" 2>/dev/null || true
    # the worker releases the lock at the end of its current unit: retry while it is held (exit 3)
    tmux new -d -s "contribute-$C" \
      "for i in \$(seq 1 60); do docker exec $W python -m app.louise share contribute $C >>/root/louise-scheduled/contribute-$C.log 2>&1; rc=\$?; [ \$rc = 3 ] || break; sleep 10; done; echo \"EXIT \$rc \$(date)\" >>/root/louise-scheduled/contribute-$C.log"
    for i in $(seq 1 60); do running && { echo "started"; exit 0; }; sleep 10; done
    echo "NOT running"; tail -5 /root/louise-scheduled/contribute-$C.log; exit 2
    ;;
  stop)
    if running; then
      pkill -TERM -f "^python -m app.louise share contribute $C" || true
      for i in $(seq 1 180); do running || break; sleep 10; done
      running && { echo "STILL RUNNING after 30 min; not resuming"; exit 2; }
      echo "stopped"
    else
      echo "not running"
    fi
    tmux kill-session -t "contribute-$C" 2>/dev/null || true
    docker exec "$W" python -m app.louise node resume "$C" && echo "resumed"
    ;;
  *) echo "usage: contribute.sh start|stop <collection>"; exit 2 ;;
esac
