#!/usr/bin/env bash
# create-normattiva.sh — the Normattiva collection on the node, after
# deploy-louise-normattiva.sh has put the code of 4493c15 (or later) live.
# Run on the node:  ssh root@node bash -s < deploy/16f/create-normattiva.sh
#
# The commands run INSIDE the live WORKER container: it carries the mounts
# (/config, /data, /corpora, /shards) and the env `create` pins
# (LOUISE_PLATFORM_IMAGE, 07 §7.3) and the VeGa client needs (VEGA_URL).
# The recipe is docs/notes/normattiva-recipe.toml of the shipped tree; the
# image carries no docs/, so it is copied into /config/recipes first.
#
# What each step is and why it stops where it does:
#   1. create — mints the PERMANENT Codex (collection_id, space_id) from the
#      recipe; holds the endpoint's stated run to the recipe's engine and
#      weights (G5), embeds the calibration set, writes 07 §7.8's file with
#      slices = [] — nothing until the operator says so (help.html) — and
#      links origin/ into /corpora. Step 5 sets slices = "all" after the ack,
#      so the worker acquires the `full` profile itself (codex_defaults).
#   2. ack — the licence is YELLOW (CC BY 4.0, an NC window before it): the
#      terms are PRINTED, then recorded as read (invariant 9). Without it
#      the worker withholds scan and archive, and nothing is fetched.
#   3. the worker takes it from there: fetch (23 zips, 7.8 GB, two
#      connections), scan, parse, fts, embed on vega-dar-legal-it. Pin the
#      model through LocalAI's API once it embeds (vega.md §12's rule).
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
# the worker, not the web: the web mounts /corpora read-only, and `create`
# links origin/ into a /corpora/<name> it makes (2026-10-12: refused there)
WEB=ix-louise-rag-worker-1
ENCODER=http://192.168.178.10:30286
MODEL=vega-dar-legal-it

say() { printf '\n=== %s\n' "$*"; }

say "0. the live code is the shipped one, and the model is served"
cat "$ROOT/src/.deployed-commit"
docker exec "$WEB" python -c "from app.plugins.normattiva import SOURCE; assert SOURCE.PARSER_VERSION == 6, SOURCE.PARSER_VERSION; print('parser', SOURCE.PARSER_VERSION)"
curl -s --max-time 10 "$ENCODER/v1/models" | tr ',' '\n' | grep -q "\"$MODEL\"" && echo "$MODEL is listed"
test ! -e "$ROOT/config/collections/normattiva.toml" || { echo "normattiva.toml exists already: nothing to create"; exit 1; }

say "1. the recipe, where the container reads it"
mkdir -p "$ROOT/config/recipes"
cp "${RECIPE:-$ROOT/src/docs/notes/normattiva-recipe.toml}" "$ROOT/config/recipes/normattiva.toml"
grep -c "TO-READ-ON-THE-NODE" "$ROOT/config/recipes/normattiva.toml" && { echo "the recipe still has placeholders"; exit 1; } || true

say "2. hold collection create (the Codex, the file, the links; minutes: the calibration set is embedded)"
docker exec "$WEB" python -m app.louise hold collection create normattiva \
  --recipe /config/recipes/normattiva.toml --name normattiva \
  --encoder "$ENCODER" --api vega --embed-model "$MODEL" --query-model "$MODEL"
cat "$ROOT/config/collections/normattiva.toml"
ls -la "$ROOT/data/collections/normattiva/"

say "3. the licence: the terms, printed, then acknowledged as read"
docker exec "$WEB" python -m app.louise hold collection ack normattiva || true
docker exec "$WEB" python -m app.louise hold collection ack normattiva --yes

say "4. the node's badge for the embedder: GPU (library-device-badge)"
grep -q '^device' "$ROOT/config/collections/normattiva.toml" || \
  python3 - "$ROOT/config/collections/normattiva.toml" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
s = re.sub(r"(\[encoder\][^\[]*?)(\n\[|\Z)", lambda m: m.group(1).rstrip("\n") + '\ndevice = "gpu"\n' + m.group(2), s, count=1, flags=re.S)
open(p, "w", encoding="utf-8").write(s)
PY
cat "$ROOT/config/collections/normattiva.toml"

say "5. slices = \"all\": the worker fetches the whole corpus (it re-reads the file between rounds)"
sed -i 's/^slices = \[\]$/slices = "all"/' "$ROOT/config/collections/normattiva.toml"
grep -n '^slices' "$ROOT/config/collections/normattiva.toml"

say "6. what the worker does next (it reads the file at its next pass)"
echo "watch:  docker logs --since 10m ix-louise-rag-worker-1 | grep -i normattiva | tail"
echo "then:   hold check normattiva  (the quarantine's guinea pig: never listed; fix what it finds missing)"
echo "pin:    curl -X PUT $ENCODER/models/toggle-pinned/$MODEL/pin   once it embeds"
