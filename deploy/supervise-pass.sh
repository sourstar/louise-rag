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
#   exit 130 or 143            STOPPED ON PURPOSE: a `docker stop` (TrueNAS's
#                              stop included) sends SIGTERM, and a pass ends
#                              its unit and exits 130 (07 §7.7), or 143 if it
#                              did not catch it. Not restarted: the operator
#                              stops a pass to fix something, and a pass that
#                              came back by itself would undo the stop
#                              (operator, 2026-10-31). The kernel's OOM kill
#                              is SIGKILL, 137, and still comes back
#   a hold file present        stopped on purpose: `touch $HOLD` before a
#                              deliberate stop (deploy, measurement)
#   exit 2                     REFUSED (07 §7.7: "a rule stood in the way
#                              before anything was done"): not restarted, since
#                              nothing changes by itself. München, 2026-11-02:
#                              pubmed's CPU reference was refused (VeGa states
#                              no run for an unmeasured tree) and came back
#                              every minute, holding the night's chain
#   exit 137 or 141            again in 60 s with PARSE_PROCESSES halved
#                              (12 -> 6 -> 3 -> 2): an OOM loop backs off.
#                              141 is SIGPIPE: until 2026-10-30 a pool child the
#                              kernel killed took its parent with it
#   any other exit             again in 60 s, at most MAX_RESTARTS times
#
# Every pass carries its own memory cap (`--memory`; refused without one)
# and runs FIRST IN LINE for the kernel's OOM killer (`--oom-score-adj`,
# default 500), under the ceiling on every container together
# (deploy/docker-memory-ceiling.sh, applied before each run): the caps of
# what runs at once may add up to more than the host, and when they do the
# kernel kills inside Docker's cgroup — a pass, which comes back — never on
# the host at large. Kubernetes does the same with kubepods.slice and its
# QoS classes' oom_score_adj.
#
# The standard alternative is Docker's own `--restart on-failure` (no
# --rm), which also survives a node reboot but cannot back off the pool on
# an OOM; this is München's /root/wikidata/build-supervisor.sh, generalised.
set -u
NAME=$1; shift
HOLD=${HOLD:-/root/supervise/$NAME.hold}
MAX_RESTARTS=${MAX_RESTARTS:-20}
PAUSE=${PAUSE:-60}
PROCS=${PARSE_PROCESSES:-}
CEILING=${CEILING:-$(dirname "$0")/docker-memory-ceiling.sh}
capped=
for a in "$@"; do
    case $a in -m|--memory|--memory=*|-m?*) capped=1 ;; esac
done
[ -n "$capped" ] || { echo "== $NAME: no --memory cap; every pass carries one (CLAUDE.md, \"Louise survives a crash\")"; exit 2; }
mkdir -p "$(dirname "$HOLD")"
rm -f "$HOLD"
n=0
while :; do
    [ -x "$CEILING" ] && "$CEILING"
    env_args=()
    [ -n "$PROCS" ] && env_args=(-e "PARSE_PROCESSES=$PROCS")
    echo "== $(date -u +%FT%TZ) $NAME: run $((n + 1))${PROCS:+, PARSE_PROCESSES=$PROCS}"
    docker run --rm --name "$NAME" --oom-score-adj "${OOM_SCORE_ADJ:-500}" "${env_args[@]}" "$@"
    code=$?
    echo "== $(date -u +%FT%TZ) $NAME: exit $code"
    [ "$code" -eq 0 ] && exit 0
    [ -e "$HOLD" ] && { echo "== $NAME: held ($HOLD), not restarted"; exit "$code"; }
    if [ "$code" -eq 2 ]; then
        echo "== $NAME: refused (exit 2), not restarted: a rule stood in the way; fix it and run it again"
        exit 2
    fi
    if [ "$code" -eq 130 ] || [ "$code" -eq 143 ]; then
        echo "== $NAME: stopped on purpose (SIGTERM, exit $code), not restarted; run it again to resume"
        exit "$code"
    fi
    n=$((n + 1))
    [ "$n" -ge "$MAX_RESTARTS" ] && { echo "== $NAME: $n restarts, giving up"; exit "$code"; }
    if [ "$code" -eq 137 ] || [ "$code" -eq 141 ]; then
        cur=${PROCS:-$(nproc)}
        PROCS=$(( cur / 2 > 2 ? cur / 2 : 2 ))
    fi
    sleep "$PAUSE"
done
