#!/usr/bin/env bash
# adopt-gutenberg-p10.sh — a PEER takes the node's parser-10 gutenberg (6f030405,
# the same space as the d913cceb it holds, its parts re-cut, 673 refused, ~133
# books revised upstream) with nothing embedded or fetched again that it
# already holds (RULE 1; 04 §4.5 as amended 2026-10-22, coverage by possession).
#   fetch      the new Codex, the node's signed snapshot chain and the records
#              parts (`share fetch --here --kinds records`): the parts are
#              re-cut, so `--shards-from` cannot link them
#   carry      --rebuilt: manifest, index/, lance/, state/bundles and the
#              coverage rows moved; the d913 rows stay under their own part
#              ids (provenance) and count as coverage by possession
#   reconcile  every document whose content_hash did not move keeps its
#              vectors; the revised and the refused lose theirs
#   673        out of the text index and the catalogue at once
#   check      vectors-sample, before use
#   rename     gutenberg aside, the new one → gutenberg, the old file's settings
# Then the worker fetches only what no held bundle covers: a held d913 bundle
# is skipped by its id (accept.listed_bundles), and the node's digest offers
# its d913 bundles under the new parts (gossip.coverage_of).
# Usage, on the peer, its deployed code at or past the node's tip:
#   NEW_ID=sha256:6f03… PRED_ID=sha256:d913… NODE_URL=https://… W=<worker container> \
#     bash adopt-gutenberg-p10.sh
# Resumable: run it again after a stop.
set -euo pipefail

: "${NEW_ID:?the collection_id of parser-10 gutenberg, as the node published it}"
: "${PRED_ID:?the collection_id of the gutenberg this peer holds (d913…)}"
: "${NODE_URL:?the address of the node, for share fetch --from}"
: "${W:?the worker container of this peer}"
L="docker exec $W python -m app.louise"
OLD=gutenberg
NEW=gutenberg10
RETIRED=gutenberg-p9-retired
TODAY=$(date +%Y%m%d)
in_w() { docker exec "$W" sh -c "$1"; }

say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1) && rc=0 || rc=$?; echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out" | grep -v warn:config; return $rc; }

say "0. this peer runs the tip: coverage by possession, records by uid"
docker exec "$W" python -c "
from app.share import contribute
from app.hold import land
assert hasattr(contribute, 'add_predecessor') and hasattr(land, 'records_by_uid')
print('yes')"

MODE=$(docker exec "$W" python -c "from app import node_state; print(node_state.peek_worker_state())" 2>/dev/null | tail -1)
[ -n "$MODE" ] || { echo "the node's mode could not be read; nothing proceeds"; exit 1; }
echo "node mode now: $MODE (restored at the end)"
RESTORED=
restore_mode() {
  case "$MODE" in
    running) $L node resume ;;
    seed-only) $L node pause --seed-only ;;
    acquire-only) $L node pause --acquire-only ;;
    *) $L node pause ;;
  esac >/dev/null
  RESTORED=1
}
on_exit() {
  rc=$?
  if [ -z "$RESTORED" ]; then
    echo "stopped (exit $rc): the node's mode restored ($MODE); $OLD and $NEW stay paused — run again"
    restore_mode || true
  fi
  exit $rc
}
trap on_exit EXIT

if in_w "test -e /config/collections/$OLD.toml.superseded-$TODAY"; then
  say "already renamed: resuming at 7b"
else

