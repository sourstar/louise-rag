#!/usr/bin/env bash
# pubmed-v8.sh — pubmed's corrected Codex (docs/notes/char-cut-truncation.md
# §5): version 8, its space cutting by tokens (09 §9.3 `cut`), so pubmed
# passes its own vectors-sample again (73 of its 74 misses were the cut).
# RULE 1 throughout, as normattiva's parser 8 (recreate-normattiva-parser8.sh):
#   recipe              pubmed's own v6 Codex, rebuilt byte for byte from it
#                       (tools/recipe_from_codex.py), + `cut = "tokens"` and
#                       the engines both sides state now; `create` makes the
#                       references through the token cut (~1 min of GPU)
#   snapshot            seeded from pubmed's parts, hard links (G3)
#   carry --recut       pubmed's manifest, text index and 40.0 M vectors MOVED
#                       (A1e): NCBI's 35.4 M and the local rows within the
#                       2,048-character budget stay; ~778 k local rows the
#                       character cut shortened are dropped and their
#                       documents embedded again — at night, over ~1 night
#   vectors-sample      the A1e proof, before use
# pubmed is then set aside (its file renamed, nothing deleted): its Codex and
# parts stay on disk, and naming pubmed-v8 `pubmed` later is the operator's call.
#
# BEFORE IT: deploy-louise-cut.sh (Louise knows Codex 8 and --recut) and
# rollout-p2.sh (the query side, llama.cpp, cuts exactly by token ids).
# RUN BETWEEN 23:00 AND 07:00 (quiet hours): the recut reads the whole table.
#   bash /root/pubmed-v8.sh 2>&1 | tee /root/pubmed-v8.log
# Re-runnable: each step finds its work done and goes on.
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
CFG=$ROOT/config/collections
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
OLD=pubmed
NEW=pubmed-v8
LOCALAI=http://192.168.178.10:30286
DOC=vega-medcpt-article-ov
QUERY=vega-medcpt-query
MODELS=/mnt/ssd5/apps/localai/models
B=.bak-pubmed-v8
WORK=/root/pubmed-v8
TOOL="docker run --rm -v $ROOT/src:/srv:ro -v $WORK:/w -v $ROOT/config:/config -w /srv -e PYTHONPATH=/srv louise-rag:local"

say() { printf '\n=== %s\n' "$*"; }
run() {   # a writer, retried while the worker still holds the collection
  local out="" rc=0
  for _ in $(seq 1 40); do
    out=$($L "$@" 2>&1) && rc=0 || rc=$?
    echo "$out" | grep -q "held by the node" || break
    sleep 15
  done
  echo "$out"
  return $rc
}

say "0. the code, the backend and the hour"
H=$(date +%H)
# QUIET_HOURS=lifted: the operator lifts them for this run (2026-10-18: "you can
# already use the full power of NAS without waiting until 23")
if [ "${QUIET_HOURS:-}" = lifted ]; then
  echo "quiet hours lifted by the operator for this run ($H h)"
else
  { [ "$H" -ge 23 ] || [ "$H" -lt 7 ]; } || { echo "run between 23:00 and 07:00 (quiet hours); it is $H h"; exit 1; }
fi
docker exec "$W" python -c "from app.hold import codex, carry; assert codex.CUT_VERSION == 8 and carry.recut; print('Louise: Codex 8, --recut')"
grep -q '"backend": "louise-sycl-f16-llama-cpp"' /mnt/tank/apps/localai/backends/vega-llama-cpp/vega.json \
  || { echo "vega-llama-cpp is not in front of P2's backend: run rollout-p2.sh first"; exit 1; }
mkdir -p "$WORK" "$ROOT/config/recipes"

