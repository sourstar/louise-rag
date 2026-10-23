#!/usr/bin/env bash
# wikipedia-keep-night.sh — wikipedia → wikipedia-v8's keep pass by sort-merge
# (7b0f3e0: the per-id lookup cost ~66 ms a row, months for 146 M rows), in the
# quiet hours under in-window.sh; resumable (the scan recomputes what is left).
# Then the re-sign of the carried documents (A1d). Both collections stay paused.
#
# Each step runs in a CONTAINER OF ITS OWN under a memory cap (CAP, default
# 12g), from the worker's image, mounts and env: on 2026-10-22 the keep ran in
# the uncapped worker and its first 50,000-value Lance delete took 33.8 GB
# before the kernel's global OOM killed it (the delete batch is now sized from
# the fragment count, carry._delete_batch); a capped step dies alone. Labelled
# louise.in-window, so in-window.sh's SIGTERM reaches it at the window's end.
# SRC (optional): a source tree to run instead of the image's, mounted at /srv.
set -euo pipefail
W=ix-louise-rag-worker-1
CAP=${CAP:-12g}
IMG=$(docker inspect "$W" -f '{{.Image}}')
ENVF=$(mktemp); chmod 600 "$ENVF"; trap 'rm -f "$ENVF"' EXIT
docker inspect "$W" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -v '^$' > "$ENVF"
say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
step() {
  docker run --rm --init --name louise-wikipedia-night --label "louise.in-window=${IN_WINDOW_ID:-none}" \
    --memory "$CAP" --memory-swap "$CAP" --cpus 4 --volumes-from "$W" --env-file "$ENVF" \
    ${SRC:+-v "$SRC:/srv:ro"} "$IMG" nice -n 19 python -m app.louise "$@" 2>&1 | grep -v warn:config
}
say "keep producer:upstash-wikipedia-2024-06, by sort-merge (cap $CAP${SRC:+, src $SRC})"
step dev carry wikipedia wikipedia-v8 --rebuilt --recut --keep producer:upstash-wikipedia-2024-06 --scan --yes
say "re-sign the carried documents (A1d)"
step dev carry wikipedia wikipedia-v8 --resign --yes
echo "WIKIPEDIA NIGHT DONE"
