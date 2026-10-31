#!/bin/bash
# rollout-qwen-fa-20261031.sh — gutenberg's encoder (vega-qwen3-embedding-4b, the P5 engine) onto flash
# attention ON with GGML_SYCL_FA_ONEDNN=0: oneDNN's fused SDPA was the cause of FA's flips and NaNs
# (approved-encoders.md: FA on, 4 of 8 runs varied, a NaN and a 0.0865 wrong vector; FA on + FA_ONEDNN=0,
# 8 of 8 bit-exact, no NaN, 23-45 % faster than FA off). PRE-APPROVED by the architecture session
# (2026-10-31) on these conditions, each a step that stops the rollout if it fails:
#   1. the 2,048 sweep on the post-#116/#124 run exits 0 with no NaN;
#   2. the multi-load gate (#120) on the CANDIDATE name passes on both sides, its statement version 5 with
#      exactness "exact" and load_variation "1";
#   3. the YAML moves to the candidate's settings exactly (cmp but for the name), the live run checked equal
#      to the gated one (the worker's log lines filtered out: rollout-qwen-p5's false mismatch), gutenberg
#      resumed if this script paused it.
# The speed is measured on real gutenberg chunks, live (FA off) against the candidate in alternating passes,
# before the YAML moves.
#   cp rollout-qwen-fa-20261031.sh /root/rollout-qwen-fa-run.sh; bash /root/rollout-qwen-fa-run.sh 2>&1 | tee /root/rollout-qwen-fa-20261031.log
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/vega-qwen3-embedding-4b.yaml
CAND=$M/vega-qwen3-embedding-4b-fa1.yaml
BAK=$Y.bak-20261031-fa
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
IMG=$(docker inspect -f "{{.Image}}" $W)
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
as_cand() { sed 's/^name: vega-qwen3-embedding-4b$/name: vega-qwen3-embedding-4b-fa1/' "$1"; }
paused() { $L node show 2>/dev/null | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)['overrides'].get('paused_collections') or []))"; }

say "0. preconditions: #116/#124 rolled out, the live YAML FA off, the candidate FA on, both FA_ONEDNN=0"
grep -q "^rolled out: #116" /root/rollout-116-20261031.log || { echo "rollout-116 (b) not complete"; exit 1; }
grep -q '^backend: vega-llama-cpp-p5$' $Y && grep -q '^flash_attention: "off"$' $Y && grep -q '^    GGML_SYCL_FA_ONEDNN: "0"$' $Y \
  || { echo "$Y is not the P5 FA-off YAML with FA_ONEDNN=0"; exit 1; }
grep -q '^backend: vega-llama-cpp-p5$' $CAND && grep -q '^flash_attention: "on"$' $CAND && grep -q '^    GGML_SYCL_FA_ONEDNN: "0"$' $CAND \
  || { echo "$CAND is not the FA-on candidate with FA_ONEDNN=0"; exit 1; }
[ "$(diff $Y $CAND | grep -c '^[<>]')" -eq 4 ] || { echo "the candidate differs in more than name and FA"; diff $Y $CAND; exit 1; }
WAS=$(paused); case " $WAS " in *" gutenberg "*) MINE=;; *) MINE=1; $L node pause gutenberg >/dev/null;; esac
echo "gutenberg paused (by this script: ${MINE:-no})"

say "1. the 2,048 sweep on the candidate (16 spread + 512 prefixes, 2 loads x 2; a NaN scan of every vector)"
docker run --rm --network host -v /mnt/ssd4/apps/louise-rag/src:/srv:ro -v /root/measure:/m:ro -w /srv -e PYTHONPATH=/srv $IMG \
  timeout 7200 python /m/repro_nan.py vega-qwen3-embedding-4b-fa1 --texts calibration/text.jsonl --n 16 --sweep 512 \
  --window 2048 --loads 2 --repeats 2 2>&1 | grep -v "warn:config" | tee /root/rollout-qwen-fa-sweep.txt
