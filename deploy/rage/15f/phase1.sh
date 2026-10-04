#!/bin/bash
# Migration step 15f, phase 1 (run as root on the node: bash phase1.sh), everything but the resume. Stops at the first
# failure; backout.sh undoes whatever ran. No secret is printed.
set -euo pipefail
NEW_RAGE=ghcr.io/sourstar/rage@sha256:e64893cfa93cfbbc6b263b700f23a9b318d1f3a36842245c3ae8155bc1ef4e26
NEW_DIGEST=sha256:e64893cfa93cfbbc6b263b700f23a9b318d1f3a36842245c3ae8155bc1ef4e26
RAGE=/mnt/ssd5/apps/louise-rage
LOUISE=/mnt/ssd4/apps/louise-rag
# the commit to deploy, as `git archive` ships it (with .commit naming it)
TREE=/mnt/ssd5/apps/louise-rage-15c/tree
BK=/mnt/ssd5/apps/louise-rage-15c/cutover/backup
say() { echo "[15f $(date -u +%H:%M:%S)] $*"; }

# ---- 0. backups ---------------------------------------------------------------------------
mkdir -p $BK; chmod 700 $BK
midclt call app.config louise-rage > $BK/louise-rage-app.json
cp -a $RAGE/config/rage.toml $BK/rage.toml
cp -a $LOUISE/config/collections/pubmed.toml $BK/pubmed.toml
python3 - <<'P'
import json, os
from truenas_api_client import Client
with Client() as c:
    cfg = c.call("app.config", "louise-rag")
