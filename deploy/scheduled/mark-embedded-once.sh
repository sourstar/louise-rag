#!/bin/bash
# mark2.sh — 457077b's `dev mark-embedded` (one scan of the table): the
# documents contribute embedded and never marked. Dry run first (no lock);
# the run under the node's pause of gutenberg, only if the dry run finds a
# plausible count (measured 2026-10-19: 363 complete of 372 with rows).
set -u
W=ix-louise-rag-worker-1
exec >>/root/mark2.log 2>&1
lou() { docker exec $W python -m app.louise "$@" 2>&1 | grep -v acquire_enabled; return ${PIPESTATUS[0]}; }
echo "== $(date '+%F %T')"
t=$(date +%s); lou dev mark-embedded gutenberg --dry-run | tee /tmp/mark-dry.out; echo "dry run $(( $(date +%s) - t )) s"
n=$(python3 -c "import json; t=open('/tmp/mark-dry.out').read(); print(json.loads(t[t.rindex('\n{')+1:] if '\n{' in t else t[t.index('{'):]).get('complete_unmarked', -1))")
echo "complete_unmarked $n"
if [ "$n" -lt 300 ] || [ "$n" -gt 450 ]; then echo "NOT PLAUSIBLE: nothing marked"; echo MARK DONE; exit 2; fi
lou node pause gutenberg >/dev/null && echo paused
for i in $(seq 1 60); do t=$(date +%s); out=$(lou dev mark-embedded gutenberg); rc=$?; echo "$out"; echo "$out" | grep -q "held by the node" || break; sleep 10; done
echo "run exit $rc, $(( $(date +%s) - t )) s"
lou node resume gutenberg >/dev/null && echo resumed
echo MARK DONE