[ "${PIPESTATUS[0]}" -eq 0 ] || { echo "THE SWEEP IS NOT CLEAN: nothing moves"; exit 1; }

say "2. the multi-load gate on the CANDIDATE, both sides (the live YAML untouched)"
$L hold embedder check gutenberg --model vega-qwen3-embedding-4b-fa1 --query-model vega-qwen3-embedding-4b-fa1 2>&1 \
  | grep -v "^\[warn:config\]" | tee /root/rollout-qwen-fa-gate.txt
GATED=$(awk '/^  run /{print $2}' /root/rollout-qwen-fa-gate.txt | sort -u)
[ "$(grep -c '^PASS ' /root/rollout-qwen-fa-gate.txt)" -eq 2 ] && [ "$(echo "$GATED" | wc -w)" -eq 1 ] \
  || { echo "the gate did not pass both sides on one run"; exit 1; }
docker exec -i $W python - "$GATED" <<'PY' 2>/dev/null | grep "^statement" | tee -a /root/rollout-qwen-fa-gate.txt
import json, sys
from pathlib import Path
from app.hold import runs
for role in ("document", "query"):
    body = json.loads(runs.path_for(Path("/data/collections"), "gutenberg", sys.argv[1], role).read_text())["body"]
    body = json.loads(body) if isinstance(body, str) else body
    print(f"statement {role}: version {body.get('version')} exactness {body.get('exactness')} "
          f"loads {body.get('loads')} load_variation {body.get('load_variation')} passed {body.get('passed')}")
PY
[ "$(grep -c '^statement .*: version 5 exactness exact .* load_variation 1 ' /root/rollout-qwen-fa-gate.txt)" -eq 2 ] \
  || { echo "the statements are not version 5, exact, load_variation 1: nothing moves"; exit 1; }
echo "gated run: $GATED"

say "3. the speed on real gutenberg chunks: live (FA off) against the candidate, alternating passes"
sed -e 's/FA on :/live   :/; s/FA off:/cand   :/' /root/measure/fa_speed.py > /root/measure/fa_speed2.py
docker exec -i $W python - 150 2 vega-qwen3-embedding-4b-fa1 < /root/measure/fa_speed2.py 2>&1 | grep -v "warn:config" \
  | tee /root/rollout-qwen-fa-speed.txt

say "4. the live YAML onto the candidate's settings (a backup beside it), equal to it but for the name"
cp -a $Y $BAK
sed 's/^name: vega-qwen3-embedding-4b-fa1$/name: vega-qwen3-embedding-4b/' $CAND > $Y.new
chown 568:0 $Y.new; chmod 644 $Y.new; mv $Y.new $Y
as_cand $Y | cmp - $CAND
grep -E '^(backend|flash_attention)|FA_ONEDNN' $Y

say "5. the candidate names removed, every name unloaded, LocalAI's configs reloaded"
rm -f $CAND $M/vega-qwen3-embedding-4b-fa2.yaml
for m in vega-qwen3-embedding-4b vega-qwen3-embedding-4b-fa1 vega-qwen3-embedding-4b-fa2; do
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
curl -s -X POST "$LOCALAI/models/reload"; echo

say "6. the live model answers with the gated run, both sides"
LIVE=$(docker exec $W python -c "
from app import collections as c, run_check
r = c.recipe_for(c.by_name('gutenberg'))
print(run_check.answering_run(r)); print(run_check.answering_run(r, query=True))" 2>/dev/null | grep '^sha256:' | sort -u)
echo "live run(s): $LIVE"
[ "$LIVE" = "$GATED" ] || { echo "the live run is not the gated one: gutenberg stays paused (BACKOUT below)"; exit 1; }

say "7. gutenberg resumed (if this script paused it)"
[ -n "$MINE" ] && $L node resume gutenberg >/dev/null
echo "gutenberg's encoder: FA on + GGML_SYCL_FA_ONEDNN=0 on the P5 engine, run $GATED; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"vega-qwen3-embedding-4b"}; POST /models/reload;
#   the run before (FA off, gated by rollout-116) answers again; resume gutenberg.
