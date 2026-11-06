#!/usr/bin/env bash
# create-normattiva-archive.sh — the FULL Normattiva collection on the node
# (docs/notes/normattiva-full-corpus.md; the operator's decision of
# 2026-10-13: a new collection beside `normattiva`, which stays as it is).
# Run on the node:  ssh root@node bash -s < deploy/16f/create-normattiva-archive.sh
# after the code carrying app/acquire/export_job.py is live.
#
# Like create-normattiva.sh@c572c3b, inside the live WORKER container. The recipe is
# docs/notes/normattiva-archive-recipe.toml: `normattiva`'s space byte for
# byte (the same space_id: the vectors of every shared chunk are carried,
# RULE 1), the origin extended by the yearly exports.
#
# What differs from create-normattiva.sh@c572c3b:
#   5. the collection file says `profile = "archive"` instead of
#      `slices = "all"` — the profile that adds the export files to the 23
#      zips (app/acquire/specs.py); `slices` and `profile` cannot both be
#      given (07 §7.8), so the `slices = []` line `create` wrote is replaced.
#   6. the first pass runs about 330 export jobs one after the other, each
#      a search the origin builds into a zip (manual §1.3.5–1.3.7); the
#      time of a year's job is OWED before this runs (the note's §5): the
#      origin answered no job on the evening of 2026-10-13 after a run of
#      probes. Measure on a clean day, then create.
set -euo pipefail

ROOT=/mnt/ssd4/apps/louise-rag
WEB=ix-louise-rag-worker-1
ENCODER=http://192.168.178.10:30286
MODEL=vega-dar-legal-it
NAME=normattiva-archive

say() { printf '\n=== %s\n' "$*"; }

say "0. the live code carries the export mode, and the model is served"
cat "$ROOT/src/.deployed-commit"
docker exec "$WEB" python -c "from app.acquire import export_job, specs; from app.plugins.normattiva import SOURCE; assert SOURCE.PARSER_VERSION == 7, SOURCE.PARSER_VERSION; n = sum(1 for f in specs.NORMATTIVA_SPEC.endpoints[0].static_files if export_job.is_export(f)); assert n > 300, n; print('parser', SOURCE.PARSER_VERSION, 'export files', n)"
curl -s --max-time 10 "$ENCODER/v1/models" | tr ',' '\n' | grep -q "\"$MODEL\"" && echo "$MODEL is listed"
test ! -e "$ROOT/config/collections/$NAME.toml" || { echo "$NAME.toml exists already: nothing to create"; exit 1; }

say "1. the recipe, where the container reads it"
mkdir -p "$ROOT/config/recipes"
cp "${RECIPE:-$ROOT/src/docs/notes/normattiva-archive-recipe.toml}" "$ROOT/config/recipes/$NAME.toml"
grep -c "TO-READ-ON-THE-NODE" "$ROOT/config/recipes/$NAME.toml" && { echo "the recipe still has placeholders"; exit 1; } || true

say "2. hold collection create (the Codex, the file, the links; minutes: the calibration set is embedded)"
docker exec "$WEB" python -m app.louise hold collection create "$NAME" \
  --recipe "/config/recipes/$NAME.toml" --name "$NAME" \
  --encoder "$ENCODER" --api vega --embed-model "$MODEL" --query-model "$MODEL"
cat "$ROOT/config/collections/$NAME.toml"
ls -la "$ROOT/data/collections/$NAME/"

say "3. the licence: the terms, printed, then acknowledged as read"
docker exec "$WEB" python -m app.louise hold collection ack "$NAME" || true
docker exec "$WEB" python -m app.louise hold collection ack "$NAME" --yes

say "4. the node's badge for the embedder: GPU (library-device-badge)"
grep -q '^device' "$ROOT/config/collections/$NAME.toml" || \
  python3 - "$ROOT/config/collections/$NAME.toml" <<'PY'
import re, sys
p = sys.argv[1]; s = open(p, encoding="utf-8").read()
s = re.sub(r"(\[encoder\][^\[]*?)(\n\[|\Z)", lambda m: m.group(1).rstrip("\n") + '\ndevice = "gpu"\n' + m.group(2), s, count=1, flags=re.S)
open(p, "w", encoding="utf-8").write(s)
PY

say "5. profile = \"archive\": the 23 zips and the yearly exports (the worker re-reads the file between rounds)"
# `profile` beside `acquire_enabled`: a selection other than the whole is
# the older spelling the loader keeps, and the worker acquires only when the
# file says so (app/collections.py; tests/test_collection_file.py)
sed -i 's/^slices = \[\]$/profile = "archive"\nacquire_enabled = true/' "$ROOT/config/collections/$NAME.toml"
grep -n '^profile\|^slices' "$ROOT/config/collections/$NAME.toml"
cat "$ROOT/config/collections/$NAME.toml"

say "6. what the worker does next"
echo "watch:  docker logs --since 10m ix-louise-rag-worker-1 | grep -i $NAME | tail"
echo "        the 23 zips are adopted by their directories if /corpora/$NAME holds copies; the exports run one job at a time"
echo "then:   hold check $NAME   (the guinea pig again: never listed in platform.toml; fix what it finds missing)"
