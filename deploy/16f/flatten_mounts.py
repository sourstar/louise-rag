#!/usr/bin/env python3
"""deploy/16f/flatten_mounts.py — one mount per kind, a folder per collection
(operator, 2026-10-12), in the louise-rag app.

In every service that has them: every bind mount whose CONTAINER path is a
collection's `origin` or `shard` (or something mounted inside one, as
wikivec's `origin/data`) leaves; `/corpora`, `/shards` and the custom
origins named below come in; and `SHARDS_ROOT` / `ORIGINS_ROOT` are set, so
`create` links a new collection's folders there (app/hold/collection.py).
The per-collection paths themselves become symlinks on the host, made by the
runbook before this runs, so the paths every manifest stores do not move.

Run on the node with the app STOPPED, through TrueNAS's API client in this
process (set_platform_image.py's reasons: the config carries the admin token,
so it never reaches a command line, a log or this output). Prints the mounts
it removes and adds and the two variables, nothing else. Idempotent.
"""
from __future__ import annotations

import re
import sys

APP = "louise-rag"
#: a collection's origin or shard, or anything mounted inside one
PER_COLLECTION = re.compile(r"^/data/collections/[^/]+/(origin|shard)(/.*)?$")
#: (host, container, read-only in `web`)
SHARED = (("/mnt/tank/louise-corpora", "/corpora", True),
          ("/mnt/tank/apps/louise-shard", "/shards", False),
          ("/mnt/tank/media/public/books/project_gutenberg", "/corpora-custom/gutenberg", True),
          # the bundles (09 §9.7): a second copy of the vectors, on tank with
          # the SHARDs (operator, 2026-10-19); the web never reads them
          ("/mnt/tank/apps/louise-bundles", "/bundles", True))
ENV = {"SHARDS_ROOT": "/shards", "ORIGINS_ROOT": "/corpora", "BUNDLES_ROOT": "/bundles"}
SERVICES = ("web", "worker")


def _parts(v) -> tuple[str | None, str | None]:
    if isinstance(v, str):
        bits = v.split(":")
        return (bits[0], bits[1] if len(bits) > 1 else None)
    if isinstance(v, dict):
        return (v.get("source"), v.get("target"))
    return (None, None)


def apply(cfg: dict, shared=SHARED, env=ENV) -> list[str]:
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")
    said: list[str] = []
    for name in SERVICES:
        svc = services.get(name)
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if not isinstance(vols, list):
            raise ValueError(f"service {name} has no volume list; not touched")
        keep = []
        for v in vols:
            src, dst = _parts(v)
            if dst and PER_COLLECTION.match(dst):
                said.append(f"{name}: remove {src} -> {dst}")
            else:
                keep.append(v)
        targets = {_parts(v)[1] for v in keep}
        for host, target, ro_in_web in shared:
            if target not in targets:
                keep.append(f"{host}:{target}" + (":ro" if ro_in_web and name == "web" else ""))
                said.append(f"{name}: add {host} -> {target}" + (" (ro)" if ro_in_web and name == "web" else ""))
        svc["volumes"] = keep
        e = svc.setdefault("environment", {})
        if not isinstance(e, dict):
            raise ValueError(f"service {name}'s environment is not a mapping; not touched")
        for k, val in env.items():
            if e.get(k) != val:
                e[k] = val
                said.append(f"{name}: {k}={val}")
    return said


def main(argv: list[str]) -> int:
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            said = apply(cfg)
        except ValueError as e:
            print(f"flatten_mounts: {e}", file=sys.stderr)
            return 2
        for line in said:
            print(line)
        if not said:
            print("nothing to change")
            return 0
        try:
            c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"flatten_mounts: app.update failed ({type(e).__name__}); see /var/log/middlewared.log",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
