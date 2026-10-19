#!/usr/bin/env sh
# deploy/localai-desktop/smoke.sh ENV_FILE [SHA] — the post-deploy smoke
# checks of a Docker Desktop peer with an AMD card (librocdxg, LocalAI on
# ROCm, VeGa; see README.md). The deploy gate is München's full suite on
# the same commit (docs/notes/STATUS.md, "the deploy gate"); this checks
# only what differs on THIS machine: the card, the network, the disk.
#
#   sh deploy/localai-desktop/smoke.sh D:/louise-peer2/louise.env 6fc29e8
#
# Prints one PASS / WARN / FAIL line per check and exits 1 if any FAILed.
# SMOKE_WAIT (s, default 240) bounds the wait for the worker's first round
# and run check after a restart. SMOKE_NO_EMBED=1 skips the live
# determinism probe (it loads the encoder if it is not loaded: ~5 GB VRAM).
# Secrets are never printed: env values are read one named variable at a time.
set -u
env_file=${1:?usage: smoke.sh ENV_FILE [SHA]}
sha=${2:-}
export MSYS_NO_PATHCONV=1
wait_s=${SMOKE_WAIT:-240}
fails=0

val() { sed -n "s/^$1=//p" "$env_file" | tail -1; }
project=$(val LOUISE_PROJECT); project=${project:-louise}
port=$(val LOUISE_HTTP_PORT); port=${port:-8742}
address=$(val LOUISE_NODE_ADDRESS)
image=$(val LOUISE_IMAGE); image=${image:-louise-rag:local}
worker="$project-worker-1"; web="$project-web-1"
localai=${SMOKE_LOCALAI:-louise-localai-localai-1}; vega=${SMOKE_VEGA:-louise-localai-vega-1}
localai_port=${SMOKE_LOCALAI_PORT:-30286}

pass() { echo "PASS  $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; fails=$((fails + 1)); }
http() { curl -s -m 10 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null; }
wpy() { docker exec -w /srv -e PYTHONPATH=/srv "$worker" python -c "$1" 2>&1; }

echo "smoke: $project ${sha:+at $sha }($(date -u +%FT%TZ))"

# 1. one image everywhere: the one just built
want=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null)
for c in "$worker" "$web" "$vega"; do
  got=$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null)
  if [ -z "$got" ]; then fail "image: $c is not running"
  elif [ "$got" != "$want" ]; then fail "image: $c runs ${got#sha256:} not $image ${want#sha256:}"
  else pass "image: $c runs $image (${want#sha256:})" | cut -c1-90; fi
done

