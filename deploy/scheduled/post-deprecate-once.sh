#!/bin/bash
# post-deprecate.sh — after 9412219: the documents contribute embedded and never
# marked (dev mark-embedded), then the 418 duplicate holder bundles deprecated
# (dev deprecate-duplicates: one `deprecate` attestation, nothing removed now;
# the copies expire a day later). Under the node's pause of gutenberg; stops
# before writing anything if the dry run's numbers are not the measured ones.
set -u
W=ix-louise-rag-worker-1
L=/root/post-deprecate.log
exec >>"$L" 2>&1
lou() { docker exec $W python -m app.louise "$@" 2>&1 | grep -v acquire_enabled; return ${PIPESTATUS[0]}; }
retry() { for i in $(seq 1 60); do out=$(lou "$@"); rc=$?; echo "$out"; echo "$out" | grep -q "held by the node" || return $rc; sleep 10; done; return 3; }
echo "== $(date '+%F %T') tank before: $(zfs list -H -o used,avail tank/apps/louise-bundles)"
echo "-- mark-embedded --dry-run"; lou dev mark-embedded gutenberg --dry-run
lou node pause gutenberg >/dev/null && echo paused
echo "-- mark-embedded"; retry dev mark-embedded gutenberg; echo "exit $?"
echo "-- deprecate-duplicates --dry-run"
lou dev deprecate-duplicates gutenberg --dry-run > /tmp/dep-dry.out; rc=$?
tail -40 /tmp/dep-dry.out | sed -n '/^{/,$p'
python3 - <<'PY' || { echo "DRY RUN DIFFERS from the measurement (418 parts, 418 bundles, 2,491,650 chunks): nothing deprecated"; lou node resume gutenberg >/dev/null; echo resumed; exit 2; }
import json, re
t = open("/tmp/dep-dry.out").read()
j = json.loads(t[t.rindex("\n{") + 1:] if "\n{" in t else t[t.index("{"):])
d = j.get("gutenberg", j)
parts, bundles, chunks = d.get("parts"), d.get("bundles"), d.get("chunks")
print("dry run:", parts, bundles, chunks)
raise SystemExit(0 if parts == 418 and bundles == 418 and abs(int(chunks) - 2491650) <= 2491650 * 0.001 else 1)
PY
echo "-- deprecate-duplicates --yes (LOUISE_PLATFORM_IMAGE $(docker exec $W printenv LOUISE_PLATFORM_IMAGE | cut -c1-19)…)"
retry dev deprecate-duplicates gutenberg --yes; echo "exit $?"
lou node resume gutenberg >/dev/null && echo resumed
echo "== $(date '+%F %T') tank after: $(zfs list -H -o used,avail tank/apps/louise-bundles)"
echo POST DONE
