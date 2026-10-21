#!/bin/sh
# Switch-over step 3 (docs/notes/vega.md §12): the repo's files the staging
# builds need, at a COMMIT (default HEAD) — never the working tree, which
# another agent may be editing — plus this directory as it is on disk.
# Run from the repo root on the dev PC; copy the tarball to the node's
# /tmp/vega-stage/ and unpack it there as src/.
#
#   sh deploy/vega/stage/stage_src.sh [out.tgz] [commit]
set -eu
out=${1:-vega-stage-src.tgz}
rev=${2:-HEAD}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git archive --format=tar "$rev" \
  deploy/vega/run.sh deploy/vega/vega_wrapper.py deploy/vega/requirements.txt \
  tests/fixtures/vega/backend.proto \
  app/__init__.py app/hold/__init__.py app/hold/codex.py \
  app/vega/model.py app/vega/procfs.py app/vega/runs.py app/vega/loadopts.py \
  app/vega/dxg.py app/vega/nvidia.py app/vega/__init__.py app/vega/tag.py \
  deploy/localai/backends/run.sh deploy/localai/backends/louise_backend.py \
  deploy/localai/backends/requirements-grpc.txt \
  deploy/localai/backends/louise-medcpt-article-ov deploy/localai/backends/louise-medcpt-rerank \
  deploy/localai/backends/louise-madlad deploy/localai/backends/louise-dar-legal-it \
  deploy/madlad-translate/server.py deploy/medcpt-rerank/server.py deploy/dar-legal-it/server.py \
  deploy/medcpt-rerank/convert_article.py \
  app/vega/client.py tools/__init__.py tools/vega_repro_check.py tools/vega_cut_scan.py \
  calibration/text.jsonl deploy/localai/patches/series | tar -x -C "$tmp"
git rev-parse "$rev" > "$tmp/REVISION"
mkdir -p "$tmp/deploy/vega/stage"
cp -r deploy/vega/stage/. "$tmp/deploy/vega/stage/"
find "$tmp" -name __pycache__ -type d -prune -exec rm -rf {} +
tar -czf "$out" -C "$tmp" .
echo "$out ($(cat "$tmp/REVISION"))"
