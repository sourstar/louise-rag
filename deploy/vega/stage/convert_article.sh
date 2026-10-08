#!/bin/sh
# Switch-over step 3 (docs/notes/vega.md §12): deploy/medcpt-rerank/convert_article.py
# on the node, as its docstring says (HF_HUB_OFFLINE, the HF cache already in
# /mnt/ssd5/apps/medcpt-models, fetching no weights), with this staging's
# pairs as its check set, then article_reference.py on the same pairs (the
# reference vectors for vega-medcpt-article-ov). Run on the node as root:
#
#   sh /tmp/vega-stage/src/deploy/vega/stage/convert_article.sh
#
# Writes /mnt/ssd5/apps/medcpt-models/medcpt-article-ov and
# /tmp/vega-stage/out/article-reference.json.
set -eu
S=/tmp/vega-stage
PY=python@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
mkdir -p "$S/out"
test -s "$S/out/pairs.json"
docker run --rm -e HF_HUB_OFFLINE=1 -e PIP_DISABLE_PIP_VERSION_CHECK=1 \
  -v /mnt/ssd5/apps/medcpt-models:/models \
  -v "$S/src/deploy/medcpt-rerank:/w:ro" -v "$S/src/deploy/vega/stage:/st:ro" \
  -v "$S/out:/out" -v "$S/pip:/root/.cache/pip" "$PY" sh -c '
    pip install -q torch --index-url https://download.pytorch.org/whl/cpu &&
    pip install -q transformers openvino==2026.3.1 tokenizers==0.22.2 numpy &&
    python -c "import torch, transformers, openvino; print(\"versions: torch\", torch.__version__, \"transformers\", transformers.__version__, \"openvino\", openvino.__version__)" &&
    python /w/convert_article.py /out/pairs.json &&
    python /st/article_reference.py /out/pairs.json /out/article-reference.json'
ls -la /mnt/ssd5/apps/medcpt-models/medcpt-article-ov
du -sh /mnt/ssd5/apps/medcpt-models/medcpt-article-ov
