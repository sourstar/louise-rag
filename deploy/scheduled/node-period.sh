#!/bin/bash
# node-period.sh night|day|quiet — the operator's schedule (2026-10-26), every day, node time:
#   22:30-06:00 night  build 12 cores   GPU uncapped, gutenberg embeds fully
#   08:00-12:00 day    build 12 cores   GPU uncapped, gutenberg embeds fully (queries keep priority)
#   13:30-16:30 day    build 12 cores   as the morning
#   06:00-08:00, 12:00-13:30, 16:30-22:30 QUIET: build stopped, gutenberg on-demand, GPU power-limited
#     so the fan holds <= ~1,200 RPM
# CPU: 12 cores by day and night since 2026-11-02 (it was 8 by day). The quiet periods suspend
# processing (the operator, 2026-11-02: "suspend processing during the quiet time frames in the
# morning, at lunch time and in the evening"): the app's own profile does it ([profiles.quiet]
# mode = "acquire-only", node-profiles-design.md decision 6); this script, for a cron that runs it
# again, stops the build. Its crons are disabled (runbook §6).
# TrueNAS cron runs it at each boundary. The build stops by SIGTERM and resumes at its next part
# (wikipedia-build.sh); a start sets the container's cpu quota, and PARSE_PROCESSES follows it.
set -u
P=${1:-}
S=/root/louise-scheduled
LOG=$S/node-period.log
H=$(ls -d /sys/class/drm/card0/device/hwmon/hwmon* 2>/dev/null | head -1)
#: the GPU power limit in the quiet periods, watts: 110 W, the operator's (2026-10-26); it held the fan at 1,213 rpm avg (runbook §6)
QUIET_W=${QUIET_W:-110}
#: the card's own limit, given back outside the quiet periods
FULL_W=230
say() { echo "== $(date '+%F %T') $P: $*" >> "$LOG"; }
pci=$(basename "$(readlink -f /sys/class/drm/card0/device)")
gpu_limit() {  # $1 watts
  if [ "$pci" != "0000:03:00.0" ] || [ -z "$H" ]; then say "card0 is $pci, not the B70: no power limit written"; return; fi
  echo $(( $1 * 1000000 )) > "$H/power1_cap" && say "GPU power1_cap $(( $(cat "$H/power1_cap") / 1000000 )) W (a power limit changes how fast a vector comes, never its bytes)"
}
case "$P" in
night|day)
  cpus=12
  bash "$S/set-embed.sh" gutenberg on-demand all
  gpu_limit "$FULL_W"
  # a build already running at another size (a manual start) is resized: stop, then start
  bash "$S/wikipedia-build.sh" stop
  CPUS=$cpus bash "$S/wikipedia-build.sh" start
  say "build $(tail -1 "$S/wikipedia-build.log" | cut -c1-160)" ;;
quiet)
  # processing suspended in the quiet periods (decision 6): the build stops, and resumes at its next part
  bash "$S/wikipedia-build.sh" stop
  say "build $(tail -1 "$S/wikipedia-build.log" | cut -c1-160)"
  bash "$S/set-embed.sh" gutenberg all on-demand
  gpu_limit "$QUIET_W" ;;
*) echo "night|day|quiet"; exit 2 ;;
esac
