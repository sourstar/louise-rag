#!/bin/sh
# Switch-over step 3 (docs/notes/vega.md §12): builds one `vega-<backend>`
# LocalAI backend package IN PLACE — VeGa's wrapper (deploy/vega) in front of
# the real backend $BACKEND — the proof's build_package.sh made general. Runs
# in the pinned python:3.12-slim with the package directory mounted at the
# path LocalAI's container sees it, so every path the venv bakes in is right:
#
#   docker run --rm -e PKG=vega-llama-cpp -e BACKEND=intel-sycl-f16-llama-cpp \
#     -e SOCKET=/backends/vega-llama-cpp/run/receipts.sock \
#     -v /mnt/tank/apps/localai/backends/vega-llama-cpp:/backends/vega-llama-cpp \
#     -v /tmp/vega-stage/src:/src:ro -v /tmp/vega-stage/cache:/cache:ro \
#     python@sha256:54c85f3c… sh /src/deploy/vega/stage/build_vega_package.sh
#
# `-e CUTS=context_size` for vega-louise-python: its tasks cut every input at
# the model's context_size themselves; llama.cpp cuts nothing, so the default,
# `tokenizer`, has the wrapper cut it (docs/notes/char-cut-truncation.md).
#
# A rebuild replaces everything but `run/` (the receipts socket, bind-mounted
# into the receipts service's container: the directory must stay the same
# inode) and `neo-cache/`.
#
# The card and its runtime default to the node's Arc. Another host passes
# them (the dev PC's RX 9060 XT under WSL2, deploy/localai-desktop):
#   -e CARDS='{"0000:03:00.0": "AMD Radeon RX 9060 XT"}' \
#   -e RUNTIME_PACKAGES='["hip-runtime-amd", "hsa-rocr", "rocdxg-roct"]' \
#   -e AMD_SMI=/opt/rocm-wsl/bin/amd-smi -e OWNER=0:0
# An NVIDIA host passes its card and nvidia-smi instead (deploy/localai-nvidia;
# RUNTIME_PACKAGES stays unset: the CUDA libraries the backend MAPS are its runtime):
#   -e CARDS='{"0000:01:00.0": "NVIDIA GeForce RTX 4090"}' -e NVIDIA_SMI=/usr/bin/nvidia-smi
set -eu
: "${PKG:?}" "${BACKEND:?}" "${SOCKET:?}"
CARDS=${CARDS:-'{"0000:03:00.0": "Intel(R) Arc(TM) Pro B70 Graphics"}'}
RUNTIME_PACKAGES=${RUNTIME_PACKAGES:-}
AMD_SMI=${AMD_SMI:-}
NVIDIA_SMI=${NVIDIA_SMI:-}
OWNER=${OWNER:-568:0}
P=/backends/$PKG
test -d "$P"
find "$P" -mindepth 1 -maxdepth 1 ! -name run ! -name neo-cache -exec rm -rf {} +

# the portable Python, as LocalAI's python backends carry it (libbackend.sh)
mkdir -p "$P/python"
tar -xzf /cache/python.tgz -C "$P/python" --strip-components=1
"$P/python/bin/python3" -m venv "$P/venv"
# relative symlinks into ../../python, as _makeVenvPortable writes them
rm -f "$P/venv/bin/python" "$P/venv/bin/python3" "$P/venv/bin/python3.12"
ln -s ../../python/bin/python3 "$P/venv/bin/python3"
ln -s python3 "$P/venv/bin/python"
ln -s python3 "$P/venv/bin/python3.12"
"$P/venv/bin/python" -m pip install --no-cache-dir -q -r /src/deploy/vega/requirements.txt
"$P/venv/bin/python" -m pip uninstall -y -q pip || true

cp /cache/stubs/backend_pb2.py /cache/stubs/backend_pb2_grpc.py /cache/stubs/backend.proto "$P/"
cp /cache/common/*.py "$P/"
cp /src/deploy/vega/run.sh /src/deploy/vega/vega_wrapper.py /src/deploy/vega/requirements.txt "$P/"
chmod 0755 "$P/run.sh"
for f in app/__init__.py app/hold/__init__.py app/hold/codex.py \
         app/vega/model.py app/vega/procfs.py app/vega/runs.py app/vega/loadopts.py \
         app/vega/dxg.py app/vega/nvidia.py app/vega/__init__.py app/vega/tag.py; do
  mkdir -p "$P/$(dirname "$f")"
  cp "/src/$f" "$P/$f"
done
cp /src/REVISION "$P/REVISION"
python - "$P/vega.json" "$BACKEND" "$SOCKET" "${CUTS:-tokenizer}" "$CARDS" \
         "$RUNTIME_PACKAGES" "$AMD_SMI" "$NVIDIA_SMI" <<'PY'
import json, sys
path, backend, sock, cuts, cards, packages, amd_smi, nvidia_smi = sys.argv[1:]
# `cuts` (vega_wrapper.CUTS): how an input is cut to its tag's window —
# llama.cpp by the wrapper with its tokenizer, louise-python by its tasks
doc = {"backend": backend, "receipts_socket": sock, "cuts": cuts, "cards": json.loads(cards)}
if packages:
    doc["runtime_packages"] = json.loads(packages)
if amd_smi:
    doc["amd_smi"] = amd_smi
if nvidia_smi:
    doc["nvidia_smi"] = nvidia_smi
open(path, "w").write(json.dumps(doc, indent=2) + "\n")
PY
printf '{"name": "%s"}\n' "$PKG" > "$P/metadata.json"
mkdir -p "$P/run" "$P/neo-cache"
chmod 0777 "$P/run"

# it imports, with the package's own python, from the package's directory
cd "$P" && ./venv/bin/python -c "
import grpc, google.protobuf, backend_pb2, backend_pb2_grpc, grpc_auth, parent_watch
import vega_wrapper
print('imports ok: grpcio', grpc.__version__, 'protobuf', google.protobuf.__version__)"
find "$P" -name __pycache__ -type d -prune -exec rm -rf {} +
# compiled once here, so nothing is written into the package at run time —
# EVERYTHING, the portable stdlib and the venv included, as
# build_louise_backend.sh does. Excluding them (until 2026-10-12) let Python
# write their .pyc files at the first loads, so a new wrapper's tree — and
# the engine identity hashed from it — moved under the first Codex pinned to
# it (vega-louise-dar-legal-it: run 85b9fa1a… became 651c4ec0…). A few
# stdlib test files do not compile, as everywhere.
"$P/venv/bin/python" -m compileall -q -j 0 "$P" >/dev/null 2>&1 || true
find "$P" -path "$P/run" -prune -o -exec chown "$OWNER" {} +
du -sh "$P"