say "1. MedCPT-query admits a 512-token window: context 512 -> 513"
# llama.cpp embeds at most context_size - 1 tokens, and the space's ctx is 512.
# A query of up to 512 tokens is the same vector at 513 (BERT positions 0..511),
# so its run is new and the gate validates it once; nothing is re-embedded.
cd "$MODELS"
test -e "$QUERY.yaml$B" || cp -p "$QUERY.yaml" "$QUERY.yaml$B"
if grep -qx "context_size: 512" "$QUERY.yaml"; then
  sed -i 's/^context_size: 512$/context_size: 513/' "$QUERY.yaml"
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" \
    -d "{\"model\":\"$QUERY\"}" || true
  curl -s -X POST "$LOCALAI/models/reload"; echo
fi
grep -qx "context_size: 513" "$QUERY.yaml"
test "$(diff "$QUERY.yaml$B" "$QUERY.yaml" | grep -c '^[<>]')" = 2

say "2. $OLD paused: its state moves to $NEW"
$L node pause "$OLD" >/dev/null || true

say "3. the runs both sides state now (G5: create holds the reference to them)"
# the JSON alone: a model loaded cold logs a "cold start" line on stdout first
# (2026-10-18, the first run: "Extra data" in the query side's file)
$L hold embedder run --encoder "$LOCALAI" --api vega --model "$DOC" --pair \
  | sed -n '/^{/,$p' > "$WORK/doc-run.json"
$L hold embedder run --encoder "$LOCALAI" --api vega --model "$QUERY" --query \
  | sed -n '/^{/,$p' > "$WORK/query-run.json"
python3 -c "import json,sys; [print(f, json.load(open(f))['run'][:23], json.load(open(f))['engine']['name']) for f in sys.argv[1:]]" \
  "$WORK/doc-run.json" "$WORK/query-run.json"

say "4. the recipe: $OLD's own Codex, rebuilt byte for byte, then cut by tokens"
docker exec "$W" cat "/data/collections/$OLD/shard/codex.json" > "$WORK/codex-v6.json"
$TOOL python -m tools.recipe_from_codex /w/codex-v6.json "/config/recipes/$NEW.json" \
  --cut tokens --engine /w/doc-run.json --query-engine /w/query-run.json

say "5. create $NEW (the calibration set embedded through the token cut)"
if [ ! -e "$CFG/$NEW.toml" ]; then
  # both sides on one URL; the query side's references are made only when
  # its endpoint is named (`--query-encoder`), and its model picks the side
  $L hold collection create pubmed --recipe "/config/recipes/$NEW.json" --name "$NEW" \
    --encoder "$LOCALAI" --query-encoder "$LOCALAI" --api vega \
    --embed-model "$DOC" --query-model "$QUERY"
fi
$L hold collection ack "$NEW" || true
$L hold collection ack "$NEW" --yes
NEWID=$(grep '^codex = ' "$CFG/$NEW.toml" | cut -d'"' -f2)
echo "$NEW = $NEWID"
docker exec "$W" python -c "
from app.hold import codex as cx
c = cx.loads(open('/data/collections/$NEW/shard/codex.json', 'rb').read())
assert c.collection_id == '$NEWID' and c.version == 8 and c.space.cut == 'tokens'
print('version', c.version, 'space', c.space_id[:23])"

say "6. the origin is $OLD's: the same files, linked, never fetched again"
O="$ROOT/data/collections/$NEW/origin"
if [ "$(readlink "$O")" != "/corpora/$OLD" ]; then
  EMPTY=$(docker exec "$W" sh -c "ls -A /corpora/$NEW 2>/dev/null | head -1")
  [ -z "$EMPTY" ] || { echo "/corpora/$NEW holds files: not ours to remove"; exit 1; }
  docker exec "$W" sh -c "rmdir /corpora/$NEW 2>/dev/null || true"
  ln -sfn "/corpora/$OLD" "$O"
fi

say "7. $NEW paused, then its snapshot seeded from $OLD's parts"
$L node pause "$NEW" >/dev/null
run hold records validation "$NEW"
run hold snapshot create "$NEW" --shards-from "$OLD"

say "8. the carry, A1e: the dry run, then the move and the recut"
run dev carry "$OLD" "$NEW" --recut
run dev carry "$OLD" "$NEW" --recut --yes

say "9. $OLD set aside: its file renamed, so no restart rebuilds it empty"
if [ -e "$CFG/$OLD.toml" ]; then
  mv "$CFG/$OLD.toml" "$CFG/$OLD.toml.superseded-$(date +%Y%m%d)"
fi

say "10. the A1e proof"
# On a failure this stops, $NEW paused and unused. A failing row the card and
# the CPU twin agree on is an input the archive embedded otherwise (NCBI's 74th
# miss of 2026-10-18: an abstract revised since): 02 §2.4's remedy drops it and
# its document is embedded here — then this step again, then 11.
run hold check "$NEW" --checks vectors-sample

say "11. $NEW's file: $OLD's settings, its own Codex"
cp -p "$CFG/$NEW.toml" "$CFG/$NEW.toml.created"
SUPERSEDED=$(ls -t "$CFG/$OLD.toml.superseded-"* | head -1)
sed "s|^codex = .*|codex = \"$NEWID\"|" "$SUPERSEDED" > "$CFG/$NEW.toml.tmp"
grep -q "^codex = \"$NEWID\"" "$CFG/$NEW.toml.tmp"
mv "$CFG/$NEW.toml.tmp" "$CFG/$NEW.toml"
diff "$SUPERSEDED" "$CFG/$NEW.toml" || true
$L node resume "$NEW" >/dev/null

say "12. the re-embed, at night only"
/root/louise-scheduled/set-embed.sh "$NEW" on-demand all
echo "the ~778 k documents to embed again: watch"
echo "  docker logs --since 10m $W | grep -i $NEW | tail"
echo "and give $NEW gutenberg's two cron jobs (TrueNAS, as jobs 5 and 6):"
echo "  07:00  /root/louise-scheduled/set-embed.sh $NEW all on-demand"
echo "  23:00  /root/louise-scheduled/set-embed.sh $NEW on-demand all"
echo "$OLD stays on disk (Codex, parts); naming $NEW 'pubmed' is the operator's call."
