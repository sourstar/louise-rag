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
LOUISE_PLATFORM_IMAGE=$(docker image inspect -f '{{.Id}}' "$image")
export LOUISE_PLATFORM_IMAGE
echo "platform image: $LOUISE_PLATFORM_IMAGE"
docker compose -f "$here/compose.yaml" --env-file "$env_file" up -d
docker compose -f "$here/compose.yaml" --env-file "$env_file" logs init
docker compose -f "$here/compose.yaml" --env-file "$env_file" ps
