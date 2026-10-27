#!/bin/bash
# RETIRED 2026-10-27 — replaced by the GPU governor app's profile level (app/gpu_level.py,
# app/gpu_governor.py, deploy/truenas-gpu-governor.yaml): platform.toml's [schedule] picks the
# profile, its [gpu.levels.<name>] is applied every tick (hwmon power1_max where writable, else
# max_freq), never under the query hold, and restored on a clean stop (docs/notes/node-profiles-design.md,
# docs/notes/node-runbook.md §9). Kept, not deleted: München's crons still call it until the
# switch-over. Remove its crons when platform.toml gets its [schedule]: both would limit the card,
# on different timetables.
#
# gpu-cap.sh on|off — the operator, 2026-10-26: "Quiet hours light gpu usage (quiet)". In the quiet
# hours (22:30-06:00, cron 6 / cron 5 with set-embed.sh) the B70's max_freq is capped at half its
# own maximum (rp0); at 06:00 it is given back. A clock changes how fast a vector comes, never its
# bytes: the same kernels, the same order (invariant 12 untouched). The GPU governor's hold during a
# query is HOLD_FRACTION (0.5) of rp0, so it never exceeds the cap.
set -u
F=/sys/class/drm/card0/device/tile0/gt0/freq0
LOG=/root/louise-scheduled/gpu-cap.log
exec >>"$LOG" 2>&1
pci=$(basename "$(readlink -f /sys/class/drm/card0/device)")
[ "$pci" = "0000:03:00.0" ] || { echo "== $(date '+%F %T') card0 is $pci, not the B70: nothing written"; exit 1; }
rp0=$(cat "$F/rp0_freq"); cap=$(( rp0 / 2 ))
case "${1:-}" in
on)  target=$cap ;;
off) target=$rp0 ;;
*) echo "on|off"; exit 2 ;;
esac
# min_freq may not exceed max_freq: never lower max below the current min
min=$(cat "$F/min_freq"); [ "$target" -lt "$min" ] && target=$min
echo "$target" > "$F/max_freq"
echo "== $(date '+%F %T') $1: max_freq $(cat "$F/max_freq") MHz (rp0 $rp0, min $(cat "$F/min_freq")); vectors' bytes unchanged (a clock, not a kernel)"
