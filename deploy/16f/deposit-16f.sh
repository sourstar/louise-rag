#!/usr/bin/env bash
# deposit-16f.sh — deposit collections on the node, step 16f (docs/02 §2.9,
# 07 §7.4/§7.8): granite-embedding-107m-multilingual on RAGE, the first deposit
# collection, the token for LocalAGI's routes. Operator's decision 2026-10-06.
#
# RUN AFTER deploy-cb419e7.sh (Louise at 3edbabb). The node has no internet
# egress today, so the weights come through the workstation:
#
#   1. On the workstation (221 MB; cmd.exe, no single quotes) — already done on
#      2026-10-06, the file is in the session's scratchpad; otherwise:
#        curl -L -o granite-embedding-107m-multilingual-f16.gguf https://huggingface.co/bartowski/granite-embedding-107m-multilingual-GGUF/resolve/main/granite-embedding-107m-multilingual-f16.gguf
#        certutil -hashfile granite-embedding-107m-multilingual-f16.gguf SHA256
#      Expected: 220,974,080 bytes, sha256 3fc99928632fcecad589c401ec33bbba86b51c457e9813e3a1cb801ff4106e21
#      (phase 1 checks both; phase 3 checks that RAGE hashed exactly this file).
#   2. scp -i C:\Users\alana\.ssh\truenas_root granite-embedding-107m-multilingual-f16.gguf root@192.168.178.10:/tmp/
#   3. ssh -i C:\Users\alana\.ssh\truenas_root root@192.168.178.10 bash -s < deploy\16f\deposit-16f.sh
#
# Phases 1–2 touch RAGE (one app.update: a new read-only volume, LocalAI
# restarts and loads every model again — the running collections pause for
# the restart, as at any RAGE update). Phases 3–5 touch Louise (platform.toml,
# a token file, one app.update for the env, one new collection). It stops at
# the first failure; every phase is safe to run again.
set -euo pipefail

LOUISE=/mnt/ssd4/apps/louise-rag
RAGE=/mnt/ssd5/apps/louise-rage
RAGE_URL=http://127.0.0.1:30437
MODEL=granite-embedding-107m-multilingual
GGUF=granite-embedding-107m-multilingual-f16.gguf
WEIGHTS_DIR=/mnt/ssd5/apps/granite-models
ENCODER_URL="http://192.168.178.10:30437/upstream/$MODEL"
FIRST=deposit-granite            # the first deposit collection: holds the space the others copy

say() { printf '\n=== %s\n' "$*"; }

# ------------------------------------------------------------ 1. the weights, placed
say "1. the weights, placed (rage.md §10: never fetched)"
mkdir -p "$WEIGHTS_DIR"
if [ ! -s "$WEIGHTS_DIR/$GGUF" ]; then
  test -s "/tmp/$GGUF" || { echo "no /tmp/$GGUF: scp it first (see the header)"; exit 1; }
  mv "/tmp/$GGUF" "$WEIGHTS_DIR/$GGUF"
fi
chmod 444 "$WEIGHTS_DIR/$GGUF"
SIZE=$(stat -c %s "$WEIGHTS_DIR/$GGUF")
[ "$SIZE" = 220974080 ] || { echo "$GGUF is $SIZE bytes, expected 220974080"; exit 1; }
FILE_SHA=$(sha256sum "$WEIGHTS_DIR/$GGUF" | cut -d' ' -f1)
EXPECTED_SHA=3fc99928632fcecad589c401ec33bbba86b51c457e9813e3a1cb801ff4106e21   # hashed on the workstation, 2026-10-06
[ "$FILE_SHA" = "$EXPECTED_SHA" ] || { echo "$GGUF: sha256 $FILE_SHA, expected $EXPECTED_SHA"; exit 1; }
echo "weights: $WEIGHTS_DIR/$GGUF sha256:$FILE_SHA"

