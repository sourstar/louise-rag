#!/usr/bin/env sh
# deploy/docker/up.sh [ENV_FILE] — build the image, then init and start the
# node of deploy/docker/compose.yaml with ENV_FILE (default deploy/docker/.env).
#
# LOUISE_PLATFORM_IMAGE is the id of the image just built: `hold collection
# create` hashes it into a new collection's identity (07 §7.3), and only
# that image — or one proven equivalent — builds its records (03 §3.4). A
# container cannot see its own image id, so the start hands it over.
#
# SKIP_BUILD=1 starts from the image already tagged.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
# Git Bash on Windows: hand docker a Windows path, whatever MSYS_NO_PATHCONV says
if command -v cygpath >/dev/null 2>&1; then here=$(cygpath -m "$here"); repo=$(cygpath -m "$repo"); fi
env_file=${1:-$here/.env}
test -f "$env_file" || { echo "no $env_file: copy $here/louise.env.example there first" >&2; exit 2; }

image=$(sed -n 's/^LOUISE_IMAGE=//p' "$env_file" | tail -1)
image=${image:-louise-rag:local}
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  docker build -t "$image" "$repo"
fi
# The image's CONFIG digest — Docker's classic store calls it the image id,
# and the node's Codexes name images by it. Under the containerd image store
# (Docker Desktop's default) `.Id` is the manifest or index digest instead,
# so the same image would carry another name on another host: the second
# peer named its build by an index digest no Codex could ever match
# (2026-10-07). `docker save` writes the classic manifest.json either way.
desc=$(docker image inspect -f '{{json .Descriptor}}' "$image" 2>/dev/null || true)
if [ -n "$desc" ] && [ "$desc" != "null" ] && [ "$desc" != "<no value>" ]; then
  cfg=$(docker save "$image" | tar -xOf - manifest.json | grep -o '"Config":"blobs/sha256/[0-9a-f]*"' | grep -oE '[0-9a-f]{64}')
  LOUISE_PLATFORM_IMAGE="sha256:$cfg"
else
  LOUISE_PLATFORM_IMAGE=$(docker image inspect -f '{{.Id}}' "$image")
fi
export LOUISE_PLATFORM_IMAGE
echo "platform image: $LOUISE_PLATFORM_IMAGE"
docker compose -f "$here/compose.yaml" --env-file "$env_file" up -d
docker compose -f "$here/compose.yaml" --env-file "$env_file" logs init
docker compose -f "$here/compose.yaml" --env-file "$env_file" ps
