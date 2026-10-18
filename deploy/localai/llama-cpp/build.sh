#!/bin/sh
# Builds LocalAI's llama.cpp backend WITH OUR PATCHES (deploy/localai/patches:
# the series' base plus `git am` of each, in order) as the LocalAI backend
# image upstream publishes — the same Dockerfile and build arguments as its CI
# matrix (.github/backend-matrix.yml at the base) — and saves it as a tar that
# LocalAI installs from (Backends → From URL, `ocifile://<tar>`).
#
#   sh deploy/localai/llama-cpp/build.sh <variant> <out dir>
#
# <variant>: `cpu`       — ubuntu:24.04, llama-cpp-cpu-all
#            `sycl_f16`  — intel/oneapi-basekit, the node's Arc (upstream's
#                          intel-sycl-f16-llama-cpp)
#            `hipblas`   — rocm/dev-ubuntu-24.04:7.2.1, an AMD card (upstream's
#                          rocm-llama-cpp); AMDGPU_TARGETS=gfx1200 narrows it
#                          to the RX 9060 XT
# Out: <out dir>/louise-llama-cpp-<variant>-<series digest>.tar, and the image
# tagged louise-llama-cpp:<variant>-<series digest>.
#
# Needs git, docker (BuildKit) and the network; nothing from Louise's image.
# The engine it builds is a NEW engine (another tree than upstream's): each
# model on it is a new run, gated once (docs/03 §3.3), measured for bitwise
# reproducibility first (CLAUDE.md invariant 12).
set -eu
VARIANT=${1:?variant: cpu | sycl_f16 | hipblas}
OUT=${2:?out dir}
HERE=$(cd "$(dirname "$0")" && pwd)
PATCHES=$HERE/../patches
BASE=$(sed -n 's/^base //p' "$PATCHES/series")
SERIES=$(grep -v '^#' "$PATCHES/series" | grep -v '^base ' | grep .)

case $VARIANT in
  cpu)      BUILD_TYPE=; BASE_IMAGE=ubuntu:24.04
            BUILDER=quay.io/go-skynet/ci-cache:base-grpc-amd64 ;;
  sycl_f16) BUILD_TYPE=sycl_f16; BASE_IMAGE=intel/oneapi-basekit:2025.3.0-0-devel-ubuntu24.04
            BUILDER=quay.io/go-skynet/ci-cache:base-grpc-intel-amd64 ;;
  hipblas)  BUILD_TYPE=hipblas; BASE_IMAGE=rocm/dev-ubuntu-24.04:7.2.1
            BUILDER=quay.io/go-skynet/ci-cache:base-grpc-rocm-amd64 ;;
  *) echo "unknown variant: $VARIANT" >&2; exit 2 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
git -C "$WORK" init -q
git -C "$WORK" -c core.autocrlf=false fetch -q --depth 1 https://github.com/mudler/LocalAI.git "$BASE"
git -C "$WORK" -c core.autocrlf=false checkout -q FETCH_HEAD
for p in $SERIES; do
  git -C "$WORK" -c user.name=build -c user.email=build@localhost -c core.autocrlf=false \
    am -q "$PATCHES/$p"
done
# the series' own digest names the build: the same patches, the same tag
HEAD=$(cd "$PATCHES" && cat series $SERIES | sha256sum | cut -c1-12)
TAG=louise-llama-cpp:$VARIANT-$HEAD

DOCKER_BUILDKIT=1 docker build --progress=plain -f "$WORK/backend/Dockerfile.llama-cpp" \
  --build-arg BUILD_TYPE="$BUILD_TYPE" --build-arg BASE_IMAGE="$BASE_IMAGE" \
  --build-arg BUILDER_TARGET=builder-prebuilt --build-arg BUILDER_BASE_IMAGE="$BUILDER" \
  --build-arg UBUNTU_VERSION=2404 ${AMDGPU_TARGETS:+--build-arg AMDGPU_TARGETS="$AMDGPU_TARGETS"} \
  -t "$TAG" "$WORK"
mkdir -p "$OUT"
docker save "$TAG" -o "$OUT/louise-llama-cpp-$VARIANT-$HEAD.tar"
echo "built $TAG from $BASE + $(echo $SERIES | wc -w) patch(es): $OUT/louise-llama-cpp-$VARIANT-$HEAD.tar"
