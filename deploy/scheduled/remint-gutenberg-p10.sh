#!/usr/bin/env bash
# remint-gutenberg-p10.sh — gutenberg on parser 10 (operator, 2026-10-22: "Drop
# 673 and use the same-title rule"; "unless the book formatting is broken, the
# embedding stays"). A1c, as parser 9 ran on 2026-10-11 (docs/notes/
# v5-migration.md), RULE 1 throughout:
#   recipe     the live Codex's, byte for byte, with parser 10 and the deployed image
#   create     --calibration-from: nothing embedded again
#   carry      --rebuilt --copy: manifest, index/, lance/ reflinked; the old
#              collection untouched until the switch
#   build      records from the same origin (parser 10: 673 unindexable)
#   reconcile  every document whose content_hash did not move keeps its vectors
#   check      every check, before use
# It STOPS there: the switch, the editions collection and the shares are run
# after the numbers are read (remint-gutenberg-p10-switch.sh).
# Run on the node: bash remint-gutenberg-p10.sh  (resumable: run it again after a stop)
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
W=ix-louise-rag-worker-1
L="nice -n 19 docker exec $W python -m app.louise"
OLD=gutenberg
NEW=gutenberg10
ENCODER=http://192.168.178.10:30286
MODEL=vega-qwen3-embedding-4b

say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1) && rc=0 || rc=$?; echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out" | grep -v warn:config; return $rc; }

say "0. the tip is live: parser 10, the sort-merge keep"
cat "$ROOT/src/.deployed-commit"
docker exec "$W" python -c "
import inspect
from app.plugins.gutenberg.source import GutenbergSource
from app.hold import carry
assert GutenbergSource.PARSER_VERSION == 10, GutenbergSource.PARSER_VERSION
assert 'carry-keep-runs' in inspect.getsource(carry.keep_producer_scan)
print('parser 10, sort-merge keep: yes')"
IMAGE=$(docker exec "$W" printenv LOUISE_PLATFORM_IMAGE)
echo "image $IMAGE"

say "1. pause $OLD: nothing embeds into the table that is copied"
$L node pause "$OLD" | grep -v warn:config || true

say "2. the recipe: the live Codex's, parser 10, the deployed image"
REC="$ROOT/config/recipes/$NEW.json"
if [ ! -e "$REC" ]; then
  docker exec -w /srv "$W" python -m tools.recipe_from_codex /data/collections/$OLD/shard/codex.json /tmp/$NEW-p9.json
  docker exec -i -w /srv "$W" python - "$IMAGE" <<'PY'
import json, sys
r = json.load(open("/tmp/gutenberg10-p9.json"))
assert r["parser"] == {"id": "gutenberg", "version": 9}, r["parser"]
r["parser"]["version"] = 10
r["toolchain"]["platform_image"] = sys.argv[1]
json.dump(r, open("/tmp/gutenberg10.json", "w"), ensure_ascii=False, indent=1, sort_keys=True)
print("recipe: parser 10, image", sys.argv[1][:19])
PY
  docker cp "$W:/tmp/$NEW.json" "$REC"
fi
grep -n '"version": 10' "$REC" | head -3

say "3. create $NEW — calibration reused — then pause it"
if [ ! -e "$ROOT/config/collections/$NEW.toml" ]; then
  run hold collection create gutenberg --recipe "/config/recipes/$NEW.json" --name "$NEW" \
    --encoder "$ENCODER" --api vega --embed-model "$MODEL" \
    --query-encoder "$ENCODER" --query-model "$MODEL" --calibration-from "$OLD"
fi
$L node pause "$NEW" | grep -v warn:config || true
docker exec "$W" python -c "
from app.hold import codex as cx
o = cx.loads(open('/data/collections/$OLD/shard/codex.json', 'rb').read())
n = cx.loads(open('/data/collections/$NEW/shard/codex.json', 'rb').read())
print('old', o.collection_id, 'parser', o.parser.version, 'space', o.space_id[:19])
print('new', n.collection_id, 'parser', n.parser.version, 'space', n.space_id[:19])
assert n.space_id == o.space_id, 'the space moved: nothing carries'
assert n.parser.version == 10"

say "4. its origin is $OLD's (the same books, never fetched again)"
O="$ROOT/data/collections/$NEW/origin"
TARGET=$(readlink "$ROOT/data/collections/$OLD/origin")
if [ "$(readlink "$O" || true)" != "$TARGET" ]; then
  EMPTY=$(docker exec "$W" sh -c "ls -A /corpora/$NEW 2>/dev/null | head -1")
  [ -z "$EMPTY" ] || { echo "/corpora/$NEW holds files: not ours to remove"; exit 1; }
  docker exec "$W" sh -c "rmdir /corpora/$NEW 2>/dev/null || true"
  ln -sfn "$TARGET" "$O"
fi
ls -la "$ROOT/data/collections/$NEW/"

say "5. carry --rebuilt --copy (dry run first)"
if ! docker exec "$W" test -e /data/collections/$NEW/state/carry.json; then
  run dev carry "$OLD" "$NEW" --rebuilt --copy
  run dev carry "$OLD" "$NEW" --rebuilt --copy --yes
fi
docker exec "$W" cat /data/collections/$NEW/state/carry.json; echo

say "6. records build from the same origin (parser 10)"
run hold records build "$NEW" --corpus /data/collections/$OLD/origin
say "7. snapshot"
run hold snapshot create "$NEW"
say "8. reconcile: unchanged documents keep their vectors"
run dev carry "$OLD" "$NEW" --reconcile --yes
say "9. every check, before use"
run hold check "$NEW" || true
say "10. the numbers"
docker exec -i "$W" python - <<'PY'
import sqlite3
for n in ("gutenberg", "gutenberg10"):
    con = sqlite3.connect(f"file:/data/collections/{n}/state/manifest.sqlite?mode=ro", uri=True)
    print(n, "documents", con.execute("select count(*) from documents").fetchone()[0],
          "in scope", con.execute("select count(*) from documents where in_scope=1").fetchone()[0],
          "embedded", con.execute("select count(*) from documents where embed_done=1").fetchone()[0],
          "errors", con.execute("select count(*) from documents where error is not null").fetchone()[0])
    print("   673:", con.execute("select in_scope, embed_done, substr(error, 1, 120) from documents where uid='gutenberg:673'").fetchone())
PY
echo "REMINT STOPPED BEFORE THE SWITCH: read the numbers, then remint-gutenberg-p10-switch.sh"
