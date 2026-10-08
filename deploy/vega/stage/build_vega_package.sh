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
# A rebuild replaces everything but `run/` (the receipts socket, bind-mounted
# into the receipts service's container: the directory must stay the same
# inode) and `neo-cache/`.
set -eu
: "${PKG:?}" "${BACKEND:?}" "${SOCKET:?}"
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
for f in app/__init__.py app/hold/__init__.py app/hold/codex.py app/rage/__init__.py \
         app/rage/model.py app/rage/procfs.py app/rage/runs.py app/rage/loadopts.py \
         app/vega/__init__.py app/vega/tag.py; do
  mkdir -p "$P/$(dirname "$f")"
  cp "/src/$f" "$P/$f"
done
cp /src/REVISION "$P/REVISION"
python - "$P/vega.json" "$BACKEND" "$SOCKET" <<'PY'
import json, sys
path, backend, sock = sys.argv[1:]
doc = {"backend": backend, "receipts_socket": sock,
       "cards": {"0000:03:00.0": "Intel(R) Arc(TM) Pro B70 Graphics"}}
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
# compiled once here, so nothing is written into the package at run time
"$P/venv/bin/python" -m compileall -q -j 0 "$P" -x '/(python|venv)/' >/dev/null
find "$P" -path "$P/run" -prune -o -exec chown 568:0 {} +
du -sh "$P"
