#!/bin/bash
# rollout-111-article-20261030.sh — pubmed's article encoder (vega-medcpt-article-ov)
# onto louise-python-20261030, the tree carrying #111/#112: on device:xpu every
# input padded to the context, one static shape (the GPU plugin gave 7 of 160
# live pairs different bytes pass to pass, all of 65–73 tokens, 2026-10-30).
#
# The tree (RULE 1, named): louise-python-20261021b copied whole, medcpt.py,
# task_medcpt_article.py and task_medcpt_rerank.py replaced by 589bd09's
# (their pre-#111 hashes matched the installed tree's), compiled with the
# tree's python, every mode and ACL equal to the original's (the copy had
# inherited an owner-execute ACE from the backends directory: a tree digest
# counts the exec bit). vega-louise-python-20261030 is the 20261021b package
# with vega.json naming the new tree. Nothing else moves: dar-legal-it,
# jina-v3, madlad and the reranker stay on 20261021b and keep their runs.
#
# pubmed stays PAUSED until the end, so the run can be stated for measurement
# with no write possible (03 §3.3: a write needs the gate's validation).
# Steps stop at the first failure; BACKOUT restores the YAML.
#   bash /root/rollout-111-article-20261030.sh 2>&1 | tee /root/rollout-111-article-20261030.log
set -euo pipefail

M=/mnt/ssd5/apps/localai/models
Y=$M/vega-medcpt-article-ov.yaml
BAK=$Y.bak-20261030-111
OLDT=louise-python-20261021b; NEWT=louise-python-20261030
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
SRC=/root/src-589bd09                         # tools/ and app/ at 589bd09: the sweep (#111)
DATA=/mnt/ssd4/apps/louise-rag/data/measure
IMG=$(docker inspect -f '{{.Image}}' $W)
say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
repro() {   # vega_repro_check from 589bd09, in the worker's image, against LocalAI
  docker run --rm --network host -v $SRC:/srv:ro -v $DATA:/m:ro -w /srv -e PYTHONPATH=/srv "$IMG" \
    timeout 3600 python -m tools.vega_repro_check --url http://192.168.178.10:30286 --model vega-medcpt-article-ov "$@"
}

say "0. preconditions: pubmed paused, the new tree and package in place, the YAML on the old tree"
$L node show | python3 -c "import sys,json; p=json.load(sys.stdin)['overrides'].get('paused_collections') or []; sys.exit(0 if 'pubmed' in p else 1)" \
  || { echo "pubmed is not paused: pause it first"; exit 1; }
test -x /mnt/ssd5/apps/localai/backends/$NEWT/run.sh && test -f /mnt/ssd5/apps/localai/backends/vega-$NEWT/vega.json
grep -q "^backend: vega-$OLDT$" $Y && grep -q "LOUISE_BITEXACT_MEASURED: $OLDT$" $Y || { echo "$Y is not on $OLDT"; exit 1; }
test -d $SRC/tools && test -s $DATA/pubmed-cal-pairs.jsonl && test -s $DATA/pubmed-160-pairs.jsonl

say "1. the YAML onto the new tree (a backup beside it), the old model unloaded"
cp -a $Y $BAK
sed -i "s/^backend: vega-$OLDT$/backend: vega-$NEWT/; s/LOUISE_BITEXACT_MEASURED: $OLDT$/LOUISE_BITEXACT_MEASURED: $NEWT/" $Y
grep -E "^backend|LOUISE_BITEXACT_MEASURED" $Y
curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d '{"model":"vega-medcpt-article-ov"}'; echo
curl -s -X POST "$LOCALAI/models/reload"; echo

say "2. invariant 12, the sweep: the calibration pairs + 512 prefixes, 2 fresh loads x 2 shuffled passes"
repro --texts /m/pubmed-cal-pairs.jsonl --window 512 --sweep 512 --loads 2 --repeats 2
say "3. the 160 live items (7 of them differed on the old tree), 2 loads x 2 passes"
repro --texts /m/pubmed-160-pairs.jsonl --n 160 --sweep 0 --window 512 --loads 2 --repeats 2

say "4. the gate (pubmed paused: nothing is written until it passes), then pubmed resumed"
$L hold embedder check pubmed
$L node resume pubmed
echo "pubmed's article encoder on $NEWT; rolled out"

# BACKOUT: cp -a $BAK $Y; POST /backend/shutdown {"model":"vega-medcpt-article-ov"}; POST /models/reload.
#   pubmed's old run is gated already; pubmed stays paused until the class is controlled.
