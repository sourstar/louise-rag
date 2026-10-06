#!/usr/bin/env bash
# localagi-smoke.sh — LocalAGI's own requests against the node's deposit routes
# (07 §7.4; the shapes of tests/deposit/test_deposit_localagi.py), with the
# deposit token read from its file, never on a command line (invariant 8).
# Creates ONE throwaway collection, exercises it, resets it, and removes its
# held file. Run on the node:  ssh … root@192.168.178.10 bash -s < deploy/16f/localagi-smoke.sh
set -euo pipefail

URL=http://127.0.0.1:30042
LOUISE=/mnt/ssd4/apps/louise-rag
TOK=$LOUISE/config/deposit.token
NAME=smoke-agent
B=/tmp/smoke-body
HDR=$(mktemp)
printf 'Authorization: Bearer %s\n' "$(cat "$TOK")" > "$HDR"
trap 'rm -f "$HDR" "$B" /tmp/smoke-*.txt' EXIT

say() { printf '\n=== %s\n' "$*"; }
req() { curl -sS -H @"$HDR" -o "$B" -w '%{http_code}' "$@"; }
body() { head -c "${1:-240}" "$B"; echo; }

say "1. without the token: 401"
code=$(curl -sS -o /dev/null -w '%{http_code}' "$URL/api/collections"); echo "$code"; [ "$code" = 401 ]

say "2. create $NAME: 201 (as LocalAGI's NewWrappedClient does for an agent)"
code=$(req -X POST -H 'Content-Type: application/json' -d "{\"name\":\"$NAME\"}" "$URL/api/collections")
echo "$code"; body; [ "$code" = 201 ]

say "3. list: $NAME among the collections"
code=$(req "$URL/api/collections"); echo "$code"; body; grep -q "\"$NAME\"" "$B"

say "4. store a memory: multipart field 'file', named with the client's temp PATH"
KEY="2026-10-06-12-00-00-$(printf 'memoria' | md5sum | cut -c1-32).txt"
printf 'Il caffè è stato ordinato alle otto. Una memoria sul caffè del mattino.\n' > "/tmp/smoke-$KEY"
code=$(req -X POST -F "file=@/tmp/smoke-$KEY;filename=/tmp/localrag3141592/$KEY" "$URL/api/collections/$NAME/upload")
echo "$code"; body; [ "$code" = 200 ]; grep -q "\"key\": *\"$KEY\"" "$B" || grep -q "$KEY" "$B"

say "5. entries: the key, by its base name"
code=$(req "$URL/api/collections/$NAME/entries"); echo "$code"; body; grep -q "$KEY" "$B"

say "6. read the entry"
code=$(req "$URL/api/collections/$NAME/entries/$KEY"); echo "$code"; body 160; grep -q "caff" "$B"

say "7. search: the memory comes back (text leg now; vectors on the worker's next round)"
code=$(req -X POST -H 'Content-Type: application/json' -d '{"query":"caffè ordinato al mattino","max_results":3}' "$URL/api/collections/$NAME/search")
echo "$code"; body 400; [ "$code" = 200 ]; grep -q -i "caff" "$B"

say "8. the open search never names it (02 §2.9: never-share)"
code=$(curl -sS -o "$B" -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"query":"caffè ordinato al mattino","mode":"fts","translate":false}' "$URL/api/search")
echo "$code"; ! grep -q "$NAME" "$B"; echo "not named: ok"

say "9. delete the entry: gone from the entries"
code=$(req -X DELETE -H 'Content-Type: application/json' -d "{\"entry\":\"$KEY\"}" "$URL/api/collections/$NAME/entry/delete")
echo "$code"; body; [ "$code" = 200 ]
code=$(req "$URL/api/collections/$NAME/entries"); ! grep -q "$KEY" "$B"; echo "entries after delete: $(head -c 120 "$B")"

say "10. store again, then reset: every deposit erased, no trace (02 §2.9)"
code=$(req -X POST -F "file=@/tmp/smoke-$KEY;filename=/tmp/localrag3141592/$KEY" "$URL/api/collections/$NAME/upload"); [ "$code" = 200 ]
code=$(req -X POST "$URL/api/collections/$NAME/reset"); echo "$code"; body; [ "$code" = 200 ]
code=$(req "$URL/api/collections/$NAME/entries"); echo "entries after reset: $(head -c 120 "$B")"; grep -q '"count": *0' "$B"
sleep 2
if grep -rqa "ordinato alle otto" "$LOUISE/data/collections/$NAME/" 2>/dev/null; then echo "TRACE LEFT under data/collections/$NAME"; exit 1; fi
echo "no trace of the text under data/collections/$NAME: ok"

say "11. the worker's side (its log, this run)"
docker logs --since 3m ix-louise-rag-worker-1 2>&1 | grep -i -E "$NAME|deposit" | tail -8 | cut -c1-150 || true

say "12. the throwaway collection: its held file removed (no \`hold collection forget\` exists yet — 07 §7.3 names one)"
rm -f "$LOUISE/config/collections/$NAME.toml"
echo "removed config/collections/$NAME.toml; data/collections/$NAME/ (empty) is left for the operator"
echo
echo "SMOKE OK: LocalAGI's create, store, list, read, search, delete, reset all answered as its client expects."
