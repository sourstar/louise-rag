#!/bin/bash
# Migration step 15f, phase 2, after phase1.sh ended "phase 1 done": one
# request to every model through RAGE over LocalAI, Louise resumed, and every
# collection's calibration answers recorded again and compared with the ones
# recorded before the swap (deploy/rage/15f/record15f.py). The worker re-gates each new run by
# itself (03 §3.3); its spaces stay in quarantine until it passes.
set -euo pipefail
DIR=/mnt/ssd5/apps/louise-rage-15c/cutover
say() { echo "[15f $(date -u +%H:%M:%S)] $*"; }

python3 - <<'P'
import json, urllib.request
B = "http://127.0.0.1:30437/upstream"
def post(path, body):
    r = urllib.request.urlopen(urllib.request.Request(B + path, data=json.dumps(body).encode(),
                               headers={"content-type": "application/json"}), timeout=600)
    return r.headers.get("louise-run"), json.loads(r.read())
for m in ("bge-m3", "medcpt-article", "medcpt-query", "qwen3-embedding-4b"):
    run, out = post(f"/{m}/v1/embeddings", {"input": ["a sentence"]})
    print(m, "run", run, "dim", len(out["data"][0]["embedding"]))
for m in ("bge-reranker", "medcpt-rerank"):
    run, out = post(f"/{m}/v1/rerank", {"query": "whale", "documents": ["a whale", "a ship"]})
    print(m, "run", run, [r["index"] for r in out["results"]])
run, out = post("/chat/v1/chat/completions", {"messages": [{"role": "user", "content": "Capital of Italy? One sentence. /no_think"}],
                                              "temperature": 0, "max_tokens": 40})
print("chat run", run, out["choices"][0]["message"]["content"].strip()[:80])
run, out = post("/madlad/v1/completions", {"prompt": json.dumps({"items": [{"text": "The whale.", "to": "it"}]})})
print("madlad run", run, json.loads(out["choices"][0]["text"])["translations"])
P
curl -s http://127.0.0.1:30437/louise/models | python3 -c "
import json, sys
bad = [m for m in json.load(sys.stdin) if m.get('reason')]
print('models stating no run:', bad or 'none')
sys.exit(1 if bad else 0)"
say "every model answered with a run"

H=$(mktemp); chmod 600 $H; printf 'X-Admin-Token: %s\n' "$(cat /root/louise-token.txt)" > $H
curl -sf -X POST -H @$H -H 'content-type: application/json' -d '{"state":"running"}' http://127.0.0.1:30042/api/control
rm -f $H; echo
say "Louise resumed: run everything"

R="docker run --rm --network host --volumes-from ix-louise-rag-worker-1:ro -v /mnt/ssd4/apps/louise-rag/src:/srv:ro -v $DIR:/out -w /srv -e PYTHONPATH=/srv -e CONFIG_DIR=/config -e DATA_DIR=/data --entrypoint python louise-rag:local deploy/rage/15f/record15f.py"
$R after /out
$R compare before after /out
say "phase 2 done: compare the cosines above with each space's tolerance; the gates run by themselves"