p = "/mnt/ssd5/apps/louise-rage-15c/cutover/backup/louise-rag-app.json"
with open(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    json.dump(cfg, f)
P
docker tag louise-rag:local louise-rag:pre-15f
docker tag ghcr.io/sourstar/rage@sha256:a8b649abc7eae74c423607817c9c9321f0463fff240cefab22bec23f0a65b9f6 rage:pre-15f
say "backups in $BK; louise-rag:pre-15f and rage:pre-15f tagged"

# ---- 1. Louise paused (finishes its units) --------------------------------------------------
H=$(mktemp); chmod 600 $H; printf 'X-Admin-Token: %s\n' "$(cat /root/louise-token.txt)" > $H
curl -sf -X POST -H @$H -H 'content-type: application/json' -d '{"state":"paused"}' http://127.0.0.1:30042/api/control
rm -f $H; echo
say "Louise paused"

# ---- 2. RAGE: LocalAI's models directory and the cards-only rage.toml ------------------------
mkdir -p $RAGE/state/localai-models
cp $TREE/deploy/rage/localai-models/*.yaml $RAGE/state/localai-models/
ls $RAGE/state/localai-models
grep -E '^(\[rage\]|device_names|pci)' $BK/rage.toml > $RAGE/config/rage.toml
cat $RAGE/config/rage.toml

# ---- 3. RAGE: the app, the new image and its volumes --------------------------------------
python3 - "$NEW_RAGE" "$NEW_DIGEST" <<'P'
import json, sys, subprocess
image, digest = sys.argv[1:3]
cfg = json.load(open("/mnt/ssd5/apps/louise-rage-15c/cutover/backup/louise-rage-app.json"))
s = cfg["services"]["rage"]
s["image"] = image
s["environment"]["RAGE_IMAGE"] = digest
s["healthcheck"]["start_period"] = "60s"
W = "/state/localai-models/weights"
s["volumes"] = [
    "/mnt/ssd5/apps/louise-rage/config:/config",
    "/mnt/ssd5/apps/louise-rage/state:/state",
    f"/mnt/ssd5/apps/bge-m3-models:{W}/bge-m3:ro",
    f"/mnt/ssd5/apps/medcpt-models:{W}/medcpt:ro",
    f"/mnt/ssd5/apps/madlad-models:{W}/madlad:ro",
    f"/mnt/ssd5/apps/llama-embed:{W}/llama-embed:ro",
    f"/mnt/ssd5/apps/louise-rage/models/chat:{W}/chat:ro",
    "/mnt/ssd5/apps/louise-rage/gpufreq:/gpufreq",
    "/mnt/ssd5/apps/louise-rage/secrets/admin.token:/run/rage/admin.token:ro",
]
from truenas_api_client import Client
with Client() as c:
    c.call("app.update", "louise-rage", {"custom_compose_config": cfg}, job=True)
print("louise-rage updated")
P
for i in $(seq 1 60); do
  [ "$(docker inspect ix-louise-rage-rage-1 --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy ] && break; sleep 5
done
docker inspect ix-louise-rage-rage-1 --format '{{.Config.Image}} {{.State.Health.Status}}'
curl -sf http://127.0.0.1:30437/louise/models; echo
say "RAGE over LocalAI is up"

# ---- 4. Louise: pubmed's reranker is LocalAI's /v1/rerank in Jina's shape -------------------
P=$LOUISE/config/collections/pubmed.toml
sed -i 's#^url = "http://192.168.178.10:30437/upstream/medcpt-rerank/rerank"$#url = "http://192.168.178.10:30437/upstream/medcpt-rerank/v1/rerank"#; s#^api = "tei"$#api = "jina"#' $P
CHANGED=$(diff $BK/pubmed.toml $P | grep -c '^[<>]' || true)
diff $BK/pubmed.toml $P || true
[ "$CHANGED" = 4 ] || { echo "pubmed.toml: expected exactly two lines changed, got $CHANGED diff lines"; exit 1; }

# ---- 5. Louise: the code, staged then promoted (DEPLOYMENT.md "Deploying") -------------------
D="app tests tools docs calibration"; F="requirements.txt Dockerfile platform.toml.example"
cd $LOUISE
rm -rf src.new && mkdir src.new
(cd $TREE && tar --exclude='__pycache__' --exclude='*.pyc' -cf - $D deploy $F) | (cd src.new && tar -xf -)
rm -rf src.old && mkdir src.old
for d in $D; do chmod -R u+w src/$d 2>/dev/null || true; mv src/$d src.old/ 2>/dev/null || true; mv src.new/$d src/; done
for f in $F; do mv src/$f src.old/ 2>/dev/null || true; mv src.new/$f src/; done
cp -a src/deploy src.old/deploy && cp -a src.new/deploy/. src/deploy/ && rm -rf src.new/deploy && rmdir src.new
cp $TREE/.commit src/.deployed-commit
cd src && docker build -q -t louise-rag:local . && cd ..
timeout 1500 docker run --rm -v $LOUISE/src:/srv -w /srv -e PYTHONPATH=/srv --entrypoint python louise-rag:local \
  -m pytest tests -q -p no:cacheprovider --deselect tests/transfer/test_rsync_accounting.py::test_local_rsync_fetch_is_accounted 2>&1 | tail -1 | tee /tmp/15f-suite.txt
grep -q " passed" /tmp/15f-suite.txt && ! grep -qE "failed|error" /tmp/15f-suite.txt || { echo "suite did not pass"; exit 1; }

# ---- 6. Louise: TRANSLATE_API=completion, and the restart onto the new image ---------------
python3 - <<'P'
from truenas_api_client import Client
with Client() as c:
    cfg = c.call("app.config", "louise-rag")
    for svc in ("web", "worker"):
        cfg["services"][svc]["environment"]["TRANSLATE_API"] = "completion"
    c.call("app.update", "louise-rag", {"custom_compose_config": cfg}, job=True)
print("louise-rag updated")
P
for i in $(seq 1 60); do
  w=$(docker inspect ix-louise-rag-web-1 --format '{{.State.Status}}' 2>/dev/null || true)
  k=$(docker inspect ix-louise-rag-worker-1 --format '{{.State.Status}}' 2>/dev/null || true)
  [ "$w" = running ] && [ "$k" = running ] && break; sleep 5
done
WI=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}'); KI=$(docker inspect ix-louise-rag-worker-1 --format '{{.Image}}')
LI=$(docker inspect louise-rag:local --format '{{.Id}}')
echo "web $WI"; echo "worker $KI"; echo "local $LI"
[ "$WI" = "$LI" ] && [ "$KI" = "$LI" ] || { echo "web and worker do not both run the new image"; exit 1; }
docker exec ix-louise-rag-worker-1 python -c 'from app import config, node_state; print(config.TRANSLATE_API, node_state.worker_state())'
say "phase 1 done: Louise is paused on the new code, RAGE runs LocalAI"
