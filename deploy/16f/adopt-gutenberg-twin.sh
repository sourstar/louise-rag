#!/usr/bin/env bash
# adopt-gutenberg-twin.sh — a PEER takes the node's gutenberg twin (A1f: the
# same records, the same document vectors, Qwen3's query instruction) with
# nothing downloaded or computed again (RULE 1). For Perugia, relayed by the
# review session; the node itself ran repin-gutenberg-instruction.sh.
#   fetch      the twin's Codex and the node's SIGNED snapshot chain only
#              (`share fetch` without --here; the node held in seed-only mode,
#              so the worker pulls no member meanwhile)
#   seed       `hold snapshot create --shards-from gutenberg`: every part linked
#              from this peer's own gutenberg SHARD, checked by pieces root;
#              the node's snapshot is identical bytes, so it is KEPT and this
#              peer's signature joins its sidecar
#   carry      manifest, index/, lance/, state/bundles, state/share moved — the
#              twin admits gutenberg's space (03 §3.5, A1f)
#   deprecate  this peer's duplicate statements re-made for the twin
#   rename     gutenberg aside, the twin → gutenberg, the old file's settings
# Usage, on the peer, its deployed code at or past the node's tip:
#   TWIN=sha256:… NODE_URL=https://… W=<worker container> bash adopt-gutenberg-twin.sh
# Resumable: run it again after a stop.
set -euo pipefail

: "${TWIN:?the twin's collection_id, as the node published it}"
: "${NODE_URL:?the node's address for share fetch --from}"
: "${W:?this peer's worker container}"
L="docker exec $W python -m app.louise"
OLD=gutenberg
NEW=gutenberg-qi
RETIRED=gutenberg-q0-retired
TODAY=$(date +%Y%m%d)
in_w() { docker exec "$W" sh -c "$1"; }

say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1) && rc=0 || rc=$?; echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out" | grep -v warn:config; return $rc; }

say "0. this peer runs the tip: A1f's carry and the admitted spaces"
docker exec "$W" python -c "
import inspect
from app.hold import carry, codex
assert 'A1f' in inspect.getsource(carry.carry) and hasattr(codex.Codex, 'admitted_spaces')
print('yes')"

MODE=$(docker exec "$W" python -c "from app import node_state; print(node_state.peek_worker_state())" 2>/dev/null | tail -1)
[ -n "$MODE" ] || { echo "the node's mode could not be read; nothing proceeds"; exit 1; }
echo "node mode now: $MODE (restored at the end)"
restore_mode() {
  case "$MODE" in
    running) $L node resume ;;
    seed-only) $L node pause --seed-only ;;
    acquire-only) $L node pause --acquire-only ;;
    *) $L node pause ;;
  esac >/dev/null
}

if in_w "test -e /config/collections/$OLD.toml.superseded-$TODAY"; then
  say "already renamed: resuming at 8b"
else

say "1. seed-only while the twin arrives: peers still served, nothing pulled; $OLD paused"
$L node pause --seed-only >/dev/null
$L node pause "$OLD" >/dev/null || true

say "2. the twin's Codex and the node's signed snapshots — no member"
if ! in_w "test -d /data/collections/$NEW"; then
  $L share fetch "$TWIN" --name "$NEW" --kinds records,vectors --from "$NODE_URL" | grep -v warn:config
fi
$L node pause "$NEW" >/dev/null || true
in_w "ls /data/collections/$NEW/shard/snapshots/"

say "3. the origin, where $OLD has one: the same folder"
OT=$(in_w "readlink /data/collections/$OLD/origin 2>/dev/null || true")
if [ -n "$OT" ]; then in_w "ln -sfn '$OT' /data/collections/$NEW/origin"; fi

say "4. the parts from this peer's own $OLD: linked, the node's snapshot kept"
run hold records validation "$NEW" || true
run hold snapshot create "$NEW" --shards-from "$OLD"

say "5. carry: the dry run must name state/bundles and state/share"
DRY=$(run dev carry "$OLD" "$NEW")
echo "$DRY"
echo "$DRY" | grep -q "state/bundles and state/share" || {
  echo "the twin does not admit $OLD's space here; nothing proceeds"; restore_mode; exit 1; }
run dev carry "$OLD" "$NEW" --yes

say "6. this peer's duplicates deprecated for the twin, before it is shared"
run dev deprecate-duplicates "$NEW" --yes

say "7. the A1 proof, before use"
run hold check "$NEW" --checks vectors-sample

say "8a. $OLD aside, its held file set aside, nothing deleted"
run hold collection rename "$OLD" "$RETIRED"
in_w "cp -p /config/collections/$RETIRED.toml /config/$RETIRED.toml.kept-$TODAY && mv /config/collections/$RETIRED.toml /config/collections/$OLD.toml.superseded-$TODAY"
fi

say "8b. $NEW → $OLD"
if in_w "test -e /config/collections/$NEW.toml"; then
  run hold collection rename "$NEW" "$OLD"
fi

say "9. the held file: the old one's settings, the twin's Codex"
in_w "cp -p /config/collections/$OLD.toml /config/$OLD.toml.fetched-$TODAY && sed 's|^codex = \".*\"|codex = \"$TWIN\"|' /config/collections/$OLD.toml.superseded-$TODAY > /config/collections/$OLD.toml.tmp && mv /config/collections/$OLD.toml.tmp /config/collections/$OLD.toml && cat /config/collections/$OLD.toml"

say "10. resume $OLD, and the node's mode as it was ($MODE)"
$L node resume "$OLD" >/dev/null || true
restore_mode
run hold collection show "$OLD" | head -20
