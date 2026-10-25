#!/bin/bash
# wikipedia-build.sh start|stop — the operator's weekday windows for wikipedia-v8's
# records build (2026-10-24, STATUS ba566b5): 08:00-12:00, 13:30-17:00, 22:30-06:00
# Monday to Friday, all day at the weekend; TrueNAS cron starts and stops it.
# stop: SIGTERM, the build finishes its part, commits, exits 130 (invariant 2) and
# the next start resumes at the part after it. start: nothing if it is running.
# Until the deployed tree carries the parse pool fixes (a833c1e streamed pages; c3dcb4c gh-115634, the bounded stop),
# the build runs the tree /root/louise-build-src points at (a symlink) on the deployed image.
set -u
C=louise-wikipedia-night
LOG=/root/louise-scheduled/wikipedia-build.log
BUILD_LOG=/root/wikipedia-v8/build-night.log
exec >>"$LOG" 2>&1
case "${1:-}" in
start)
  if docker ps -q --filter "name=^${C}$" | grep -q .; then echo "== $(date '+%F %T') start: already running"; exit 0; fi
  if [ -e /root/hold-wikipedia-build ]; then echo "== $(date '+%F %T') start: held ($(cat /root/hold-wikipedia-build))"; exit 0; fi
  extra=""
  if ! grep -q "gh-115634" /mnt/ssd4/apps/louise-rag/src/app/parallel.py; then
    extra="-v ${BUILD_SRC:-/root/louise-build-src}:/srv:ro -w /srv -e PYTHONPATH=/srv -e PYTHONDONTWRITEBYTECODE=1"
  fi
  # the pool takes the whole quota, every core (the operator, 2026-10-25: "Use all the cores";
  # 6694b19 sizes it so by itself, c3dcb4c's tree needs PARSE_PROCESSES)
  extra="$extra -e PARSE_PROCESSES=$(nproc)"
  EXTRA="$extra" nohup setsid bash /root/wk-build-step.sh >>"$BUILD_LOG" 2>&1 </dev/null &
  sleep 20
  echo "== $(date '+%F %T') start: $(docker ps --filter "name=^${C}$" --format '{{.Status}}') EXTRA=${extra:-none}"
  ;;
stop)
  if ! docker ps -q --filter "name=^${C}$" | grep -q .; then echo "== $(date '+%F %T') stop: not running"; exit 0; fi
  last=$(grep -o 'shard [0-9]* written ([0-9]*/[0-9]* records of the slice)' <(docker logs --tail 50 "$C" 2>&1) | tail -1)
  docker kill -s TERM "$C" >/dev/null
  rc=$(timeout 900 docker wait "$C" 2>/dev/null) || {
    # 2026-10-25: a833c1e's streamed pool can hang in interpreter shutdown after main() has
    # returned and committed (queue feeders blocked on exited children). Its parts are
    # committed; SIGKILL only skips the teardown (staged and resumable, invariant 2's bound).
    docker kill -s KILL "$C" >/dev/null 2>&1; rc="no exit within 15 min: SIGKILL (the parts before the signal are committed)"; }
  echo "== $(date '+%F %T') stop: exit $rc; last before the signal: ${last:-?}"
  ;;
*) echo "start|stop"; exit 2 ;;
esac
