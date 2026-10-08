"""Switch-over step 3 (docs/notes/vega.md §12): the weights RAGE uses, mounted
READ-ONLY into the LocalAI app at /models/weights/<name> — the same host paths
RAGE mounts, and the same paths relative to the models directory as RAGE's
YAMLs name (`weights/<name>/…`), so a model's options name its weights as
RAGE's do. Run on the node as root:

    python3 mounts.py [--apply]

Reads ONLY the app's `storage` section, appends the missing mounts to its
`additional_storage` (host_path, read_only, NO ACL: TrueNAS would otherwise
rewrite the ACL of RAGE's directories; uid 568 already reads them through
their inherited NFSv4 entry `user:apps`), and calls `app.update` with that
section alone. Then checks, without printing them, that every other key of the
app's configuration is byte-identical (sha256 of its canonical JSON) and that
the storage is exactly the old one plus the new mounts. Prints no secret: the
configuration's other sections are hashed, never shown.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys

APP = "localai"
#: host path -> mount path in the LocalAI container (RAGE's mounts, read on
#: the node 2026-10-08: `docker inspect ix-louise-rage-rage-1`, Mounts)
WEIGHTS = {
    "/mnt/ssd5/apps/bge-m3-models": "/models/weights/bge-m3",
    "/mnt/ssd5/apps/louise-rage/models/chat": "/models/weights/chat",
    "/mnt/ssd5/apps/granite-models": "/models/weights/granite",
    "/mnt/ssd5/apps/llama-embed": "/models/weights/llama-embed",
    "/mnt/ssd5/apps/madlad-models": "/models/weights/madlad",
    "/mnt/ssd5/apps/medcpt-models": "/models/weights/medcpt",
}


def midclt(*args: str) -> object:
    out = subprocess.run(["midclt", "call", *args], check=True, capture_output=True,
                         text=True).stdout
    return json.loads(out) if out.strip() else None


def h(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"))
                          .encode()).hexdigest()


def rest(cfg: dict) -> dict:
    """Everything but the storage and `ix_context`: TrueNAS's record of the
    last operation (`operation`, `is_update`, `scale_version`…), which every
    update rewrites — on 2026-10-08 only its `scale_version` moved, checked
    leaf by leaf against the dataset's snapshot (vega.md §12)."""
    return {k: v for k, v in cfg.items() if k not in ("storage", "ix_context")}


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    cfg = midclt("app.config", APP)
    assert isinstance(cfg, dict)
    storage = cfg["storage"]
    extra = storage.get("additional_storage", [])
    have = {e.get("host_path_config", {}).get("path") for e in extra if e.get("type") == "host_path"}
    taken = {e.get("mount_path") for e in extra}
    new = [{"type": "host_path", "read_only": True, "mount_path": mount,
            "host_path_config": {"acl_enable": False, "path": host}}
           for host, mount in WEIGHTS.items() if host not in have and mount not in taken]
    print(f"additional_storage: {len(extra)} entries; to add {len(new)}:")
    for e in new:
        print(f"  {e['host_path_config']['path']} -> {e['mount_path']} (read-only)")
    if not new or not apply:
        return 0
    want = copy.deepcopy(storage)
    want["additional_storage"] = list(extra) + new
    before_rest = h(rest(cfg))
    subprocess.run(["midclt", "call", "-j", "app.update", APP,
                    json.dumps({"values": {"storage": want}})],
                   check=True, capture_output=True, text=True)
    after = midclt("app.config", APP)
    assert isinstance(after, dict)
    same_rest = h(rest(after)) == before_rest
    same_storage = h(after["storage"]) == h(want)
    print(f"other sections byte-identical: {same_rest}; storage as intended: {same_storage}")
    return 0 if same_rest and same_storage else 1


if __name__ == "__main__":
    sys.exit(main())
