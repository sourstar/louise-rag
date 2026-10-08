#!/usr/bin/env python3
"""deploy/16f/drop_mounts.py PREFIX — remove from the louise-rag app every
bind mount whose host source starts with PREFIX, in every service.

Written 2026-10-08 for the genome collections' deletion (operator's yes):
a container does not start while a bind source is missing, so the origin
folders could not be deleted before their mounts left the compose. Run on
the node with the app STOPPED, through TrueNAS's API client in this process
(set_platform_image.py's reasons: the config carries the admin token, so
it never reaches a command line, a log or this output). Prints the mounts
it removes — host and container paths, nothing else.
"""
from __future__ import annotations

import sys

APP = "louise-rag"


def _source(volume) -> str | None:
    if isinstance(volume, str):
        return volume.split(":", 1)[0]
    if isinstance(volume, dict):
        return str(volume.get("source") or "") or None
    return None


def apply(cfg: dict, prefix: str) -> list[str]:
    """Drop the matching volumes in place; return them, as `src -> dst`.
    Refuses a config it does not recognise, and a prefix too short to be
    a deliberate one."""
    if not prefix.startswith("/mnt/") or len(prefix.rstrip("/").split("/")) < 4:
        raise ValueError(f"{prefix!r}: give a prefix under /mnt/<pool>/<dataset>/…")
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")
    removed: list[str] = []
    for name, svc in services.items():
        vols = (svc or {}).get("volumes")
        if not isinstance(vols, list):
            continue
        keep = []
        for v in vols:
            src = _source(v)
            if src and src.startswith(prefix):
                dst = v.split(":", 2)[1] if isinstance(v, str) else v.get("target")
                removed.append(f"{name}: {src} -> {dst}")
            else:
                keep.append(v)
        svc["volumes"] = keep
    return removed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} /mnt/<pool>/<dataset>/<prefix>", file=sys.stderr)
        return 2
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            removed = apply(cfg, argv[1])
        except ValueError as e:
            print(f"drop_mounts: {e}", file=sys.stderr)
            return 2
        for line in removed:
            print(f"remove {line}")
        if not removed:
            print("nothing matches")
            return 0
        try:
            c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"drop_mounts: app.update failed ({type(e).__name__}); see /var/log/middlewared.log",
                  file=sys.stderr)
            return 1
        left = apply(c.call("app.config", APP), argv[1])
        if left:
            print(f"drop_mounts: still mounted: {left}", file=sys.stderr)
            return 1
        print("done")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
