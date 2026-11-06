#!/bin/sh
# Switch-over step 3 (docs/notes/vega.md §12): builds one of the platform's
# Python backends (deploy/localai/backends) IN PLACE for the LocalAI app —
# the model pool's LocalAI image build's `louise-backends` stage (in git before 6a4f517) and its venv step,
# with one difference: the app's image is not the model pool's, so the backend carries
# its own portable CPython (as LocalAI's own python backends do) and, for
# MADLAD, its own torch (2.14.0+xpu, the version the model pool's image pins) in its venv
# instead of a system one; `lib` -> `venv/lib` puts torch's SYCL runtime before
# the image's older oneAPI on LD_LIBRARY_PATH (run.sh), as the model pool's lib/ did.
#
#   docker run --rm -e NAME=louise-medcpt-rerank \
#     -v /mnt/tank/apps/localai/backends/louise-medcpt-rerank:/backends/louise-medcpt-rerank \
#     -v /tmp/vega-stage/src:/src:ro -v /tmp/vega-stage/cache:/cache:ro \
#     -v /tmp/vega-stage/pip:/pipcache python@sha256:54c85f3c… \
#     sh /src/deploy/vega/stage/build_louise_backend.sh
#
# Everything is compiled here, so nothing is written into the backend at run
# time: a backend behind VeGa is identified by its installed files, hashed at
# every load (vega.md §4), and a __pycache__ appearing between two loads would
# be a new engine.
set -eu
: "${NAME:?}"
P=/backends/$NAME
B=/src/deploy/localai/backends
test -d "$P" && test -d "$B/$NAME"
find "$P" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
export PIP_CACHE_DIR=/pipcache PIP_DISABLE_PIP_VERSION_CHECK=1

# the files, as the `louise-backends` stage lays them out
cp "$B/$NAME"/*.py "$B/$NAME/requirements.txt" "$B/run.sh" "$B/louise_backend.py" "$P/"
cp /cache/stubs/backend_pb2.py /cache/stubs/backend_pb2_grpc.py /cache/common/*.py "$P/"
case "$NAME" in
  louise-madlad) cp /src/deploy/madlad-translate/server.py "$P/madlad.py" ;;
  louise-medcpt-rerank|louise-medcpt-article-ov) cp /src/deploy/medcpt-rerank/server.py "$P/medcpt.py" ;;
  louise-dar-legal-it) cp /src/deploy/dar-legal-it/server.py "$P/dar_legal_it.py" ;;
  *) echo "unknown backend $NAME" >&2; exit 2 ;;
esac
cp /src/REVISION "$P/REVISION"
chmod 0755 "$P/run.sh"
printf '{"name": "%s"}\n' "$NAME" > "$P/metadata.json"

# the portable Python and the venv (relative links, as _makeVenvPortable)
mkdir -p "$P/python"
tar -xzf /cache/python.tgz -C "$P/python" --strip-components=1
"$P/python/bin/python3" -m venv "$P/venv"
rm -f "$P/venv/bin/python" "$P/venv/bin/python3" "$P/venv/bin/python3.12"
ln -s ../../python/bin/python3 "$P/venv/bin/python3"
ln -s python3 "$P/venv/bin/python"
ln -s python3 "$P/venv/bin/python3.12"

# requirements.txt reads ../requirements-grpc.txt: resolved from a scratch copy
mkdir -p /tmp/req/b && cp "$B/requirements-grpc.txt" /tmp/req/ && cp "$B/$NAME/requirements.txt" /tmp/req/b/
# torch on the card, in the backend's own venv: MADLAD and DAR-legal-it (2026-10-12)
if [ "$NAME" = louise-madlad ] || [ "$NAME" = louise-dar-legal-it ]; then
  "$P/venv/bin/python" -m pip install -q "torch==2.14.0" --index-url https://download.pytorch.org/whl/xpu
  ln -s venv/lib "$P/lib"
fi
"$P/venv/bin/python" -m pip install -q -r /tmp/req/b/requirements.txt
"$P/venv/bin/python" -m pip uninstall -y -q pip || true

cd "$P"
LD_LIBRARY_PATH="$P/lib" ./venv/bin/python -c "import backend; print('import backend ok')"
case "$NAME" in
  louise-madlad) LD_LIBRARY_PATH="$P/lib" ./venv/bin/python -c "import torch, transformers; print('torch', torch.__version__, 'transformers', transformers.__version__)" ;;
  louise-dar-legal-it) LD_LIBRARY_PATH="$P/lib" ./venv/bin/python -c "import torch, transformers, sentence_transformers; print('torch', torch.__version__, 'transformers', transformers.__version__, 'sentence-transformers', sentence_transformers.__version__)" ;;
  *) ./venv/bin/python -c "import openvino, tokenizers; print('openvino', openvino.__version__, 'tokenizers', tokenizers.__version__)" ;;
esac
find "$P" -name __pycache__ -type d -prune -exec rm -rf {} +
# compile EVERYTHING now (the backend's files, the venv, the portable stdlib);
# a few test files of the stdlib do not compile, as everywhere
"$P/venv/bin/python" -m compileall -q -j 0 "$P" >/dev/null 2>&1 || true
chown -R 568:0 "$P"
du -sh "$P"
