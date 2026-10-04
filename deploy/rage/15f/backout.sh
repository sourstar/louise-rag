#!/bin/bash
# Back out migration step 15f: today's RAGE (llama-swap, b10630-8) and today's
# Louise, from the backups phase1.sh took. Run as root on the node:
#   bash /mnt/ssd5/apps/louise-rage-15c/cutover/backout.sh
# The localai-models directory under RAGE's state is left in place; it is
# read by nothing once the old image runs.
set -euo pipefail
BK=/mnt/ssd5/apps/louise-rage-15c/cutover/backup
LOUISE=/mnt/ssd4/apps/louise-rag
echo "pausing Louise"
H=$(mktemp); chmod 600 $H; printf 'X-Admin-Token: %s\n' "$(cat /root/louise-token.txt)" > $H
curl -s -X POST -H @$H -H 'content-type: application/json' -d '{"state":"paused"}' http://127.0.0.1:30042/api/control || true
rm -f $H
cp -a $BK/rage.toml /mnt/ssd5/apps/louise-rage/config/rage.toml
cp -a $BK/pubmed.toml $LOUISE/config/collections/pubmed.toml
if [ -d $LOUISE/src.old/app ]; then
  cd $LOUISE
  for d in app tests tools docs calibration; do [ -d src.old/$d ] && { rm -rf src/$d; mv src.old/$d src/; }; done
  for f in requirements.txt Dockerfile platform.toml.example; do [ -e src.old/$f ] && mv -f src.old/$f src/; done
  [ -d src.old/deploy ] && { rm -rf src/deploy; mv src.old/deploy src/; }
fi
docker tag louise-rag:pre-15f louise-rag:local
python3 - <<'P'
import json
from truenas_api_client import Client
BK = "/mnt/ssd5/apps/louise-rage-15c/cutover/backup"
with Client() as c:
    c.call("app.update", "louise-rage", {"custom_compose_config": json.load(open(f"{BK}/louise-rage-app.json"))}, job=True)
    c.call("app.update", "louise-rag", {"custom_compose_config": json.load(open(f"{BK}/louise-rag-app.json"))}, job=True)
print("both apps restored")
P
echo "Louise stays PAUSED: resume it from the Library (run everything) once RAGE answers."
