#!/usr/bin/env bash
# flatten_localai_weights.sh — LocalAI's model weights under ONE mount
# (operator, 2026-10-12: "currently each collection is adding a new mount path").
#
# Before: six read-only mounts, /models/weights/<x> <- /mnt/ssd5/apps/<x-dataset or dir>.
# After:  one, /models/weights <- /mnt/ssd5/apps/models (read-only), with
#         bge-m3/ llama-embed/ medcpt/ as renamed datasets (no copy) and
#         granite/ madlad/ chat/ copied from plain folders (the originals stay).
# The model YAMLs name `weights/<x>/…`, unchanged, so no model file moves and
# no encoder run changes. Only LocalAI mounts these paths (checked 2026-10-12).
#
#   ssh root@node bash -s < deploy/16f/flatten_localai_weights.sh
#
# Idempotent: each step skips what is done. Stops at the first failure.
set -euo pipefail
A=/mnt/ssd5/apps
say() { printf '\n=== %s\n' "$*"; }

say "stop localai"
J=$(midclt call app.stop localai)
for i in $(seq 1 120); do
  js=$(midclt call core.get_jobs "[[\"id\",\"=\",$J]]" | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  case "$js" in SUCCESS|FAILED|ABORTED) break;; esac; sleep 3
done
echo "stop: $js"; [ "$js" = SUCCESS ]

say "one weights dataset"
zfs list -H -o name ssd5/apps/models >/dev/null 2>&1 || zfs create ssd5/apps/models
for pair in bge-m3-models:bge-m3 llama-embed:llama-embed medcpt-models:medcpt; do
  src=${pair%%:*}; dst=${pair##*:}
  if zfs list -H -o name "ssd5/apps/$src" >/dev/null 2>&1; then
    zfs rename "ssd5/apps/$src" "ssd5/apps/models/$dst" && echo "renamed ssd5/apps/$src -> ssd5/apps/models/$dst"
  fi
done
for pair in granite-models:granite madlad-models:madlad louise-rage/models/chat:chat; do
  src=${pair%%:*}; dst=${pair##*:}
  if [ ! -e "$A/models/$dst" ]; then
    cp -a "$A/$src" "$A/models/$dst.part" && mv "$A/models/$dst.part" "$A/models/$dst" \
      && echo "copied $A/$src -> $A/models/$dst ($(du -sh "$A/models/$dst" | cut -f1))"
  fi
done
ls -la "$A/models"

say "localai: six weights mounts -> one"
python3 - <<'P'
from truenas_api_client import Client
APP, HOST, TARGET = "localai", "/mnt/ssd5/apps/models", "/models/weights"
with Client() as c:
    cfg = c.call("app.config", APP)
    storage = cfg["storage"]
    extra = storage.get("additional_storage") or []
    keep = [e for e in extra if not str(e.get("mount_path", "")).startswith(TARGET + "/")]
    for e in extra:
        if e not in keep:
            print("remove", e["host_path_config"]["path"], "->", e["mount_path"])
    if not any(e.get("mount_path") == TARGET for e in keep):
        keep.append({"type": "host_path", "mount_path": TARGET, "read_only": True,
                     "host_path_config": {"acl_enable": False, "path": HOST}})
        print("add", HOST, "->", TARGET, "(ro)")
    if keep != extra:
        storage["additional_storage"] = keep
        c.call("app.update", APP, {"values": {"storage": storage}}, job=True)
        print("updated")
    else:
        print("nothing to change")
P

say "start localai"
s=$(midclt call app.query '[["name","=","localai"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
[ "$s" = RUNNING ] || midclt call app.start localai >/dev/null
for i in $(seq 1 90); do
  s=$(midclt call app.query '[["name","=","localai"]]' | python3 -c "import sys,json; print(json.load(sys.stdin)[0]['state'])")
  [ "$s" = RUNNING ] && break; sleep 3
done
echo "state: $s"; [ "$s" = RUNNING ]
docker exec ix-localai-localai-1 sh -c 'ls /models/weights'
