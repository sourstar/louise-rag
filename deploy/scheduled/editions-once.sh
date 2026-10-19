#!/bin/bash
# editions.sh — one annotations collection for gutenberg's versions of a work
# (operator 2026-10-19: "both collections belong to the same one"; share = yes):
# the merged claims (tools/annotations_merge.py, 4,387) dry-run against
# gutenberg 2026-09, then the two local, unshared collections forgotten, the
# merged one created, checked and shared. Stops before forgetting anything if
# the dry run's counts are not the expected ones.
set -u
W=ix-louise-rag-worker-1
exec >>/root/editions.log 2>&1
lou() { docker exec $W python -m app.louise "$@" 2>&1 | grep -v acquire_enabled; return ${PIPESTATUS[0]}; }
TITLE="Gutenberg: the versions of a work — editions and translations"
SUMMARY="Which Project Gutenberg books are versions of one work: editions, translations and volumes of it. From the books' own Wikipedia lines resolved through Wikidata (4,376 claims), and a title/author matcher pairing Latin and Greek books with their English versions (11 more, adaptations left out by a hand review of 2026-10-19)."
ANNOTATOR="Louise reference node (one-language-per-work and classical-translation measurements, 2026-10-09; reviewed 2026-10-19)"
ARGS=(hold collection create annotations --annotates gutenberg --from-snapshot 2026-09 --claims /tmp/merged.jsonl --title "$TITLE" --summary "$SUMMARY" --annotator "$ANNOTATOR")
echo "== $(date '+%F %T')"
docker cp /root/editions/merged.jsonl $W:/tmp/merged.jsonl
echo "-- dry run"
lou "${ARGS[@]}" --name gutenberg-book-editions-v2 --dry-run | tee /tmp/ed-dry.out
python3 - <<'PY' || { echo "DRY RUN NOT AS EXPECTED: nothing forgotten, nothing created"; echo EDITIONS DONE; exit 2; }
import json
t = open("/tmp/ed-dry.out").read()
j = json.loads(t[t.index("{"):t.rindex("}") + 1])
flat = json.dumps(j)
print("dry:", {k: j.get(k) for k in ("claims", "target_records_named", "parts", "records")})
ok = j.get("claims") == 4387 and j.get("target_records_named") == 6073
raise SystemExit(0 if ok else 1)
PY
echo "-- forget the two"
for c in gutenberg-book-editions gutenberg-same-work; do lou hold collection forget "$c" --yes; echo "forget $c exit $?"; done
echo "-- create"
lou "${ARGS[@]}" --name gutenberg-book-editions; echo "create exit $?"
echo "-- check"
lou hold check gutenberg-book-editions; echo "check exit $?"
echo "-- share"
lou share collection gutenberg-book-editions; echo "share exit $?"
grep -n '^share' /mnt/ssd4/apps/louise-rag/config/collections/gutenberg-book-editions.toml
echo EDITIONS DONE
