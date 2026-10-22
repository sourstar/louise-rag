#!/usr/bin/env bash
# remint-gutenberg-p10-switch.sh — after remint-gutenberg-p10.sh's numbers are
# read: gutenberg10 becomes gutenberg (the old one set aside, nothing deleted,
# as the 2026-10-20 twin), then gutenberg-book-editions is made again against
# it from the current claims + the same-title claims (/root/titles/claims.jsonl,
# tools/annotations_from_titles.py v2), checked and shared, the old one set
# aside. Stops before any rename if a dry run's counts are not the expected.
#   bash remint-gutenberg-p10-switch.sh SNAPSHOT_ID CLAIMS_EXPECTED
set -euo pipefail
SNAP=$1; EXPECT=$2
ROOT=/mnt/ssd4/apps/louise-rag
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
OLD=gutenberg; NEW=gutenberg10; RETIRED=gutenberg-p9-retired
ED=gutenberg-book-editions; ED_RETIRED=gutenberg-book-editions-p9-retired
TODAY=$(date +%Y%m%d)
say() { printf '\n=== %s  %s\n' "$(date +%T)" "$*"; }
run() { for i in $(seq 1 40); do out=$($L "$@" 2>&1) && rc=0 || rc=$?; echo "$out" | grep -q "held by the node" || break; sleep 15; done; echo "$out" | grep -v warn:config; return $rc; }

TITLE="Gutenberg: the versions of a work — editions and translations"
SUMMARY="Which Project Gutenberg books are versions of one work: editions, translations and volumes of it. From the books' own Wikipedia lines resolved through Wikidata (4,376 claims), a title/author matcher pairing Latin and Greek books with their English versions (11), and the same-title rule on the catalogue's agent ids, scans, LCCNs and volume runs (2,767; operator 2026-10-22)."
ANNOTATOR="Louise reference node (one-language-per-work and classical-translation measurements, 2026-10-09; reviewed 2026-10-19; same-title rule 2026-10-22)"

say "1. the editions dry run against $NEW, before anything is renamed"
docker cp /root/titles/claims.jsonl $W:/tmp/editions-claims.jsonl
EARGS=(hold collection create annotations --annotates "$NEW" --from-snapshot "$SNAP" --claims /tmp/editions-claims.jsonl
       --title "$TITLE" --summary "$SUMMARY" --annotator "$ANNOTATOR")
run "${EARGS[@]}" --name "$ED-new" --dry-run | tee /tmp/ed-dry.out
python3 - "$EXPECT" <<'PY'
import json, sys
t = open("/tmp/ed-dry.out").read()
j = json.loads(t[t.index("{"):t.rindex("}") + 1])
print("dry:", {k: j.get(k) for k in ("claims", "target_records_named", "parts", "records")})
raise SystemExit(0 if j.get("claims") == int(sys.argv[1]) else 1)
PY

say "2. $OLD aside (renamed, its held file set aside), $NEW → $OLD"
if [ ! -e "$ROOT/config/collections/$OLD.toml.superseded-$TODAY" ]; then
  run hold collection rename "$OLD" "$RETIRED"
  cp -p "$ROOT/config/collections/$RETIRED.toml" "$ROOT/config/$RETIRED.toml.kept-$TODAY"
  mv "$ROOT/config/collections/$RETIRED.toml" "$ROOT/config/collections/$OLD.toml.superseded-$TODAY"
fi
if [ -e "$ROOT/config/collections/$NEW.toml" ]; then
  run hold collection rename "$NEW" "$OLD"
fi
F="$ROOT/config/collections/$OLD.toml"
CODEX=$(docker exec "$W" python -c "from app.hold import codex as cx; print(cx.loads(open('/data/collections/$OLD/shard/codex.json', 'rb').read()).collection_id)")
cp -p "$F" "$ROOT/config/$OLD.toml.created-$TODAY"
sed "s|^codex = \".*\"|codex = \"$CODEX\"|" "$ROOT/config/collections/$OLD.toml.superseded-$TODAY" > "$F.tmp"
mv "$F.tmp" "$F"
cat "$F"

say "2b. 673 out of every index at once (parser 10's UnindexableDocument), before $OLD resumes"
run dev unindexable "$OLD" gutenberg:673 --yes
docker exec -i -w /srv -e PYTHONPATH=/srv "$W" python - <<'PY'
from pathlib import Path
from app import catalog, db, fts
from app.hold import collection as col, records as rec
root = Path("/data/collections")
name, codex = col.resolve(root, "gutenberg")
c = rec.local_collection(root, name, codex)
con = db.connect(c)
left = len(fts.book_chunks("en", 673, c))
hits = catalog.search(con, c, "Gutenberg Webster Unabridged Dictionary", 50) or []
err = con.execute("SELECT error FROM documents WHERE uid='gutenberg:673'").fetchone()[0]
print("673: lexical chunks", left, "| catalog has it", any(h["book_id"] == 673 for h in hits), "| error", err)
assert left == 0 and not any(h["book_id"] == 673 for h in hits) and err, "673 is still served"
PY
$L node resume "$OLD" | grep -v warn:config || true
run hold collection show "$OLD" | head -30

say "3. $ED aside, made again against the new $OLD, checked, shared"
if [ -e "$ROOT/config/collections/$ED.toml" ] && ! grep -q "$CODEX" "$ROOT/data/collections/$ED/shard/codex.json" 2>/dev/null; then
  run hold collection rename "$ED" "$ED_RETIRED"
  cp -p "$ROOT/config/collections/$ED_RETIRED.toml" "$ROOT/config/$ED_RETIRED.toml.kept-$TODAY"
  mv "$ROOT/config/collections/$ED_RETIRED.toml" "$ROOT/config/collections/$ED.toml.superseded-$TODAY"
fi
EARGS[5]="$OLD"
if [ ! -e "$ROOT/config/collections/$ED.toml" ]; then
  run "${EARGS[@]}" --name "$ED"
fi
run hold check "$ED"
run share collection "$ED"
grep -n '^share' "$ROOT/config/collections/$ED.toml" || true
echo "SWITCH DONE: $OLD = $CODEX; $ED made against it"
