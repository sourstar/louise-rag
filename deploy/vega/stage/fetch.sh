#!/bin/sh
# Switch-over step 3 (docs/notes/vega.md §12): what every staged package
# carries and nothing in the repo holds, fetched ONCE into /cache and checked:
# - the portable CPython LocalAI's own python backends carry
#   (python-build-standalone, checked against the release's SHA256SUMS);
# - LocalAI's grpc_auth.py, model_identity.py, parent_watch.py at the release
#   the LocalAI app runs (v4.11.0), printed with their sha256.
# Runs in the pinned python:3.12-slim with /cache mounted:
#   docker run --rm -v /tmp/vega-stage/cache:/cache -v /tmp/vega-stage/src:/src:ro \
#     python@sha256:54c85f3c… sh /src/deploy/vega/stage/fetch.sh
set -eu
LOCALAI_TAG=v4.11.0
PBS_TAG=20250818
PBS_FILE=cpython-3.12.11+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only.tar.gz
PBS_URL=https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}
mkdir -p /cache/common
python - <<PY
import hashlib, os, urllib.request
base = "${PBS_URL}"
sums = urllib.request.urlopen(base + "/SHA256SUMS", timeout=60).read().decode()
want = next(l.split()[0] for l in sums.splitlines() if l.endswith("${PBS_FILE}"))
path = "/cache/python.tgz"
data = open(path, "rb").read() if os.path.exists(path) else \
    urllib.request.urlopen(base + "/${PBS_FILE}", timeout=600).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, (got, want)
open(path, "wb").write(data)
print("python-build-standalone", "${PBS_FILE}", "sha256", got)
for f in ("grpc_auth.py", "model_identity.py", "parent_watch.py"):
    url = ("https://raw.githubusercontent.com/mudler/LocalAI/${LOCALAI_TAG}"
           "/backend/python/common/" + f)
    b = urllib.request.urlopen(url, timeout=60).read()
    open("/cache/common/" + f, "wb").write(b)
    print(f, "sha256", hashlib.sha256(b).hexdigest())
PY
# stubs from LocalAI's backend.proto at v4.11.0 (the fixture is byte-identical)
pip install --no-cache-dir -q grpcio-tools==1.84.0
rm -rf /cache/stubs && mkdir -p /cache/stubs
python -m grpc_tools.protoc -I/src/tests/fixtures/vega --python_out=/cache/stubs \
  --grpc_python_out=/cache/stubs /src/tests/fixtures/vega/backend.proto
cp /src/tests/fixtures/vega/backend.proto /cache/stubs/
sha256sum /cache/stubs/*
