#!/usr/bin/env bash
# repin-gutenberg-instruction.sh — gutenberg's queries with the instruction
# Qwen3-Embedding expects (operator, 2026-10-20: "Do it"; A1f, 02 §2.4;
# docs/notes/query-campaign.md). RULE 1 throughout: the records are the same
# bytes (the parts are linked, not built), and every vector is kept — the
# instruction is prepended to queries only, so no stored row depends on it.
#   recipe        the live Codex's, query instruction set, engines = the runs stated now
#   create        the twin Codex (its calibration computed by the runs that embed)
#   seed          `hold snapshot create --shards-from`: the same parts, the same snapshot
#   carry         manifest, text index and vector table MOVED (one rename each, ssd4)
#   check         vectors-sample, before use
#   rename        gutenberg → gutenberg-q0-retired (its held file set aside, as
#                 pubmed-v6's), the twin → gutenberg; the old file's settings kept
# Run on the node, A1f live:  ssh root@node bash -s < deploy/16f/repin-gutenberg-instruction.sh
# Every step is resumable: run it again after a stop and it finishes.
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
OLD=gutenberg
NEW=gutenberg-qi
RETIRED=gutenberg-q0-retired
ENCODER=http://192.168.178.10:30286
MODEL=vega-qwen3-embedding-4b
# Qwen3-Embedding's documented default task, in its own format (measured
# against bare and a library wording: docs/notes/query-campaign.md, run 2)
INSTRUCTION=$'Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:'
TODAY=$(date +%Y%m%d)

say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1) && rc=0 || rc=$?; echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out" | grep -v warn:config; return $rc; }

say "0. A1f is live"
cat "$ROOT/src/.deployed-commit"
docker exec "$W" python -c "import inspect; from app.hold import carry; assert 'A1f' in inspect.getsource(carry.carry); print('A1f: yes')"

if [ -e "$ROOT/config/collections/$RETIRED.toml" ] || [ -e "$ROOT/config/collections/$OLD.toml.superseded-$TODAY" ]; then
  say "already renamed: resuming at 8"
else

say "1. pause $OLD: nothing embeds into the table that moves"
$L node pause "$OLD" | grep -v warn:config || true

say "2. the recipe: the live Codex's, the instruction set, the runs stated now"
REC="$ROOT/config/recipes/$NEW.json"
if [ ! -e "$REC" ]; then
  $L hold embedder run --encoder "$ENCODER" --api vega --model "$MODEL" 2>/dev/null > /tmp/$NEW-run-doc.json
  $L hold embedder run --encoder "$ENCODER" --api vega --model "$MODEL" --query 2>/dev/null > /tmp/$NEW-run-query.json
  docker cp /tmp/$NEW-run-doc.json "$W:/tmp/run-doc.json"
  docker cp /tmp/$NEW-run-query.json "$W:/tmp/run-query.json"
  docker exec -w /srv "$W" python -m tools.recipe_from_codex /data/collections/$OLD/shard/codex.json \
    /tmp/$NEW.json --instruction "$INSTRUCTION" --engine /tmp/run-doc.json --query-engine /tmp/run-query.json
  mkdir -p "$ROOT/config/recipes"
  docker cp "$W:/tmp/$NEW.json" "$REC"
fi
grep -n '"instruction"' "$REC"

say "3. create $NEW (its calibration: 512 texts each side, on the card), then pause it at once"
if [ ! -e "$ROOT/config/collections/$NEW.toml" ]; then
  $L hold collection create gutenberg --recipe "/config/recipes/$NEW.json" --name "$NEW" \
    --encoder "$ENCODER" --api vega --embed-model "$MODEL" \
    --query-encoder "$ENCODER" --query-model "$MODEL" | grep -v warn:config
fi
$L node pause "$NEW" | grep -v warn:config || true
$L hold collection ack "$NEW" --yes 2>/dev/null | grep -v warn:config || true

say "4. its origin is $OLD's (the same books, never fetched again)"
O="$ROOT/data/collections/$NEW/origin"
TARGET=$(readlink "$ROOT/data/collections/$OLD/origin")
if [ "$(readlink "$O")" != "$TARGET" ]; then
  EMPTY=$(docker exec "$W" sh -c "ls -A /corpora/$NEW 2>/dev/null | head -1")
  [ -z "$EMPTY" ] || { echo "/corpora/$NEW holds files: not ours to remove"; exit 1; }
  docker exec "$W" sh -c "rmdir /corpora/$NEW 2>/dev/null || true"
  ln -sfn "$TARGET" "$O"
fi
ls -la "$ROOT/data/collections/$NEW/"

say "5. the parts: $OLD's, linked; the same snapshot frozen"
run hold records validation "$NEW"
run hold snapshot create "$NEW" --shards-from "$OLD"

say "6. carry: the dry run, then the move (manifest, index/, lance/)"
run dev carry "$OLD" "$NEW"
run dev carry "$OLD" "$NEW" --yes

say "7. the A1 proof, before use"
run hold check "$NEW" --checks vectors-sample

say "8a. $OLD aside: renamed, its held file set aside (as pubmed-v6's), nothing deleted"
run hold collection rename "$OLD" "$RETIRED"
fi

if [ -e "$ROOT/config/collections/$RETIRED.toml" ]; then
  cp -p "$ROOT/config/collections/$RETIRED.toml" "$ROOT/config/$RETIRED.toml.kept-$TODAY"
  mv "$ROOT/config/collections/$RETIRED.toml" "$ROOT/config/collections/$OLD.toml.superseded-$TODAY"
fi

say "8b. $NEW → $OLD"
if [ -e "$ROOT/config/collections/$NEW.toml" ]; then
  run hold collection rename "$NEW" "$OLD"
fi

say "9. the held file: the old one's settings, the new Codex"
F="$ROOT/config/collections/$OLD.toml"
CODEX=$(docker exec "$W" python -c "import json; print(json.load(open('/data/collections/$OLD/shard/codex.json'))['collection_id'])" 2>/dev/null || true)
[ -n "$CODEX" ] || CODEX=$(grep -m1 '^codex' "$F" | cut -d'"' -f2)
cp -p "$F" "$ROOT/config/$OLD.toml.created-$TODAY"
sed "s|^codex = \".*\"|codex = \"$CODEX\"|" "$ROOT/config/collections/$OLD.toml.superseded-$TODAY" > "$F.tmp"
mv "$F.tmp" "$F"
cat "$F"

say "10. resume, and look"
$L node resume "$OLD" | grep -v warn:config || true
run hold collection show "$OLD" | head -30
echo "next: the campaign (LOUISE_URL=http://192.168.178.10:30042 pytest tests_campaign) and node show: in use?"
