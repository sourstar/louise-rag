#!/bin/bash
# rollout-116-20261031.sh — #116 (f1fd164): THE WRAPPER IS PART OF THE ENGINE. A
# backend engine's `image` hashes VeGa's package (`engine_manifest`) beside the
# backend's tree, so every model behind a VeGa package gets a new run once.
# Louise went first (f70dbf9: the receipts service takes the `setting` entry).
# The wrapper is #117's (3d723bd): its half of the engine by an ALLOW-list —
# the package's *.py outside venv/ and __pycache__, the top-level run.sh, and
# `cuts` — so neo-cache/, run/, REVISION and vega.json are out by construction.
# And #124's (49001b7): the Intel contract is GGML_SYCL_FA_ONEDNN=0 beside
# GGML_SYCL_ENABLE_DNN=0 — DNN=0 never covered flash attention, whose first
# choice is oneDNN's fused SDPA (the FA flips and NaNs, approved-encoders.md) —
# so step 2b puts it in every SYCL model's env first. A model with FA on (bge-m3
# for wikipedia) then runs another kernel: its bytes may move, and its gate,
# multi-load since #120, decides (NEW_BYTES); every other run's fingerprint must
# equal the run before's.
#
# The packages that back a model (2026-10-31): vega-llama-cpp, -cpu, -p5
# (REVISION 0ec2239, #110) and vega-llama-cpp-gen, vega-louise-python-20261020c
# (pubmed's article CPU twin, a JSON model file), -20261021a, -20261030
# (8cbb8ad). Not touched: vega-llama-cpp-t5 (MADLAD's llama-cpp, no wrapper)
# and the packages no model uses (vega-louise-python, -20261020, -20261020b at
# a2c89e8; -20261021b) — a model put on one later is a new run whatever it
# carries.
#
# RULE 1, the shortcuts, named:
#   - a rebuild by build_vega_package.sh would reinstall every file; the files
#     a rebuild at this commit changes against the installed REVISIONs are
#     vega_wrapper.py and app/hold/codex.py (8cbb8ad: #110's cut, which a
#     context_size package never reaches — `_cut` returns before it — and a
#     cached collection_id) and REVISION. Those three are replaced and
#     compiled with each package's own python; the rest is carried.
#   - invariant 12's fresh-load measurement is CARRIED from each model's
#     current run: the engine's binary, flags and env are unchanged and so is
#     every byte the wrapper sends; the gate's fingerprint (sixteen fixed texts
#     one at a time, again in reverse order) must equal the current run's
#     bit for bit, or the rollout stops (architecture session, 2026-10-31).
#
# Prepared on the dev PC: a tar of vega_wrapper.py, app/hold/codex.py and
# REVISION (this commit) -> /root/vega-116.tar; fp_runs.py -> /root/fp_runs.py.
#   bash /root/rollout-116-20261031.sh 2>&1 | tee /root/rollout-116-20261031.log
# Stops at the first failure. BACKOUT at the end.
set -euo pipefail

BK=/mnt/ssd5/apps/localai/backends
M=/mnt/ssd5/apps/localai/models
PKGS="vega-llama-cpp vega-llama-cpp-cpu vega-llama-cpp-p5 vega-llama-cpp-gen vega-louise-python-20261020c vega-louise-python-20261021a vega-louise-python-20261030"
FILES="vega_wrapper.py app/hold/codex.py REVISION"
PYC="__pycache__/vega_wrapper.cpython-312.pyc app/hold/__pycache__/codex.cpython-312.pyc"
SRC=/root/vega-116.tar
SAVED=/mnt/ssd5/apps/localai/backends-bak-20261031-116     # outside the backends path
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"
LOCALAI=http://localhost:30286
LA=ix-localai-localai-1
GATED="gutenberg wikipedia pubmed normattiva wikidata"     # every collection whose encoder is behind these packages
# the SYCL llama.cpp models (GGML_SYCL_ENABLE_DNN=0 in their env), 2026-10-31; not Louise's models on
# intel-sycl-f16-llama-cpp (gemma-3-27b-it, qwen3-30b-a3b: no VeGa, no run stated)
SYCL_MODELS="index-translate-9b louise-chat-qwen3-8b louise-rerank-bge-strict louise-rerank-bge louise-rerank-lamar
  louise-translate-madlad vega-bge-m3 vega-granite vega-medcpt-article vega-medcpt-query vega-qwen3-embedding-4b"
NEW_BYTES="wikipedia"        # its encoder runs FA on: FA_ONEDNN=0 changes its kernel, so its fingerprint may move
. "$(dirname "$0")/backend_files.sh"