# 2. the web, locally and at the address peers use (Tailscale)
[ "$(http "http://localhost:$port/healthz")" = 200 ] && pass "web: localhost:$port answers" \
  || fail "web: localhost:$port/healthz does not answer 200"
if [ -n "$address" ]; then
  [ "$(http "$address/healthz")" = 200 ] && pass "web: $address answers (the address peers dial)" \
    || fail "web: $address/healthz does not answer — peers cannot reach this node"
fi
declared=$(docker exec "$worker" python -c "import tomllib;print(tomllib.load(open('/config/platform.toml','rb')).get('node',{}).get('address',''))" 2>/dev/null)
[ "$declared" = "$address" ] && pass "address: platform.toml [node] = LOUISE_NODE_ADDRESS" \
  || fail "address: platform.toml [node] '$declared' differs from LOUISE_NODE_ADDRESS '$address' (04 §4.2: peers dial the declared one)"

# 3. the worker: started, a first round, no traceback, the run check
started=$(docker inspect -f '{{.State.StartedAt}}' "$worker" 2>/dev/null)
deadline=$(( $(date +%s) + wait_s ))
round=""; runok=""
while :; do
  log=$(docker logs --since "$started" "$worker" 2>&1)
  round=$(printf '%s\n' "$log" | grep -m1 "info:sched\]")
  runok=$(printf '%s\n' "$log" | grep -m1 "embedding enabled: run")
  { [ -n "$round" ] && [ -n "$runok" ]; } && break
  [ "$(date +%s)" -ge "$deadline" ] && break
  sleep 10
done
[ -n "$round" ] && pass "worker: first round done (${round%% *})" \
  || fail "worker: no scheduler round within ${wait_s}s of the start ($started)"
[ -n "$runok" ] && pass "worker: $(printf '%s' "$runok" | sed 's/.*\] //' | cut -c1-80)" \
  || warn "worker: no 'embedding enabled' run check within ${wait_s}s (no collection embeds, or the gate deferred)"
tb=$(printf '%s\n' "$log" | grep -c "Traceback")
[ "$tb" = 0 ] && pass "worker: no traceback since the start" || fail "worker: $tb traceback(s) since the start"
printf '%s\n' "$log" | grep -E "run-check\].*(not validated|refus|fail)" | head -2 | while read -r l; do
  echo "FAIL  worker: $l" | cut -c1-200; done
printf '%s\n' "$log" | grep -qE "run-check\].*(not validated|refus|fail)" && fails=$((fails + 1))

# 4. the node's mode and every collection's copy state
states=$(wpy "
from app import collections as cm, copy_state, node_state
nc = node_state.connect(); print('mode', node_state.worker_state(nc))
for c in cm.load_all():
    try: s = copy_state.of(c); print('copy', c.name, s.state)
    except Exception as e: print('copy', c.name, 'unreadable', type(e).__name__)
")
while read -r kind a b; do
  case "$kind" in
    mode) echo "INFO  node mode: $a";;
    copy) [ "$b" = in-use ] && pass "copy: $a in use" || warn "copy: $a is $b";;
    *) fail "mode/copy read: $(echo "$kind $a $b" | cut -c1-150)";;
  esac
done <<EOF
$states
EOF

# 5. LocalAI and VeGa's backends
[ "$(http "http://localhost:$localai_port/readyz")" = 200 ] && pass "localai: ready on :$localai_port" \
  || fail "localai: /readyz on :$localai_port does not answer 200"
backends=$(curl -s -m 10 "http://localhost:$localai_port/system" 2>/dev/null)
for b in louise-rocm-llama-cpp vega-llama-cpp; do
  printf '%s' "$backends" | grep -q "\"$b\"" && pass "localai: backend $b installed" \
    || fail "localai: backend $b missing (README steps 3-4)"
done

# 6. the card, through /dev/dxg and librocdxg
docker exec "$localai" test -e /dev/dxg && docker exec "$localai" test -e /usr/lib/libdxcore.so \
  && pass "card: /dev/dxg and libdxcore.so present in $localai" \
  || fail "card: /dev/dxg or /usr/lib/libdxcore.so missing in $localai (compose devices/volumes)"
[ "$(docker exec "$localai" printenv HSA_ENABLE_DXG_DETECTION 2>/dev/null)" = 1 ] \
  && pass "card: HSA_ENABLE_DXG_DETECTION=1" || fail "card: HSA_ENABLE_DXG_DETECTION is not 1: ROCm will not see the card"
vram=$(docker exec "$localai" /opt/rocm-wsl/bin/amd-smi metric -g 0 -m 2>/dev/null | sed -n 's/.*TOTAL_VRAM: *\([0-9]*\).*/\1/p' | head -1)
[ -n "$vram" ] && [ "$vram" -gt 0 ] && pass "card: amd-smi sees GPU 0 ($vram MB VRAM)" \
  || fail "card: amd-smi (dxg) does not see GPU 0"

# 7. invariant 12 as deployed: every model on the card carries the AMD mode
for y in $(docker exec "$localai" sh -c 'ls /models/*.yaml' 2>/dev/null); do
  body=$(docker exec "$localai" cat "$y")
  printf '%s' "$body" | grep -qE '^gpu_layers: *0\b' && continue      # a CPU twin
  printf '%s' "$body" | grep -qE 'ROCBLAS_DEFAULT_ATOMICS_MODE: *"?0"?' \
    && pass "determinism: ${y##*/} carries ROCBLAS_DEFAULT_ATOMICS_MODE=0" \
    || fail "determinism: ${y##*/} runs on the card without ROCBLAS_DEFAULT_ATOMICS_MODE=0 (invariant 12)"
done

# 8. invariant 12 measured, live: same texts, again, and reversed — bit-identical
if [ "${SMOKE_NO_EMBED:-0}" != 1 ]; then
  # through Louise's own client and each collection's recipe, as the stages call it
  wpy "
from app import collections as cm, embeddings as emb
texts = ['Louise smoke check one.', 'Il giorno dopo piovve su Perugia.',
         'Der dritte Satz ist etwas laenger als die anderen beiden.']
seen = set()
for c in cm.load_all():
    r = cm.recipe_for(c)
    if (r.url, r.model) in seen:
        continue
    seen.add((r.url, r.model))
    try:
        def run(xs):
            return dict(zip(xs, (tuple(v) for v in emb.embed_calibration(r, xs, raw=True))))
        a, b, z = run(texts), run(texts), run(texts[::-1])
        same = all(a[t] == b[t] == z[t] for t in texts)
        print('identical' if same else 'DIFFER', r.model, len(a[texts[0]]))
    except Exception as e:
        print('ERROR', r.model, type(e).__name__, str(e)[:120].replace(chr(10), ' '))
" > "${TMPDIR:-/tmp}/smoke-embed.$$" 2>&1
  # the client logs to the same stream (a cold start, a run check): verdicts only
  grep -qE '^(identical|DIFFER|ERROR) ' "${TMPDIR:-/tmp}/smoke-embed.$$" \
    || fail "determinism: probe gave no verdict: $(tail -1 "${TMPDIR:-/tmp}/smoke-embed.$$" | cut -c1-150)"
  while read -r verdict model rest; do
    case "$verdict" in
      identical) pass "determinism: $model bit-identical over 3 requests and a reversed order (dim $rest)";;
      DIFFER) fail "determinism: $model vectors differ between identical requests (invariant 12)";;
      ERROR) fail "determinism: $model probe failed: $rest";;
    esac
  done <<EOF
