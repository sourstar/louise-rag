#!/bin/bash
# rollout-qwen-p5-20261031.sh — gutenberg's encoder (vega-qwen3-embedding-4b)
# onto candidate (iii): flash attention OFF, the F16 KV cache, the llama.cpp
# engine carrying P1–P5 (louise-sycl-f16-llama-cpp-p5 behind vega-llama-cpp-p5;
# stage-p5-embedder-20261031.sh). On the shared engine FA on is not bit-exact
# across fresh loads, and FA off is bit-exact and WRONG (P5's bug: gate cosine
# 0.13; rollout-qwen-nofa-20261031.sh).
#
# Measured on the candidate model vega-qwen3-embedding-4b-p5, 2026-10-31:
#   - correct: cosine to FA on min 0.999737, median 1.000000 on 48 texts;
#   - 1.99–2.06 chunks/s on 150 gutenberg chunks (FA on 3.9–5.4; candidate
#     (i), f32 KV on the shared engine, 1.69–1.81);
#   - reproducible: 8 runs of 48 texts x 3 fresh loads x 2 + the 512-prefix
#     sweep to 2,048 (tools/vega_repro_check.py, exit code; qwen-p5.log).
# The ORDER is the lesson of the FA-off attempt: the candidate is GATED under
# its own name first (`hold embedder check --model`, 5388ade), and the YAML
# moves only after a pass, to the candidate's settings exactly (cmp but for
# the name). A run is its engine, not its name: the live model then answers
# with the run just validated — checked in step 4 before gutenberg resumes.
#   bash /root/rollout-qwen-p5-20261031.sh 2>&1 | tee /root/rollout-qwen-p5-20261031.log
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/vega-qwen3-embedding-4b.yaml
CAND=$M/vega-qwen3-embedding-4b-p5.yaml
NEXT=$M/vega-qwen3-embedding-4b-next.yaml         # candidate (i), measured and not chosen
BAK=$Y.bak-20261031-p5
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
as_cand() { sed 's/^name: vega-qwen3-embedding-4b$/name: vega-qwen3-embedding-4b-p5/' "$1"; }

say "0. preconditions: gutenberg paused, the live YAML FA on the shared engine, the candidate staged"
$L node show | python3 -c "import sys,json; p=json.load(sys.stdin)['overrides'].get('paused_collections') or []; sys.exit(0 if 'gutenberg' in p else 1)" \
  || { echo "gutenberg is not paused: pause it first"; exit 1; }
grep -q '^backend: vega-llama-cpp$' $Y && grep -q '^flash_attention: "on"$' $Y || { echo "$Y is not the FA-on shared-engine YAML"; exit 1; }
grep -q '^backend: vega-llama-cpp-p5$' $CAND && grep -q '^flash_attention: "off"$' $CAND || { echo "$CAND is not candidate (iii)"; exit 1; }
grep -q "^verdict: CLEAN$" /root/measure/qwen-p5.log || { echo "the candidate's repro runs are not CLEAN"; exit 1; }

say "1. the gate on the CANDIDATE, both sides (the live YAML untouched)"
$L hold embedder check gutenberg --model vega-qwen3-embedding-4b-p5 --query-model vega-qwen3-embedding-4b-p5 \
  | tee /root/rollout-qwen-p5-gate.txt
GATED=$(awk '/^  run /{print $2}' /root/rollout-qwen-p5-gate.txt | sort -u)
echo "gated run(s): $GATED"

say "2. the live YAML onto the candidate's settings (a backup beside it), equal to it but for the name"
cp -a $Y $BAK
sed 's/^name: vega-qwen3-embedding-4b-p5$/name: vega-qwen3-embedding-4b/' $CAND > $Y.new
chown 568:0 $Y.new; chmod 644 $Y.new; mv $Y.new $Y
as_cand $Y | cmp - $CAND
grep -E '^(backend|flash_attention)' $Y

say "3. the candidate names removed, every name unloaded, LocalAI's configs reloaded"
rm -f $CAND $NEXT
for m in vega-qwen3-embedding-4b vega-qwen3-embedding-4b-p5 vega-qwen3-embedding-4b-next; do
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
curl -s -X POST "$LOCALAI/models/reload"; echo

say "4. the live model answers with the gated run, both sides"
LIVE=$(docker exec $W python -c "
from app import collections as c, run_check
r = c.recipe_for(c.by_name('gutenberg'))
print(run_check.answering_run(r)); print(run_check.answering_run(r, query=True))" | sort -u)
echo "live run(s): $LIVE"
[ "$LIVE" = "$GATED" ] || { echo "the live run is not the gated one: gutenberg stays paused (BACKOUT below)"; exit 1; }

say "5. gutenberg resumed"
$L node resume gutenberg
echo "gutenberg's encoder on the P5 engine, FA off; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"vega-qwen3-embedding-4b"};
#   POST /models/reload; gutenberg stays paused (FA on is the unreproducible run).
