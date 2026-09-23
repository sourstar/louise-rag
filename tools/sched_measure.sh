#!/bin/bash
# THE SCHEDULING MEASUREMENT. Run ON THE NODE; one row of
# docs/notes/scheduling-log.md comes out of one run of this.
#
#   bash tools/sched_measure.sh [window-minutes]     # default 30
#
# It is a script and not a set of remembered commands so that two rows of
# that log are measured the same way — worker-tree CPU (the whole tree, so
# the pool's children count), the workload's SHAPE, and throughput.
#
# READ chunks/doc FIRST. A wikipedia document is not a unit of work: a
# redirect-dense stretch of the corpus gives ~0.6 chunks per document and a
# stretch of real articles ~3.6, so two windows can differ five-fold in
# actual work while `docs/s` looks flat. Compare chunks/s; use docs/s only
# between windows whose chunks/doc agree. On 2026-09-23 this distinction
# turned one apparent regression into a real one and a second into a 5.7x
# win — see the log's preamble.
python3 - <<'PY'
import os, time, subprocess
HZ = os.sysconf('SC_CLK_TCK')
init = int(subprocess.run(['docker','inspect','-f','{{.State.Pid}}','ix-louise-rag-worker-1'],
                          capture_output=True, text=True).stdout.strip())
kids = set()
def tree_cpu():
    tot = 0
    for p in os.listdir('/proc'):
        if not p.isdigit(): continue
        try:
            s = open(f'/proc/{p}/stat').read()
            r = s[s.rindex(')')+2:].split()
            if int(r[1]) == init or int(r[1]) in kids:
                kids.add(int(p)); tot += int(r[11]) + int(r[12])
        except OSError: pass
    return tot
tree_cpu()
a = tree_cpu(); t0 = time.time()
time.sleep(60)
b = tree_cpu(); dt = time.time() - t0
print(f"worker tree {100*(b-a)/HZ/dt:.0f}% of 1200% over {dt:.0f}s")
PY
WIN=${1:-30}
# THE WORKLOAD NORMALISER. wikipedia's documents differ by two orders of
# magnitude — a redirect-dense stretch of the corpus yields ~0.6 chunks per
# document, a stretch of real articles ~3.6 — so docs/s across two windows
# compares two different amounts of work. chunks/s is what to compare, and
# chunks/doc is what says whether two rows are comparable at all.
echo "--- workload shape (fts batches), last ${WIN} min ---"
docker logs --since ${WIN}m ix-louise-rag-worker-1 2>&1 \
  | grep -oE 'indexed [0-9]+ documents / [0-9]+ chunks' \
  | sed -E 's/indexed ([0-9]+) documents \/ ([0-9]+) chunks/\1 \2/' \
  | awk -v w=$WIN '{d+=$1; c+=$2; n++} END {
      if (d) printf "%d fts batches, %d docs, %d chunks -> %.2f chunks/doc, %.1f chunks/s of wall\n",
      n, d, c, c/d, c/(w*60) }'
echo "--- docs/s from the batch log, last ${WIN} min ---"
docker logs --since ${WIN}m ix-louise-rag-worker-1 2>&1 \
  | grep -oE '(extracted|indexed) [0-9]+ documents.*\(([0-9.]+)s\)' \
  | sed -E 's/.*(extracted|indexed) ([0-9]+) documents.*\(([0-9.]+)s\)/\2 \3/' \
  | awk -v w=$WIN '{d+=$1; s+=$2; n++} END {
      printf "%d batches, %d documents, %.0fs inside batches of %ds wall -> %.1f docs/s, %.0f%% of wall inside batches\n",
      n, d, s, w*60, d/s, 100*s/(w*60) }'
echo "--- deferrals (a writer that lost the manifest lock) ---"
docker logs --since ${WIN}m ix-louise-rag-worker-1 2>&1 | grep -c "round deferred" || true
echo "--- errors ---"
docker logs --since ${WIN}m ix-louise-rag-worker-1 2>&1 | grep -iE "error|traceback" | tail -5 || true

# RUNNING CHILDREN, sampled at 1 Hz. The in-process metric (`parse pool —
# N of 11 slots in flight`) counts tasks SUBMITTED, and `compute` submits a
# whole batch at once, so it reads near-full through the straggler tail
# while most children are idle: 73-81 % in flight against 34 % running,
# measured 2026-09-23. This is the one that sees the sawtooth.
echo "--- running pool children, 1 Hz for ${2:-90} s ---"
INIT=$(docker inspect -f '{{.State.Pid}}' ix-louise-rag-worker-1)
PY=$(ps -eo pid,ppid --no-headers | awk -v p=$INIT '$2==p {print $1; exit}')
python3 - "$PY" "${2:-90}" <<'PY'
import os, sys, time, subprocess
par, secs = sys.argv[1], int(sys.argv[2])
def kids():
    return [l.split()[0] for l in subprocess.run(['ps','-eo','pid,ppid','--no-headers'],
            capture_output=True, text=True).stdout.splitlines() if l.split()[1] == par]
def running(ps):
    n = 0
    for p in ps:
        try:
            f = open(f'/proc/{p}/stat').read()
            if f[f.rindex(')')+2:].split()[0] == 'R':
                n += 1
        except OSError:
            pass
    return n
line = []
for _ in range(secs):
    line.append(running(kids())); time.sleep(1)
# one hex digit per second, so a sawtooth is visible as a shape
print("".join(f"{v:x}" if v < 16 else "F" for v in line))
n = len(line) or 1
print(f"mean {sum(line)/n:.1f} running;  at 0: {line.count(0)}s;  >=8: {sum(1 for v in line if v>=8)}s")
PY
