#!/bin/bash
# THE CEILING ON EVERY CONTAINER TOGETHER (2026-10-30). Each container has
# its own cap (web 12g, worker 24g, LocalAI, a pass's --memory), and the
# caps of what runs at once can add up to more than the host: worker 24 +
# records build 32 + regroup 12 + LocalAI 24 on a 61.9 GB node (München).
# A cap alone keeps a container's own runaway inside it; the overlap of
# several would be a GLOBAL OOM, whose victim the kernel picks anywhere on
# the host (the 14:14 kill could as easily have been LocalAI's).
#
# cgroup v2 is hierarchical: Docker's cgroupfs driver puts every container
# under /sys/fs/cgroup/docker, and a memory.max there bounds their SUM. An
# overlap then ends in an OOM inside that cgroup (CONSTRAINT_MEMCG), whose
# victim is the highest oom_score_adj — a supervised pass (+500,
# deploy/supervise-pass.sh), which comes back by itself — and the host
# keeps its margin. The standard this follows: Kubernetes' kubepods.slice
# with the QoS classes' oom_score_adj. The alternative weighed, admission
# by the sum of the caps (a pass waits until its cap fits): no OOM at all,
# but the services' caps alone add up to more than the ceiling today, and
# memory the services do not use stays idle.
#
#   deploy/docker-memory-ceiling.sh        idempotent; run by supervise-pass
#                                          before each run, and after a boot
#
#   DOCKER_MEM_CEILING   bytes; default 90 % of MemTotal (55.7 GB of 61.9)
#
# NOT persistent: the cgroup's value goes with a reboot or a restart of
# Docker, hence the call before every supervised run. A ceiling under what
# the containers already use is not set: the kernel would OOM at once.
set -u
CG=${DOCKER_CGROUP:-/sys/fs/cgroup/docker}
MEMINFO=${MEMINFO:-/proc/meminfo}
if [ ! -w "$CG/memory.max" ]; then
    echo "== docker memory ceiling: $CG/memory.max is not there or not writable (driver $(docker info --format '{{.CgroupDriver}}' 2>/dev/null || echo '?')); not set"
    exit 0
fi
total=$(awk '/^MemTotal:/ {print $2 * 1024}' "$MEMINFO")
want=${DOCKER_MEM_CEILING:-$(( total * 9 / 10 ))}
want=$(( want / 4096 * 4096 ))                    # the kernel keeps whole pages
have=$(cat "$CG/memory.max")
[ "$have" = "$want" ] && exit 0
used=$(cat "$CG/memory.current" 2>/dev/null || echo 0)
if [ "$used" -ge "$want" ]; then
    echo "== docker memory ceiling: NOT set — the containers use $used bytes, more than $want"
    exit 1
fi
echo "$want" > "$CG/memory.max" || exit 1
echo "== docker memory ceiling: $have -> $want bytes (MemTotal $total, in use $used)"
