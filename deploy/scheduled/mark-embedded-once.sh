#!/bin/bash
# mark.sh — the one-off reconcile of documents contribute embedded and never
# marked (ea59c1b `dev mark-embedded`), under the node's pause of gutenberg.
set -u
W=ix-louise-rag-worker-1
exec >>/root/mark-embedded.log 2>&1
echo "== $(date '+%F %T')"
docker exec $W python -m app.louise dev mark-embedded gutenberg --dry-run 2>&1 | grep -v acquire_enabled
docker exec $W python -m app.louise node pause gutenberg >/dev/null && echo paused
for i in $(seq 1 60); do
  docker exec $W python -m app.louise dev mark-embedded gutenberg 2>&1 | grep -v acquire_enabled > /tmp/mark.out; rc=${PIPESTATUS[0]}
  cat /tmp/mark.out
  grep -q "held by the node" /tmp/mark.out || break
  sleep 10
done
echo "mark-embedded exit $rc $(date '+%T')"
docker exec $W python -m app.louise node resume gutenberg >/dev/null && echo resumed
echo MARK DONE