# ------------------------------------------------------------ 2. RAGE serves it
say "2. RAGE: the model YAML, the weights mount, LocalAI reloaded"
mkdir -p "$RAGE/state/localai-models"
# the operator's YAML, byte for byte deploy/rage/localai-models/$MODEL.yaml at 3edbabb
# (no first-line marker: RAGE's API never rewrites it)
cat > "$RAGE/state/localai-models/$MODEL.yaml" <<'YAML'
# granite-embedding-107m-multilingual — LocalAI's model YAML, the operator's
# (migration step 16f; docs/02 §2.9: the encoder every deposit collection is
# created with unless platform.toml names another — LocalAGI's own default,
# chosen by the operator on 2026-10-06 over bge-m3 and qwen3-embedding-4b).
# Copied once into LocalAI's models directory
# (/mnt/ssd5/apps/louise-rage/state/localai-models/) by the deposit runbook; no
# first-line marker, so RAGE's API never rewrites it (409): edit it by hand.
# JSON, which is YAML: one parser for LocalAI and for tests/rage/test_localai_deploy.py.
# EVERY OPTION LocalAI WOULD FILL IN BY ITSELF IS PINNED (rage-over-localai
# §10): context_size, threads, gpu_layers, parameters.batch and seed, and
# options parallel — as bge-m3.yaml pins them, which is the same architecture
# (XLM-RoBERTa, CLS pooling). The model: ibm-granite/granite-embedding-107m-
# multilingual, Apache-2.0, 384 dimensions, CLS pooling, 512-token window
# (max_position_embeddings 514), as bartowski's f16 GGUF (220,974,080 bytes).
# The weights are placed, never fetched (rage.md §10): the operator puts the
# file at /mnt/ssd5/apps/granite-models/, mounted read-only at weights/granite.
{
  "name": "granite-embedding-107m-multilingual",
  "backend": "llama-cpp",
  "embeddings": true,
  "context_size": 512,
  "threads": 6,
  "gpu_layers": 999,
  "flash_attention": "on",
  "mmap": true,
  "parameters": {
    "model": "weights/granite/granite-embedding-107m-multilingual-f16.gguf",
    "batch": 512,
    "seed": 0
  },
  "options": [
    "parallel:1",
    "pooling:cls",
    "ubatch:512"
  ],
  "env": {
    "NEO_CACHE_DIR": "/state/neo-cache"
  }
}
YAML
ls -la "$RAGE/state/localai-models/$MODEL.yaml"

# the volume, added once (TrueNAS keeps the app definition; env is in it — never printed)
python3 - "$WEIGHTS_DIR" <<'P'
import sys
from truenas_api_client import Client
weights = sys.argv[1]
vol = f"{weights}:/state/localai-models/weights/granite:ro"
with Client() as c:
    cfg = c.call("app.config", "louise-rage")
    s = cfg["services"]["rage"]
    if vol in s["volumes"]:
        print("louise-rage: the granite volume is already there; no update")
    else:
        s["volumes"].append(vol)
        c.call("app.update", "louise-rage", {"custom_compose_config": cfg}, job=True)
        print("louise-rage updated: +", vol)
P
for i in $(seq 1 60); do
  [ "$(docker inspect ix-louise-rage-rage-1 --format '{{.State.Health.Status}}' 2>/dev/null)" = healthy ] && break; sleep 5
done
docker inspect ix-louise-rage-rage-1 --format '{{.Config.Image}} {{.State.Health.Status}}'
curl -sf "$RAGE_URL/louise/models" | python3 -c "
import sys, json
rows = json.load(sys.stdin)
names = [r.get('model') or r.get('id') or r.get('name') for r in rows]
print('models:', names)
assert any('$MODEL' in str(r) for r in rows), 'granite is not listed by RAGE'
"

# ------------------------------------------------------------ 3. the space file, from the run RAGE states
say "3. one embedding through RAGE; its run becomes the space file's encoder block"
HDR=$(mktemp)
# --compressed: RAGE hands LocalAI's gzip body through (found 2026-10-06)
curl -sS --compressed -D "$HDR" -o /tmp/granite-first.json --max-time 300 \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"input\":[\"Il deposito è la memoria di un agente.\"]}" \
  "$RAGE_URL/upstream/$MODEL/v1/embeddings"
RUN=$(grep -i '^Louise-Run:' "$HDR" | tr -d '\r' | awk '{print $2}')
[ -n "$RUN" ] || { echo "no Louise-Run header: RAGE stated no run"; cat "$HDR"; head -c 600 /tmp/granite-first.json; exit 1; }
echo "run: $RUN"
python3 -c "
import json, gzip
raw = open('/tmp/granite-first.json', 'rb').read()
if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
d = json.loads(raw)
v = d['data'][0]['embedding']; print('vector dim:', len(v)); assert len(v) == 384
"
curl -sf --compressed "$RAGE_URL/louise/runs/$RUN" > /tmp/granite-run.json
python3 - "$FILE_SHA" "$LOUISE/config/deposit-space.json" <<'P'
import json, sys
file_sha, out = sys.argv[1], sys.argv[2]
run = json.load(open("/tmp/granite-run.json"))
engine, weights = run["engine"], run["weights"]
assert weights == "sha256:" + file_sha, f"RAGE hashed {weights}, the file is sha256:{file_sha}"
# the calibration set's id, from the image this node runs (app/hold/calibration.py)
import subprocess
set_id = subprocess.check_output(
    ["docker", "run", "--rm", "-e", "PYTHONPATH=/srv", "louise-rag:local", "python", "-c",
     "from app.hold import calibration as c; print(c.load('text')[0])"], text=True).strip()