say "1. seed-only while it arrives: peers still served, nothing pulled; $OLD paused"
$L node pause --seed-only >/dev/null
$L node pause "$OLD" >/dev/null || true
HELD=$(docker exec "$W" python -c "
from app.hold import codex as cx
print(cx.loads(open('/data/collections/$OLD/shard/codex.json', 'rb').read()).collection_id)")
[ "$HELD" = "$PRED_ID" ] || { echo "$OLD here is $HELD, not $PRED_ID; nothing proceeds"; exit 1; }

say "2. the new Codex, the node's signed snapshots and the records parts"
if ! in_w "test -d /data/collections/$NEW"; then
  $L share fetch "$NEW_ID" --name "$NEW" --kinds records --from "$NODE_URL" | grep -v warn:config
fi
$L node pause "$NEW" >/dev/null || true
run share fetch "$NEW" --kinds records --here --from "$NODE_URL"
docker exec "$W" python -c "
from app.hold import codex as cx
o = cx.loads(open('/data/collections/$OLD/shard/codex.json', 'rb').read())
n = cx.loads(open('/data/collections/$NEW/shard/codex.json', 'rb').read())
assert n.space_id == o.space_id, 'the space moved: nothing carries'
assert n.parser.version == 10, n.parser.version
print('same space', n.space_id[:19], '| parser', o.parser.version, '->', n.parser.version)"
in_w "ls /data/collections/$NEW/shard/snapshots/"

say "3. the origin, where $OLD has one: the same folder"
OT=$(in_w "readlink /data/collections/$OLD/origin 2>/dev/null || true")
if [ -n "$OT" ]; then in_w "ln -sfn '$OT' /data/collections/$NEW/origin"; fi

say "4. carry --rebuilt (dry run first)"
if ! in_w "grep -q '\"done\": *true' /data/collections/$NEW/state/carry.json 2>/dev/null"; then
  run dev carry "$OLD" "$NEW" --rebuilt
  run dev carry "$OLD" "$NEW" --rebuilt --yes
fi
in_w "cat /data/collections/$NEW/state/carry.json"; echo

say "5. reconcile: unchanged documents keep their vectors; then 673 out of every index"
run dev carry "$OLD" "$NEW" --reconcile --yes
run dev unindexable "$NEW" gutenberg:673 --yes

say "6. the proof, before use"
run hold check "$NEW" --checks vectors-sample

say "7a. $OLD aside, its held file set aside, nothing deleted"
run hold collection rename "$OLD" "$RETIRED"
in_w "cp -p /config/collections/$RETIRED.toml /config/$RETIRED.toml.kept-$TODAY && mv /config/collections/$RETIRED.toml /config/collections/$OLD.toml.superseded-$TODAY"
fi

say "7b. $NEW → $OLD"
if in_w "test -e /config/collections/$NEW.toml"; then
  run hold collection rename "$NEW" "$OLD"
fi

say "8. the held file: the old one's settings, the new Codex"
in_w "cp -p /config/collections/$OLD.toml /config/$OLD.toml.fetched-$TODAY && sed 's|^codex = \".*\"|codex = \"$NEW_ID\"|' /config/collections/$OLD.toml.superseded-$TODAY > /config/collections/$OLD.toml.tmp && mv /config/collections/$OLD.toml.tmp /config/collections/$OLD.toml && cat /config/collections/$OLD.toml"

say "9. the numbers: documents, embedded, covered by possession"
docker exec -i -w /srv -e PYTHONPATH=/srv "$W" python - <<'PY'
from pathlib import Path
from app import db
from app.hold import collection as col, records as rec
from app.share import contribute, possession
root = Path("/data/collections")
name, codex = col.resolve(root, "gutenberg")
con = db.connect(rec.local_collection(root, name, codex))
print("documents", con.execute("select count(*) from documents").fetchone()[0],
      "| embedded", con.execute("select count(*) from documents where embed_done=1").fetchone()[0],
      "| errors", con.execute("select count(*) from documents where error is not null").fetchone()[0])
print("predecessors", contribute.predecessors(root, name))
u = possession.covered(root, name, codex, con)
print("parts", len(u), "| covered chunks", sum(u.values()), "| parts with none", sum(1 for v in u.values() if not v))
PY

say "10. resume $OLD, and the node's mode as it was ($MODE)"
$L node resume "$OLD" >/dev/null || true
restore_mode
run hold collection show "$OLD" | head -20
