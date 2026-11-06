#!/bin/bash
# derive_gguf_context.sh SRC DST KEY VALUE
#
# A GGUF whose declared training context is rewritten, and NOTHING else.
#
# Operator, 2026-11-06, "B" (Command R7B at the Qwens' 16K):
# - every Command R7B GGUF declares cohere2.context_length 8192: llama.cpp's
#   conversion never read the 128K Cohere documents (llama.cpp #10816,
#   ollama #8465);
# - our engine caps a slot's context at the model's declared training context,
#   so the 16384 the model file asks became 8192 (the sampling probe, 11-06:
#   "request (21673 tokens) exceeds the available context size (8192 tokens)").
#
# llama.cpp's own gguf-py tool (gguf_set_metadata.py) rewrites the value in
# place, on a copy: a scalar of the same type, no other byte moves. The bytes
# that differ are then counted (one uint32: at most 4 bytes; anything else is
# refused), and DST.derivation.json records the source and its sha256, the
# key and value, the tool, the bytes changed and DST's sha256. A verifier can
# redo it from the uploader's file.
#
#   GGUF_PY=<llama.cpp source>/gguf-py deploy/vega/stage/derive_gguf_context.sh \
#     llama-cpp/models/c4ai-command-r7b-12-2024-Q8_0.gguf \
#     llama-cpp/models/c4ai-command-r7b-12-2024-Q8_0.ctx131072.gguf \
#     cohere2.context_length 131072
#
# Run with numpy available (the worker image has it); GGUF_PY is the gguf-py
# directory of the llama.cpp source the engine is built from.
set -euo pipefail
SRC=$1; DST=$2; KEY=$3; VALUE=$4
GGUF_PY=${GGUF_PY:?the gguf-py directory of the llama.cpp source the engine is built from}
TOOL=""
for t in "$GGUF_PY"/gguf/scripts/gguf_set_metadata.py "$GGUF_PY"/scripts/gguf_set_metadata.py; do
  [ -f "$t" ] && { TOOL=$t; break; }
done
[ -n "$TOOL" ] || { echo "refused: no gguf_set_metadata.py under $GGUF_PY"; exit 2; }
[ -e "$DST" ] && { echo "refused: $DST exists"; exit 2; }
cp "$SRC" "$DST.tmp"
PYTHONPATH="$GGUF_PY" python3 "$TOOL" --force "$DST.tmp" "$KEY" "$VALUE"
n=$(cmp -l "$SRC" "$DST.tmp" | wc -l || true)
if [ "$n" -lt 1 ] || [ "$n" -gt 4 ]; then
  echo "refused: $n bytes differ; one uint32 value is 1 to 4 bytes"; rm -f "$DST.tmp"; exit 1
fi
src_sha=$(sha256sum "$SRC" | cut -d' ' -f1)
dst_sha=$(sha256sum "$DST.tmp" | cut -d' ' -f1)
mv "$DST.tmp" "$DST"
cat > "$DST.derivation.json" <<EOF
{"source": "$(basename "$SRC")", "source_sha256": "$src_sha", "key": "$KEY", "value": $VALUE,
 "tool": "llama.cpp gguf-py $(basename "$TOOL")", "bytes_changed": $n, "sha256": "$dst_sha"}
EOF
echo "derived $DST: $n bytes changed, sha256 $dst_sha"
