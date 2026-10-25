#!/usr/bin/env bash
# wikipedia-v8's records build, as wk-build.sh, with EXTRA docker arguments (the tree mounted at /srv,
# the pool's size); resumable. The operator, 2026-10-25: "Use all the cores" — every core of the
# node, at nice 19: a person's query and the GPU chain's harness run at nice 0 and go first.
set -euo pipefail
W=ix-louise-rag-worker-1
CAP=${CAP:-24g}
CPUS=${CPUS:-$(nproc)}
IMG=$(docker inspect "$W" -f '{{.Image}}')
ENVF=$(mktemp); chmod 600 "$ENVF"; trap 'rm -f "$ENVF"' EXIT
docker inspect "$W" -f '{{range .Config.Env}}{{println .}}{{end}}' | grep -v '^$' > "$ENVF"
printf '\n=== %s  records build wikipedia-v8 (cap %s, %s cpus, image of %s, EXTRA=%s)\n' "$(date +%T)" "$CAP" "$CPUS" \
  "$(cat /mnt/ssd4/apps/louise-rag/src/.deployed-commit)" "${EXTRA:-}"
# shellcheck disable=SC2086
docker run --rm --init --name louise-wikipedia-night --label "louise.in-window=${IN_WINDOW_ID:-none}" \
  --memory "$CAP" --memory-swap "$CAP" --cpus "$CPUS" --volumes-from "$W" --env-file "$ENVF" -e PYTHONUNBUFFERED=1 ${EXTRA:-} \
  "$IMG" nice -n 19 python -m app.louise hold records build wikipedia-v8 2>&1 | grep --line-buffered -v warn:config
echo "WIKIPEDIA BUILD DONE"