assert set_id.startswith("sha256:"), set_id
space = {
    "encoder": {"weights": weights, "label": "granite-embedding-107m-multilingual f16 (LocalAI, the node's card)",
                "engine": engine},
    "dim": 384, "dtype": "f16", "ctx": 512, "pooling": "cls", "normalise": True,
    "query": {"encoder": None, "instruction": None},       # symmetric: one encoder, no prefix
    # 03 §3.1: the same weights on another build land above 0.9995, a different
    # model below 0.7; 0.995 leaves a driver or a batch size room and admits no
    # other model. Not calibrated against a second engine (none runs granite);
    # a sister can tighten it when `hold embedder calibrate` has a second build.
    "tolerance": "0.995",
    "calibration": {"set": set_id, "quorum": {"n": 16, "min_pass": 12}},
    "epoch": "0", "evaluation": None,
}
json.dump({"space": space}, open(out, "w"), indent=2, sort_keys=True)
print("wrote", out)
print("engine:", engine.get("name"), engine.get("build"), engine.get("device"))
print("calibration set:", set_id)
P
chmod 644 "$LOUISE/config/deposit-space.json"

# ------------------------------------------------------------ 4. Louise's config: [deposit], the token
say "4. platform.toml [deposit], the deposit token file, the env for the routes"
P=$LOUISE/config/platform.toml
cp -a "$P" "$P.bak-$(date +%Y%m%d-%H%M%S)"
if grep -q '^\[deposit\]' "$P"; then
  echo "platform.toml already has [deposit]:"; grep -n -A3 '^\[deposit\]' "$P"
else
  printf '\n[deposit]\nencoder = "%s"\nspace = "/config/deposit-space.json"\n' "$ENCODER_URL" >> "$P"
  echo "platform.toml: [deposit] added"; tail -4 "$P"
fi
TOK=$LOUISE/config/deposit.token
if [ ! -s "$TOK" ]; then
  umask 077; openssl rand -hex 24 > "$TOK"; umask 022
  echo "deposit token written: $TOK (never printed; LocalAGI's local_rag_api_key is its content)"
else
  echo "deposit token kept: $TOK"
fi
chmod 600 "$TOK"
python3 - <<'P'
from truenas_api_client import Client
with Client() as c:
    cfg = c.call("app.config", "louise-rag")
    changed = False
    for svc in ("web", "worker"):
        env = cfg["services"][svc]["environment"]
        if env.get("DEPOSIT_TOKEN_FILE") != "/config/deposit.token":
            env["DEPOSIT_TOKEN_FILE"] = "/config/deposit.token"; changed = True
    if changed:
        c.call("app.update", "louise-rag", {"custom_compose_config": cfg}, job=True)
        print("louise-rag updated: DEPOSIT_TOKEN_FILE on web and worker (restarted)")
    else:
        print("louise-rag: DEPOSIT_TOKEN_FILE already set; no update")
P
for i in $(seq 1 90); do
  s=$(midclt call app.query '[["name","=","louise-rag"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = "RUNNING" ] && break; sleep 2
done
echo "louise-rag state: $s"; [ "$s" = "RUNNING" ]
sleep 10
WEB=$(docker inspect ix-louise-rag-web-1 --format '{{.Image}}'); WRK=$(docker inspect ix-louise-rag-worker-1 --format '{{.Image}}')
[ "$WEB" = "$WRK" ] || { echo "web and worker run different images"; exit 1; }

# ------------------------------------------------------------ 5. the first deposit collection
say "5. the first deposit collection: $FIRST (embeds the calibration set once through granite)"
if [ -s "$LOUISE/config/collections/$FIRST.toml" ]; then
  echo "$FIRST is held already"
else
  docker exec -e PYTHONUNBUFFERED=1 ix-louise-rag-worker-1 \
    python -m app.louise hold collection create deposit --name "$FIRST"
fi
echo "--- its file:"; cat "$LOUISE/config/collections/$FIRST.toml"
echo "--- its state (the worker gates the run on its next round; quarantine until then):"
for i in $(seq 1 30); do
  STATE=$(docker exec ix-louise-rag-worker-1 python -m app.louise hold collection show "$FIRST" 2>/dev/null \
          | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['state'], '|', '; '.join(d.get('reasons') or []))" || echo "unknown")
  echo "  $STATE"
  case "$STATE" in in-use*) break;; esac
  sleep 20
done
case "$STATE" in in-use*) ;; *) echo "not in use yet: watch 'hold collection show $FIRST' and the worker log"; exit 1;; esac

say "DONE. What 16f needs from here (the operator's stop)"
echo "Louise's LocalAGI routes answer at the web port, with the token in $TOK:"
docker port ix-louise-rag-web-1 2>/dev/null || true
cat <<'EOF'
- LocalAGI: set LOCALAGI_LOCALRAG_URL=http://192.168.178.10:<web port> for the pool
  (or local_rag_url / local_rag_api_key on one agent), the key being the
  token file's content. Each agent gets a collection named after itself,
  created on granite by copying deposit-granite's space.
- The contract check, for real, from a machine with Go and a LocalAGI
  checkout (7e0947d): tools/localagi_check/run.sh <checkout> http://192.168.178.10:<web port> <token file> [source-url]
- Then the stop itself: memory stored, found, compacted; a file uploaded from
  LocalAGI's UI; one URL source (needs `hold collection ack --web-sources`
  for the disclaimer first). Numbers go in STATUS.md.
EOF
