# syntax=docker/dockerfile:1
# louise-python as a LocalAI backend IMAGE: what LocalAI's gallery, its web
# GUI ("Backends" -> "From URL") and `local-ai backends install` take. The
# image holds the backend's directory at its root, run.sh first, as
# LocalAI's own backend images do (core/gallery/backends.go refuses an image
# with no run.sh); LocalAI unpacks it into its backends path under the name
# the install gives, and writes metadata.json there itself.
#
#   docker build -f deploy/localai/backends/louise-python/Dockerfile \
#     --build-arg VARIANT=intel -t louise-python:intel .
#   docker save louise-python:intel -o louise-python-intel.tar
#     -> GUI "From URL": ocifile:///<the tar, as LocalAI's container sees it>
#
# Context: the repository root. VARIANT picks requirements-<VARIANT>.txt.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ${PYTHON_IMAGE} AS build
ARG VARIANT=intel
# LocalAI's release the stubs and its common/ files come from: the app's
ARG LOCALAI_TAG=v4.11.0
ARG GRPCIO_TOOLS_VERSION=1.84.0
# the portable CPython LocalAI's own Python backends carry
ARG PBS_TAG=20250818
ARG PBS_FILE=cpython-3.12.11+20250818-x86_64-unknown-linux-gnu-install_only.tar.gz
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY deploy/localai/backends/requirements-grpc.txt /src/requirements-grpc.txt
RUN pip install -q "grpcio-tools==${GRPCIO_TOOLS_VERSION}" -r /src/requirements-grpc.txt
# LocalAI's backend.proto at v4.11.0, byte for byte (tests/fixtures/vega)
COPY tests/fixtures/vega/backend.proto /proto/backend.proto
RUN set -eu; mkdir -p /out; python - <<PY
import hashlib, urllib.request
base = "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
sums = urllib.request.urlopen(base + "/SHA256SUMS", timeout=60).read().decode()
want = next(l.split()[0] for l in sums.splitlines() if l.endswith("${PBS_FILE}"))
data = urllib.request.urlopen(base + "/${PBS_FILE}", timeout=600).read()
assert hashlib.sha256(data).hexdigest() == want, "python-build-standalone: sha256 mismatch"
open("/python.tgz", "wb").write(data)
for f in ("grpc_auth.py", "model_identity.py", "parent_watch.py"):
    b = urllib.request.urlopen("https://raw.githubusercontent.com/mudler/LocalAI/${LOCALAI_TAG}"
                               "/backend/python/common/" + f, timeout=60).read()
    open("/out/" + f, "wb").write(b)
    print(f, hashlib.sha256(b).hexdigest())
PY
RUN mkdir /out/python && tar -xzf /python.tgz -C /out/python --strip-components=1 \
 && python -m grpc_tools.protoc -I/proto --python_out=/out --grpc_python_out=/out backend.proto

# the libraries first, so a change to the code below does not refetch them
COPY deploy/localai/backends/louise-python/requirements*.txt /req/b/
RUN set -eu; cp /src/requirements-grpc.txt /req/; P=/out/python/bin/python3; \
    $P -m pip install -q -r /req/b/requirements-${VARIANT}.txt; \
    $P -m pip install -q -r /req/b/requirements.txt; \
    $P -m pip check; \
    $P -m pip freeze; \
    $P -m pip uninstall -y -q pip; \
    if [ "${VARIANT}" = rocm ]; then \
      # torch's ROCm build links the system's libatomic, which neither this
      # image nor every host has: the backend carries the one it was built with
      apt-get update -qq && apt-get install -y -qq --no-install-recommends libatomic1 >/dev/null; \
      cp -L /usr/lib/x86_64-linux-gnu/libatomic.so.1 /out/python/lib/; \
    fi

# the backend: the dispatcher, the shared module, and each task as the backend
# it replaces (backend.py's TASKS), beside the model code it imports
COPY deploy/localai/backends/louise_backend.py               /out/
COPY deploy/localai/backends/louise-python/backend.py        /out/
COPY deploy/localai/backends/louise-python/run.sh            /out/
COPY deploy/localai/backends/louise-dar-legal-it/backend.py  /out/task_dar_legal_it.py
COPY deploy/localai/backends/louise-madlad/backend.py        /out/task_madlad.py
COPY deploy/localai/backends/louise-medcpt-article-ov/backend.py /out/task_medcpt_article.py
COPY deploy/localai/backends/louise-medcpt-rerank/backend.py /out/task_medcpt_rerank.py
COPY deploy/dar-legal-it/server.py                           /out/dar_legal_it.py
COPY deploy/madlad-translate/server.py                       /out/madlad.py
COPY deploy/medcpt-rerank/server.py                          /out/medcpt.py
COPY deploy/localai/backends/louise-python/task_jina_v3.py   /out/task_jina_v3.py
COPY deploy/jina-embeddings-v3/server.py                     /out/jina_v3.py
# everything compiled now: nothing is written into the tree at run time (a
# backend behind VeGa is identified by its installed files, hashed at every load)
RUN set -eu; P=/out/python/bin/python3; chmod 0755 /out/run.sh; \
    cd /out && LD_LIBRARY_PATH=/out/python/lib $P -B -E -s -c "import backend, task_dar_legal_it, \
task_madlad, task_medcpt_article, task_medcpt_rerank, task_jina_v3, backend_pb2_grpc, grpc_auth, torch, \
transformers, sentence_transformers, openvino, tokenizers; \
print('torch', torch.__version__, 'transformers', transformers.__version__, 'openvino', \
openvino.__version__)"; \
    find /out -name __pycache__ -type d -prune -exec rm -rf {} +; \
    $P -m compileall -q -j 0 /out >/dev/null 2>&1 || true

FROM scratch
COPY --from=build /out/ /
