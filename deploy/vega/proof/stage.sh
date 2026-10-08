#!/bin/sh
# VeGa node proof: stage the repo's files for build_package.sh and the
# vega-proof image into one tarball (run from the repo root, on the dev PC).
set -eu
out=${1:-vega-src.tgz}
tar -czf "$out" \
  deploy/vega/run.sh deploy/vega/vega_wrapper.py deploy/vega/requirements.txt \
  deploy/vega/proof tests/fixtures/vega/backend.proto \
  app/__init__.py app/hold/__init__.py app/hold/codex.py app/rage/__init__.py \
  app/rage/model.py app/rage/procfs.py app/rage/runs.py app/rage/loadopts.py \
  app/rage/store.py app/vega/__init__.py app/vega/tag.py app/vega/client.py \
  app/vega/service.py app/vega/__main__.py \
  app/gpu.py app/gpu_probe.py app/gpu_memory.py
echo "$out"
