#!/bin/sh
# VeGa node proof (docs/notes/vega.md §10): builds the `vega-llama-cpp` LocalAI
# backend package IN PLACE, inside a throwaway python:3.12-slim container that
# mounts the package directory at the path LocalAI's container sees it
# (/backends/vega-llama-cpp), so every absolute path a venv bakes in is right
# there. Inputs under /src (staged by stage.sh). Not a deliverable: a proof.
#
#   docker run --rm -v <backends>/vega-llama-cpp:/backends/vega-llama-cpp \
#     -v /tmp/vega-src:/src:ro python:3.12-slim-bookworm@sha256:54c85f… sh /src/build_package.sh
set -eu
PKG=/backends/vega-llama-cpp
LOCALAI_TAG=v4.11.0
PBS_TAG=20250818
PBS_FILE=cpython-3.12.11+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz
PBS_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}

cd /tmp
python - <<PY
import hashlib, urllib.request
base = "${PBS_URL}"
sums = urllib.request.urlopen(base + "/SHA256SUMS", timeout=60).read().decode()
want = next(l.split()[0] for l in sums.splitlines() if l.endswith("${PBS_FILE}"))
data = urllib.request.urlopen(base + "/${PBS_FILE}", timeout=600).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, (got, want)
open("/tmp/py.tgz", "wb").write(data)
print("python-build-standalone", "${PBS_FILE}", "sha256", got)
for f in ("grpc_auth.py", "model_identity.py", "parent_watch.py"):
    url = "https://raw.githubusercontent.com/mudler/LocalAI/${LOCALAI_TAG}/backend/python/common/" + f
    b = urllib.request.urlopen(url, timeout=60).read()
    open("${PKG}/" + f, "wb").write(b)
    print(f, "sha256", hashlib.sha256(b).hexdigest())
PY

# the portable Python, as LocalAI's python backends carry it (libbackend.sh)
rm -rf "$PKG/python" "$PKG/venv"
mkdir -p "$PKG/python"
tar -xzf /tmp/py.tgz -C "$PKG/python" --strip-components=1
"$PKG/python/bin/python3" -m venv "$PKG/venv"
# relative symlinks into ../../python, as _makeVenvPortable writes them
rm -f "$PKG/venv/bin/python" "$PKG/venv/bin/python3" "$PKG/venv/bin/python3.12"
ln -s ../../python/bin/python3 "$PKG/venv/bin/python3"
ln -s python3 "$PKG/venv/bin/python"
ln -s python3 "$PKG/venv/bin/python3.12"
"$PKG/venv/bin/python" -m pip install --no-cache-dir -q -r /src/deploy/vega/requirements.txt
"$PKG/venv/bin/python" -m pip uninstall -y -q pip || true

# stubs from LocalAI's backend.proto at v4.11.0 (the fixture is byte-identical)
pip install --no-cache-dir -q grpcio-tools==1.84.0
mkdir -p /tmp/stubs
python -m grpc_tools.protoc -I/src/tests/fixtures/vega --python_out=/tmp/stubs \
  --grpc_python_out=/tmp/stubs /src/tests/fixtures/vega/backend.proto
cp /tmp/stubs/backend_pb2.py /tmp/stubs/backend_pb2_grpc.py "$PKG/"
cp /src/tests/fixtures/vega/backend.proto "$PKG/"

# the wrapper and the Louise modules it carries (vega_wrapper.py's docstring)
cp /src/deploy/vega/run.sh /src/deploy/vega/vega_wrapper.py /src/deploy/vega/requirements.txt "$PKG/"
chmod 0755 "$PKG/run.sh"
for f in app/__init__.py app/hold/__init__.py app/hold/codex.py \
         app/vega/model.py app/vega/procfs.py app/vega/runs.py app/vega/loadopts.py \
         app/vega/__init__.py app/vega/tag.py; do
  mkdir -p "$PKG/$(dirname "$f")"
  cp "/src/$f" "$PKG/$f"
done
cp /src/vega.json "$PKG/vega.json"
printf '{"name": "vega-llama-cpp"}\n' > "$PKG/metadata.json"
mkdir -p "$PKG/run" "$PKG/neo-cache"
chmod 0777 "$PKG/run"

# it imports, with the package's own python, from the package's directory
cd "$PKG" && ./venv/bin/python -c "
import grpc, google.protobuf, backend_pb2, backend_pb2_grpc, grpc_auth, parent_watch
import vega_wrapper
print('imports ok: grpcio', grpc.__version__, 'protobuf', google.protobuf.__version__)"
find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R 568:0 "$PKG"
du -sh "$PKG"
