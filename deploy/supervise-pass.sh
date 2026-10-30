#!/bin/bash
# THE SUPERVISOR FOR AN OPERATOR PASS (operator, 2026-10-30: a process the
# kernel kills recovers by itself, "not involving this agent. This applies
# to any worker"). Every pass is resumable (invariant 2: records build,
# snapshot, check, vectors build, relocate), so a pass that dies is started
# again until it COMPLETES:
#
#   deploy/supervise-pass.sh <name> <docker run arguments...>
#
#   exit 0                     done
#   a hold file present        stopped on purpose: `touch $HOLD` before a
#                              deliberate stop (deploy, measurement)
#   exit 137 (OOM / SIGKILL)   again in 60 s with PARSE_PROCESSES halved
#                              (12 -> 6 -> 3 -> 2): an OOM loop backs off
#   any other exit             again in 60 s, at most MAX_RESTARTS times
#
# The standard alternative is Docker's own `--restart on-failure` (no
# --rm), which also survives a node reboot but cannot back off the pool on
# an OOM; this is München's /root/wikidata/build-supervisor.sh, generalised.
set -u
NAME=$1; shift
HOLD=${HOLD:-/root/supervise/$NAME.hold}
MAX_RESTARTS=${MAX_RESTARTS:-20}
PROCS=${PARSE_PROCESSES:-}
mkdir -p "$(dirname "$HOLD")"
rm -f "$HOLD"
n=0
while :; do
    env_args=()
    [ -n "$PROCS" ] && env_args=(-e "PARSE_PROCESSES=$PROCS")
    echo "== $(date -u +%FT%TZ) $NAME: run $((n + 1))${PROCS:+, PARSE_PROCESSES=$PROCS}"
    docker run --rm --name "$NAME" "${env_args[@]}" "$@"
    code=$?
    echo "== $(date -u +%FT%TZ) $NAME: exit $code"
    [ "$code" -eq 0 ] && exit 0
    [ -e "$HOLD" ] && { echo "== $NAME: held ($HOLD), not restarted"; exit "$code"; }
    n=$((n + 1))
    [ "$n" -ge "$MAX_RESTARTS" ] && { echo "== $NAME: $n restarts, giving up"; exit "$code"; }
    if [ "$code" -eq 137 ]; then
        cur=${PROCS:-$(nproc)}
        PROCS=$(( cur / 2 > 2 ? cur / 2 : 2 ))
    fi
    sleep 60
done