$(grep -E '^(identical|DIFFER|ERROR) ' "${TMPDIR:-/tmp}/smoke-embed.$$")
EOF
  rm -f "${TMPDIR:-/tmp}/smoke-embed.$$"
fi

# 9. the network: the pinned peers answer; the socket count stays small
for a in $(docker exec "$worker" python -c "import tomllib;print(' '.join(tomllib.load(open('/config/platform.toml','rb')).get('peers',{}).get('addresses',[])))" 2>/dev/null); do
  code=$(docker exec "$worker" python -c "import urllib.request;print(urllib.request.urlopen('$a/healthz',timeout=10).status)" 2>/dev/null)
  [ "$code" = 200 ] && pass "peers: $a answers from the worker" || fail "peers: $a does not answer from the worker"
done
socks=$(docker exec "$worker" sh -c 'echo $(($(wc -l < /proc/net/udp) - 1)) $(($(wc -l < /proc/net/tcp) - 1))')
udp=${socks% *}; tcp=${socks#* }
# docs/notes/dht-flood.md: ~10k flows took a home router down; 73-110 is normal
if [ "$udp" -lt 200 ] && [ "$tcp" -lt 400 ]; then pass "sockets: udp $udp, tcp $tcp"
else fail "sockets: udp $udp, tcp $tcp — a flow flood (docs/notes/dht-flood.md)"; fi

# 10. D: (this PC stores everything there; measurement is a writer)
free_gb=$(df -k /d 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}')
if [ -z "$free_gb" ]; then warn "disk: D: free space unreadable"
elif [ "$free_gb" -lt 20 ]; then fail "disk: D: has $free_gb GB free"
elif [ "$free_gb" -lt 100 ]; then warn "disk: D: has $free_gb GB free"
else pass "disk: D: has $free_gb GB free"; fi

echo "smoke: $fails failure(s)"
[ "$fails" = 0 ]
