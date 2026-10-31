#!/bin/bash
# rollout-112-rerank-20261031.sh — pubmed's reranker (louise-rerank-medcpt) onto
# louise-python-20261030, the tree carrying #112 (on device:xpu every input
# padded to one static shape). The deployed tree gave 55–56 of 256 scores
# different across 3 fresh loads x fwd/rev (batch-mates), on 3 pubmed queries.
#
# MEASURED FIRST, on the temporary model louise-rerank-medcpt-next (the same
# YAML, the new backend, LOUISE_BITEXACT_MEASURED = the new tree), 2026-10-31:
#   - reproducible: 0 of 256 differ across 3 loads x 2 orders, each query
#     (/root/measure/rerank_sweep2.py);
#   - CORRECT against the deployed tree: max |diff| 2.8e-4, the same top 10
#     and top 1, no pair ordered differently (/root/measure/rerank_agree.py) —
#     a bit-exact run can still be wrong (lessons-learned, 2026-10-31);
#   - latency of one 50-document rerank, warm: median 99 ms against 54 ms (1.8x).
# The YAML moves only to what was measured: it must equal -next's but for the name.
#   bash /root/rollout-112-rerank-20261031.sh 2>&1 | tee /root/rollout-112-rerank-20261031.log
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/louise-rerank-medcpt.yaml
NEXT=$M/louise-rerank-medcpt-next.yaml
BAK=$Y.bak-20261031-112
OLDT=louise-python-20261021b; NEWT=louise-python-20261030
LOCALAI=http://localhost:30286
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
as_next() { sed 's/^name: louise-rerank-medcpt$/name: louise-rerank-medcpt-next/' "$1"; }

say "0. preconditions: the YAML on the old tree, -next on the new one"
grep -q "^backend: vega-$OLDT$" $Y && grep -q "LOUISE_BITEXACT_MEASURED: $OLDT$" $Y || { echo "$Y is not on $OLDT"; exit 1; }
grep -q "^backend: vega-$NEWT$" $NEXT || { echo "$NEXT is not on $NEWT"; exit 1; }

say "1. the YAML onto the new tree (a backup beside it), equal to the measured -next but for the name"
cp -a $Y $BAK
sed -i "s/^backend: vega-$OLDT$/backend: vega-$NEWT/; s/LOUISE_BITEXACT_MEASURED: $OLDT$/LOUISE_BITEXACT_MEASURED: $NEWT/" $Y
as_next $Y | cmp - $NEXT || { echo "$Y differs from the measured $NEXT: backing out"; cp -a $BAK $Y; exit 1; }
grep -E "^backend|LOUISE_BITEXACT_MEASURED" $Y

say "2. the temporary model removed, both unloaded, LocalAI's configs reloaded"
rm -f $NEXT
for m in louise-rerank-medcpt louise-rerank-medcpt-next; do
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}"; echo
done
curl -s -X POST "$LOCALAI/models/reload"; echo

say "3. the live model answers on the new tree, reproducibly (one load, fwd/rev)"
curl -s -X POST "$LOCALAI/v1/rerank" -H "Content-Type: application/json" \
  -d '{"model":"louise-rerank-medcpt","query":"natural killer cell activation","documents":["NK cells kill tumor cells.","Atrial flutter in athletes."],"top_n":2}'; echo
echo "pubmed's reranker on $NEWT; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"louise-rerank-medcpt"}; POST /models/reload.
