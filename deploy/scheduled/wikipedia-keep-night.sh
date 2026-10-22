#!/usr/bin/env bash
# wikipedia-keep-night.sh — wikipedia → wikipedia-v8's keep pass by sort-merge
# (7b0f3e0: the per-id lookup cost ~66 ms a row, months for 146 M rows), in the
# quiet hours under in-window.sh; resumable (the scan recomputes what is left).
# Then the re-sign of the carried documents (A1d). Both collections stay paused.
set -euo pipefail
W=ix-louise-rag-worker-1
L="nice -n 19 docker exec $W python -m app.louise"
say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
say "keep producer:upstash-wikipedia-2024-06, by sort-merge"
$L dev carry wikipedia wikipedia-v8 --rebuilt --recut --keep producer:upstash-wikipedia-2024-06 --scan --yes 2>&1 | grep -v warn:config
say "re-sign the carried documents (A1d)"
$L dev carry wikipedia wikipedia-v8 --resign --yes 2>&1 | grep -v warn:config
echo "WIKIPEDIA NIGHT DONE"