say() { printf '\n=== %s %s\n' "$(date +%T)" "$*"; }
mode() { $L node show 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['mode'])"; }
paused_collections() { $L node show 2>/dev/null | python3 -c "import sys,json; print(' '.join(json.load(sys.stdin)['overrides'].get('paused_collections') or []))"; }
# `<collection> <role> <run> <fingerprint>` per side; the worker's log lines go to stdout too, so only these
fp() { docker exec -i $W python - "$@" < /root/fp_runs.py 2>/dev/null | grep -E "^[a-z0-9-]+ (document|query) sha256:[0-9a-f]{64} "; }
# every model whose YAML (or JSON) names one of these packages as its backend
models() {
  python3 - "$M" $PKGS <<'PY'
import re, sys
from pathlib import Path
pk = set(sys.argv[2:])
for f in sorted(Path(sys.argv[1]).glob("*.yaml")):
    t = f.read_text(errors="replace")
    b = re.search(r'^\s*"?backend"?\s*:\s*"?([A-Za-z0-9._-]+)"?', t, re.M)
    n = re.search(r'^\s*"?name"?\s*:\s*"?([A-Za-z0-9._-]+)"?', t, re.M)
    if b and n and b.group(1) in pk:
        print(n.group(1))
PY
}

say "0. preconditions, the new files, the ones they replace saved, the runs and fingerprints now"
# a candidate model left for its gate (vega-qwen3-embedding-4b-p5fa) is unloaded with the rest and gated
# after this, on its new run; a measurement still loading models fresh is not run across
pgrep -f "^bash /root/measure/(qwen|age-probe|after-|variant-runs)" >/dev/null && { echo "a measurement is running"; exit 1; } || true
test -s "$SRC" && test -s /root/fp_runs.py || { echo "no $SRC or /root/fp_runs.py"; exit 1; }
ls /root/stage-models/*.yaml >/dev/null || { echo "no /root/stage-models (deploy/vega/stage/models at this commit)"; exit 1; }
STAGE=$(mktemp -d)
tar -xf "$SRC" -C "$STAGE"
for f in $FILES; do test -s "$STAGE/$f" || { echo "the tar lacks $f"; exit 1; }; done
grep -q "^def engine_manifest(" "$STAGE/vega_wrapper.py" || { echo "the tar's wrapper is not #116's"; exit 1; }
grep -q '^WRAPPER_ENTRY = "run.sh"$' "$STAGE/vega_wrapper.py" || { echo "the tar's wrapper is not #117's (the allow-list)"; exit 1; }
grep -q '"sycl": {"GGML_SYCL_ENABLE_DNN": "0", "GGML_SYCL_FA_ONEDNN": "0"}' "$STAGE/vega_wrapper.py" \
  || { echo "the tar's wrapper is not #124's (FA_ONEDNN in the Intel contract)"; exit 1; }
echo "revision $(cat "$STAGE/REVISION")"
for p in $PKGS; do
  for f in $PYC; do test -f "$BK/$p/$f" || { echo "$p lacks $f"; exit 1; }; done
  test -d "$SAVED/$p" || {
    mkdir -p "$SAVED/$p/app/hold/__pycache__" "$SAVED/$p/__pycache__"
    for f in $FILES $PYC; do cp -a "$BK/$p/$f" "$SAVED/$p/$f"; done
    bf_record "$BK/$p" $FILES $PYC > "$SAVED/$p.modes"
  }
  echo "$p: was $(cut -c1-10 "$SAVED/$p/REVISION"), wrapper $(sha256sum "$SAVED/$p/vega_wrapper.py" | cut -c1-12); saved"
done
MODELS=$(models | tr '\n' ' ')
echo "models: $MODELS"
MODE=${MODE_BEFORE:-$(mode)}; BEFORE_PAUSED=${PAUSED_BEFORE:-$(paused_collections)}
echo "mode $MODE; paused collections: ${BEFORE_PAUSED:-none}"
case $MODE in running|paused) ;; *) echo "mode $MODE: run this from running or paused"; exit 1 ;; esac
test -s /root/rollout-116-before.txt || fp $GATED > /root/rollout-116-before.txt
cat /root/rollout-116-before.txt
for c in $GATED; do grep -q "^$c document " /root/rollout-116-before.txt || { echo "$c: its run not read"; exit 1; }; done
grep -q " none$" /root/rollout-116-before.txt && { echo "a side's current run has no kept statement to compare with"; exit 1; } || true

# RESUME_GATES=1 (with PAUSED_BEFORE = the list step 0 printed the first time): steps 1-3 ran already. Every
# model on these packages is unloaded AGAIN first: on 2026-10-31 a search loaded bge-m3 between step 2 and
# step 3 (the node's pause stops the worker, not a search), its process kept the OLD wrapper's run, and
# wikipedia's gate met two runs and deferred
if [ -n "${RESUME_GATES:-}" ]; then
say "1-3 done in the run before (RESUME_GATES): every model on these packages unloaded again"
for m in $MODELS; do
  r=$(curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}" || true)
  case "$r" in *stopped*) echo "$m: stopped";; esac
done
else
say "1. the node paused while the wrappers change"
$L node pause

say "2. every model on these packages unloaded"
for m in $MODELS; do
  printf "%s: " "$m"
  curl -s -X POST "$LOCALAI/backend/shutdown" -H "Content-Type: application/json" -d "{\"model\":\"$m\"}" || true
  echo
done

say "2b. GGML_SYCL_FA_ONEDNN=0 beside GGML_SYCL_ENABLE_DNN=0 in every SYCL model (#124), each saved first"
mkdir -p "$SAVED/models"
python3 - "$M" "$SAVED/models" $SYCL_MODELS <<'PY'
import re, shutil, sys
from pathlib import Path
import yaml
models, saved, names = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
for n in names:
    p = models / f"{n}.yaml"
    text = p.read_text()
    before = yaml.safe_load(text)
    env = before.get("env") or {}
    if env.get("GGML_SYCL_FA_ONEDNN") == "0":
        print(f"{n}: has it already"); continue
    if env.get("GGML_SYCL_ENABLE_DNN") != "0":
        sys.exit(f"{n}: no GGML_SYCL_ENABLE_DNN=0 to put it beside")
    if not (saved / p.name).exists():
        shutil.copy2(p, saved / p.name)
    out, done = [], False
    for line in text.splitlines(keepends=True):
        y = re.match(r'^(\s+)GGML_SYCL_ENABLE_DNN: "0"\s*$', line)
        j = re.match(r'^(\s+)"GGML_SYCL_ENABLE_DNN": "0"(,?)\s*$', line)
        if y and not done:          # YAML: the line below
            out += [line, f'{y.group(1)}GGML_SYCL_FA_ONEDNN: "0"\n']; done = True
        elif j and not done:        # JSON: with the comma the object needs
            out += [f'{j.group(1)}"GGML_SYCL_ENABLE_DNN": "0",\n', f'{j.group(1)}"GGML_SYCL_FA_ONEDNN": "0"{j.group(2)}\n']
            done = True
        else:
            out.append(line)
    new = "".join(out)
    want = dict(before); want["env"] = {**env, "GGML_SYCL_FA_ONEDNN": "0"}
    if not done or yaml.safe_load(new) != want:
        sys.exit(f"{n}: the edit would change more than the one key; nothing written")
    p.write_text(new)                # in place: the file keeps its owner, mode and ACL
    print(f"{n}: + GGML_SYCL_FA_ONEDNN=0")
PY
# the staged files (deploy/vega/stage/models at this commit) are what the node holds, `pinned` aside;
# one staged and not yet here (a CPU twin) is put here
python3 - "$M" /root/stage-models <<'PY'
import shutil, sys
from pathlib import Path
import yaml
models, staged = Path(sys.argv[1]), Path(sys.argv[2])
bad = 0
for s in sorted(staged.glob("*.yaml")):
    p = models / s.name
    if not p.exists():
        shutil.copy2(s, p); shutil.chown(p, 568, 0); p.chmod(0o644)
        print(f"{s.name}: new here, from the staged file"); continue
    a, b = yaml.safe_load(p.read_text()), yaml.safe_load(s.read_text())
    a.pop("pinned", None); b.pop("pinned", None)
    if a != b:
        bad += 1
        print(f"{s.name}: DIFFERS from the staged file: "
              f"{sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))}")
sys.exit(1 if bad else 0)
PY
curl -s -X POST "$LOCALAI/models/reload"; echo

say "3. the three files replaced, compiled with each package's own python"
for p in $PKGS; do
  for f in $FILES; do install -m 0644 -o 568 -g 0 "$STAGE/$f" "$BK/$p/$f"; done
  docker exec $LA /backends/$p/venv/bin/python -m compileall -q \
    /backends/$p/vega_wrapper.py /backends/$p/app/hold/codex.py >/dev/null
  chown 568:0 $(for f in $PYC; do echo "$BK/$p/$f"; done)
  bf_restore "$BK/$p" "$SAVED/$p.modes" || { echo "$p: modes or ACLs not as before; BACKOUT below"; exit 1; }
  docker exec -w /backends/$p $LA /backends/$p/venv/bin/python -B -c "import vega_wrapper; vega_wrapper.engine_manifest; print('$p: imports ok')"
  echo "$p: now $(cut -c1-10 "$BK/$p/REVISION"), wrapper $(sha256sum "$BK/$p/vega_wrapper.py" | cut -c1-12)"
done
fi
rm -rf "$STAGE"

say "4. the node back to its mode; each collection gated, its fingerprints equal to the run before"
[ "$MODE" = running ] && $L node resume
check() {   # retried while the worker still holds the collection; a deferral (exit 3) asked again, 3 times
  local out="" rc=0 n=0
  for _ in $(seq 1 40); do
    out=$($L hold embedder check "$1" 2>&1) && rc=0 || rc=$?
    if echo "$out" | grep -q "held by the node"; then sleep 15; continue; fi
    if [ $rc -eq 3 ] && [ $n -lt 3 ]; then
      n=$((n + 1)); echo "$out" | grep "deferred" | head -2 | cut -c1-200; echo "$1: deferred, asked again ($n of 3)"; sleep 20; continue
    fi
    break
  done
  echo "$out" | grep -v "^\[warn:config\]"
  return $rc
}
for c in ${GATED_ONLY:-$GATED}; do
  echo "--- $c"
  was_paused=; case " $BEFORE_PAUSED " in *" $c "*) was_paused=1 ;; esac
  [ -n "$was_paused" ] || $L node pause "$c" >/dev/null
  if [ -n "$was_paused" ] && $L hold embedder check "$c" 2>&1 | grep -q "is held by"; then
    echo "$c: held by another writer and paused; gated when it next embeds (its fingerprint compared then)"; continue
  fi
  check "$c" || { echo "THE GATE FAILED on $c: it stays paused; BACKOUT below"; exit 1; }
  fp "$c" | tee -a /root/rollout-116-after.txt | while read -r col role run f; do
    read -r _ _ oldrun oldf < <(grep "^$col $role " /root/rollout-116-before.txt)
    [ "$run" != "$oldrun" ] || { echo "$col $role: still the run before ($run)"; exit 1; }
    if [ "$f" = "$oldf" ]; then
      echo "$col $role: ${oldrun:7:8} -> ${run:7:8}, fingerprint ${f:7:12} unchanged"
    elif case " $NEW_BYTES " in *" $col "*) true ;; *) false ;; esac; then
      echo "$col $role: ${oldrun:7:8} -> ${run:7:8}, fingerprint ${oldf:7:12} -> ${f:7:12}: MOVED, as FA_ONEDNN=0 may move an FA-on run; its multi-load gate passed (above)"
    else
      echo "$col $role: FINGERPRINT DIFFERS from the run before ($oldf -> $f): $col stays paused; tell the architecture session"; exit 1
    fi
  done || exit 1
  [ -n "$was_paused" ] || $L node resume "$c" >/dev/null
done

say "5. the models without a gate answer: the rerankers and the chat model"
for m in louise-rerank-bge louise-rerank-medcpt; do
  printf "%s: " "$m"
  curl -s -m 300 "$LOCALAI/v1/rerank" -H "Content-Type: application/json" \
    -d "{\"model\":\"$m\",\"query\":\"heart failure\",\"documents\":[\"cardiac failure treatment\",\"the moon\"],\"top_n\":2}" \
    | python3 -c "import sys,json; r=json.load(sys.stdin)['results']; print([(x['index'], round(x['relevance_score'], 4)) for x in r])"
done
printf "louise-chat-qwen3-8b: "
curl -s -m 300 "$LOCALAI/v1/chat/completions" -H "Content-Type: application/json" \
  -d '{"model":"louise-chat-qwen3-8b","messages":[{"role":"user","content":"Say OK. /no_think"}],"max_tokens":8}' \
  | python3 -c "import sys,json; print(repr(json.load(sys.stdin)['choices'][0]['message']['content'][:40]))"
echo "mode $(mode); paused collections: $(paused_collections) (before: ${BEFORE_PAUSED:-none})"
echo "rolled out: #116's wrapper in $PKGS"

# BACKOUT (the runs before this rollout come back, gated already):
#   node pause; shutdown the models above; then, in bash:
#   . /root/backend_files.sh; S=/mnt/ssd5/apps/localai/backends-bak-20261031-116
#   for p in <PKGS>; do
#     for f in vega_wrapper.py app/hold/codex.py REVISION __pycache__/vega_wrapper.cpython-312.pyc \
#              app/hold/__pycache__/codex.cpython-312.pyc; do install -m 0644 -o 568 -g 0 $S/$p/$f /mnt/ssd5/apps/localai/backends/$p/$f; done
#     bf_restore /mnt/ssd5/apps/localai/backends/$p $S/$p.modes
#   done
#   node resume (and the collections paused by step 4)
