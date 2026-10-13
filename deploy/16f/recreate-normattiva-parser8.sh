#!/usr/bin/env bash
# recreate-normattiva-parser8.sh — `normattiva` without the 1948 floor
# (operator, 2026-10-13: "I want everything"; parser 8). RULE 1 throughout:
# nothing is re-fetched (the 23 zips on disk are the origin), and the
# 318,349 versions parser 7 already parsed, indexed and embedded keep all of
# it — A1c, as gutenberg's parser 9 on 2026-10-11 (docs/notes/v5-migration.md):
#   carry --rebuilt --copy   manifest, text index, vector table into the new collection
#   records build, snapshot  the parts, cut fresh (old slices gain older versions)
#   carry --reconcile        every document whose content_hash did not move keeps
#                            its vectors; the 23,610 new versions are embedded
# Run on the node:  ssh root@node bash -s < deploy/16f/recreate-normattiva-parser8.sh
#
# ORDER MATTERS. Once the parser-8 code is live, `normattiva` (Codex: parser 7)
# would discover the older versions itself at its next pass, and its build
# refuses them (02 §2.3) — so step 1 pauses it, right after the deploy, and it
# stays paused: it is superseded. Removing it (its index/, lance/, state/) is
# the operator's call, after normattiva-1861 is checked; nothing here deletes.
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
OLD=normattiva
NEW=normattiva-1861
ENCODER=http://192.168.178.10:30286
MODEL=vega-dar-legal-it

say() { printf '\n=== %s\n' "$*"; }

say "0. parser 8 is live"
cat "$ROOT/src/.deployed-commit"
docker exec "$W" python -c "from app.plugins.normattiva import SOURCE; assert SOURCE.PARSER_VERSION == 8, SOURCE.PARSER_VERSION; print('parser', SOURCE.PARSER_VERSION)"

say "1. pause $OLD: it must not discover with parser 8 (it stays paused; superseded)"
$L node pause "$OLD"
$L node show | grep -i "$OLD" || true

say "2. create $NEW from the same recipe (only the parser differs: A1c)"
test ! -e "$ROOT/config/collections/$NEW.toml" || { echo "$NEW.toml exists: resume from step 3"; }
if [ ! -e "$ROOT/config/collections/$NEW.toml" ]; then
  mkdir -p "$ROOT/config/recipes"
  cp "$ROOT/src/docs/notes/normattiva-recipe.toml" "$ROOT/config/recipes/$NEW.toml"
  $L hold collection create normattiva --recipe "/config/recipes/$NEW.toml" --name "$NEW" \
    --encoder "$ENCODER" --api vega --embed-model "$MODEL" --query-model "$MODEL"
  $L hold collection ack "$NEW" || true
  $L hold collection ack "$NEW" --yes
fi
# THE ORIGIN IS THE OLD ONE'S: the same zips, linked, never copied or
# fetched again (RULE 1). `create` linked origin/ to an empty /corpora/$NEW;
# it points at /corpora/$OLD instead, and the empty folder goes. Discovery
# sees the zips under the new path as RELOCATED, not changed
# (app/discovery.py), and the first acquisition pass adopts every zip by
# its central directory (a few MB of Range reads, no rewrite).
O="$ROOT/data/collections/$NEW/origin"
if [ "$(readlink "$O")" != "/corpora/$OLD" ]; then
  EMPTY=$(docker exec "$W" sh -c "ls -A /corpora/$NEW 2>/dev/null | head -1")
  [ -z "$EMPTY" ] || { echo "/corpora/$NEW holds files: not ours to remove"; exit 1; }
  docker exec "$W" sh -c "rmdir /corpora/$NEW 2>/dev/null || true"
  ln -sfn "/corpora/$OLD" "$O"
fi
ls -la "$ROOT/data/collections/$NEW/"
docker exec "$W" sh -c "ls /data/collections/$NEW/origin/akn/M | wc -l"   # 22

say "3. carry: the dry run, then the copy (both locks; resumable)"
$L dev carry "$OLD" "$NEW" --rebuilt --copy
$L dev carry "$OLD" "$NEW" --rebuilt --copy --yes

say "4. the records, with parser 8, and the snapshot (about twenty minutes)"
# the worker adopts the new collection within a round and holds its lock:
# pause it for the writes below, retry while the lock is released, resume at 7
$L node pause "$NEW" >/dev/null
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1); echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out"; }
run hold records build "$NEW"
run hold records validation "$NEW"          # 09 §9.4: every snapshot lists it
run hold snapshot create "$NEW"

say "5. reconcile: vectors kept where the content did not move"
run dev carry "$OLD" "$NEW" --reconcile --yes

say "6. the A1c proof"
$L hold check "$NEW" --checks vectors-sample

say "7. the collection file: as $OLD's — everything, embedding on, the GPU badge"
F="$ROOT/config/collections/$NEW.toml"
sed -i 's/^slices = \[\]$/slices = "all"/' "$F"
sed -i 's/^embed = "off"$/embed = "all"/' "$F"     # create writes "off"
grep -q '^embed' "$F" || sed -i '/^kinds/a embed = "all"' "$F"
grep -q '^device' "$F" || sed -i '/^query_model/a device = "gpu"' "$F"
cat "$F"
$L hold index show "$NEW"
$L node resume "$NEW" >/dev/null

say "8. next"
echo "watch:  docker logs --since 10m $W | grep -i $NEW | tail   (the 23,610 new versions: rdf, fts, embed)"
echo "check:  hold check $NEW; then the pilot's coverage again (tools/legal_pilot.py coverage) against $NEW"
echo "retire: $OLD stays paused; removing it is the operator's decision"
