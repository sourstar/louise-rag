#!/usr/bin/env bash
# in-window.sh — run a resumable node script only inside the operator's time
# window (2026-10-22: "until 12:00, then 13:30 to 17:00"); at the window's end
# every `app.louise` process the script started in the worker gets SIGTERM —
# finish the unit, commit, exit 130 (invariant 2) — and the script stops; the
# next window runs it again and it resumes.
#   in-window.sh END SCRIPT LOG     (END: "HH:MM" today, or a date(1) time such as "2026-10-23 06:00")
set -u
END=$1; SCRIPT=$2; LOG=$3
W=ix-louise-rag-worker-1
case "$END" in *-*) end_s=$(date -d "$END" +%s) ;; *) end_s=$(date -d "today $END" +%s) ;; esac
now_s=$(date +%s)
[ "$now_s" -lt "$end_s" ] || { echo "$(date +%T) window over before it began" >> "$LOG"; exit 0; }
before=$(docker top "$W" -eo pid,args | awk '/app.louise/ {print $1}' | sort -u)
bash "$SCRIPT" >> "$LOG" 2>&1 &
RUNNER=$!
while kill -0 "$RUNNER" 2>/dev/null; do
  if [ "$(date +%s)" -ge "$end_s" ]; then
    echo "=== $(date +%T) window ends at $END: SIGTERM to this run's louise processes" >> "$LOG"
    for p in $(docker top "$W" -eo pid,args | awk '/app.louise/ {print $1}' | sort -u); do
      echo "$before" | grep -qx "$p" || kill -TERM "$p" 2>/dev/null
    done
    # and a step the script runs in a container of its own (a capped one,
    # wikipedia-keep-night.sh): labelled louise.in-window, signalled alike
    for c in $(docker ps -q --filter label=louise.in-window); do
      docker kill -s TERM "$c" >/dev/null 2>&1
    done
    for i in $(seq 1 120); do kill -0 "$RUNNER" 2>/dev/null || break; sleep 5; done
    kill -TERM "$RUNNER" 2>/dev/null
    echo "=== $(date +%T) stopped for the window; run again in the next one" >> "$LOG"
    exit 0
  fi
  sleep 20
done
wait "$RUNNER"; rc=$?
echo "=== $(date +%T) the script exited $rc inside the window" >> "$LOG"
exit $rc
