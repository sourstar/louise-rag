#!/bin/bash
# rollout-rerank-fa-20261031.sh — louise-rerank-bge (bge-reranker-v2-m3, every query's reranker) from the
# strict settings (flash attention off, GGML_SYCL_ENABLE_OPT/GRAPH/FUSION=0: 13-17 s for 50 candidates) onto
# the fast ones it had until 2026-10-21 (FA on, OPT/GRAPH/FUSION default: /root/louise-rerank-bge.yaml.fast-NaN-
# 20261021, 1.8-2.2 s for 50), which gave NaN scores in batches of 40-72 — now with GGML_SYCL_FA_ONEDNN=0, the
# cause of FA's NaNs (oneDNN's fused SDPA; approved-encoders.md). PRE-APPROVED by the architecture session
# (2026-10-31, the operator: "reranker first ... used for every query") if the study is clean (0 NaN, scores
# identical across loads and orders; variants34.log) and faster. A reranker has no calibration gate: the check
# is #112's (rollout-112-rerank-20261031.sh) — the candidate's scores against the live ones on gutenberg's
# real candidates (top-10, max |diff|), kept, and the live model after the move giving those scores bit for bit.
#   cp rollout-rerank-fa-20261031.sh /root/rollout-rerank-fa-run.sh; bash /root/rollout-rerank-fa-run.sh 2>&1 | tee /root/rollout-rerank-fa-20261031.log
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/louise-rerank-bge.yaml
CAND=$M/louise-rerank-bge-fa1.yaml
BAK=$Y.bak-20261031-fa
KEPT=/root/rerank-bge-fa1-scores.json
CANDS=/root/measure/rerank-cands.json
LOCALAI=http://localhost:30286
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }

say "0. preconditions: the live YAML strict, the candidate FA on + FA_ONEDNN=0, the study clean"
grep -q '^flash_attention: "off"$' $Y && grep -q '^    GGML_SYCL_ENABLE_OPT: "0"$' $Y || { echo "$Y is not the strict YAML"; exit 1; }
grep -q '^flash_attention: "on"$' $CAND && grep -q '^    GGML_SYCL_FA_ONEDNN: "0"$' $CAND || { echo "$CAND is not the candidate"; exit 1; }
S=$(sed -n '/^=== .* reranker fa1$/,/^=== .* reranker live/p' /root/measure/variants34.log)
echo "$S" | grep -q "^=== louise-rerank-bge-fa1: 0 request(s) with NaN or an error" || { echo "the study is not clean (NaN or an error)"; exit 1; }
echo "$S" | grep "positions differing across answers" | grep -vq "answers 0 of" && { echo "the study's scores varied across loads or orders"; exit 1; } || true
echo "$S" | grep "latency n=" ; echo "study: 0 NaN, scores identical across loads and orders"

say "1. the candidate against the live reranker on gutenberg's real candidates; its scores kept; the latency"
python3 /root/measure/rerank_cmp.py louise-rerank-bge louise-rerank-bge-fa1 $CANDS --save $KEPT

say "2. the live YAML onto the candidate's settings (a backup beside it): equal to it but for the name, the live pin kept"
cp -a $Y $BAK
python3 - $Y $CAND <<'PY'
import sys, yaml
y, cand = sys.argv[1:3]
live, new = yaml.safe_load(open(y)), yaml.safe_load(open(cand))
new["name"] = live["name"]
if "pinned" in live:
    new["pinned"] = live["pinned"]                 # LocalAI's pin state, not a setting: kept as it is
text = yaml.safe_dump(new, sort_keys=True, default_flow_style=False)
open(y + ".new", "w").write(text)
a = dict(new); a.pop("pinned", None); a.pop("name")
b = yaml.safe_load(open(cand)); b.pop("pinned", None); b.pop("name")
assert a == b, "the new YAML differs from the candidate in more than name and pin"
print("the new YAML = the candidate's settings, the name and the pin the live one's")
PY
chown 568:0 $Y.new; chmod 644 $Y.new; mv $Y.new $Y
grep -E '^(flash_attention|pinned)|FA_ONEDNN|ENABLE_OPT' $Y || true

say "3. both names unloaded, the candidate removed, LocalAI's configs reloaded"
for m in louise-rerank-bge louise-rerank-bge-fa1; do
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
rm -f $CAND
curl -s -X POST "$LOCALAI/models/reload"; echo

say "4. the live reranker gives the candidate's scores, bit for bit"
python3 /root/measure/rerank_cmp.py louise-rerank-bge louise-rerank-bge $CANDS --expect $KEPT \
  || { echo "THE LIVE RERANKER DOES NOT GIVE THE MEASURED SCORES: BACKOUT below"; exit 1; }
echo "louise-rerank-bge: flash attention on + GGML_SYCL_FA_ONEDNN=0; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"louise-rerank-bge"}; POST /models/reload.
