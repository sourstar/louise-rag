#!/bin/bash
# rollout-qwen-nofa-20261031.sh — gutenberg's encoder (vega-qwen3-embedding-4b)
# with flash attention OFF: on, it was not bit-exact across fresh loads (flips
# in 4 of 8 runs of 16 spread + 32 prefixes up to 8,191 words, max abs
# 3.3e-5–1.3e-4, a spread text among them; invariant 12). Off, measured on the
# temporary model vega-qwen3-embedding-4b-next (the same YAML, FA off): 8 of 8
# runs and the 512-prefix sweep up to 2,048 bit-identical, 2,400 vectors
# (tools/vega_repro_check.py at b3dedc0, gated on its exit code;
# docs/notes/approved-encoders.md).
#
# The YAML moves only to what was measured: it must equal -next's but for the
# name. gutenberg stays PAUSED until the gate has passed (nothing is written
# meanwhile: its documents are all embedded, but a new book would be).
#   bash /root/rollout-qwen-nofa-20261031.sh 2>&1 | tee /root/rollout-qwen-nofa-20261031.log
#
# RAN 2026-10-31 04:31 AND FAILED AT THE GATE (run c73e2a75): FA off is
# bit-exact and WRONG on SYCL, cosine to the reference median 0.13 / 0.09.
# Backed out to $BAK at 04:56; nothing written, gutenberg paused. Kept as the
# record. The next candidate is gated BEFORE its YAML moves:
#   louise hold embedder check gutenberg --model <candidate> --query-model <candidate>
# (docs/notes/lessons-learned.md, "A bit-exact encoder that was wrong").
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/vega-qwen3-embedding-4b.yaml
NEXT=$M/vega-qwen3-embedding-4b-next.yaml
BAK=$Y.bak-20261031-nofa
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
as_next() { sed 's/^name: vega-qwen3-embedding-4b$/name: vega-qwen3-embedding-4b-next/' "$1"; }

say "0. preconditions: gutenberg paused, the YAML FA on, -next the measured FA-off copy of it"
$L node show | python3 -c "import sys,json; p=json.load(sys.stdin)['overrides'].get('paused_collections') or []; sys.exit(0 if 'gutenberg' in p else 1)" \
  || { echo "gutenberg is not paused: pause it first"; exit 1; }
grep -q '^flash_attention: "on"$' $Y || { echo "$Y is not FA on"; exit 1; }
grep -q '^flash_attention: "off"$' $NEXT || { echo "$NEXT is not FA off"; exit 1; }

say "1. the YAML FA off (a backup beside it), equal to the measured -next but for the name"
cp -a $Y $BAK
sed -i 's/^flash_attention: "on"$/flash_attention: "off"/' $Y
as_next $Y | cmp - $NEXT || { echo "$Y differs from the measured $NEXT: backing out"; cp -a $BAK $Y; exit 1; }
grep -E '^flash_attention' $Y

say "2. the temporary model removed, both unloaded, LocalAI's configs reloaded"
rm -f $NEXT
for m in vega-qwen3-embedding-4b vega-qwen3-embedding-4b-next; do
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
curl -s -X POST "$LOCALAI/models/reload"; echo

say "3. the gate (gutenberg paused: nothing is written until it passes), then gutenberg resumed"
$L hold embedder check gutenberg
$L node resume gutenberg
echo "gutenberg's encoder FA off; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"vega-qwen3-embedding-4b"};
#   POST /models/reload; gutenberg stays paused (FA on is the unreproducible run).
