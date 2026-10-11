#!/usr/bin/env python3
"""deploy/16f/rename_mounts.py OLD_HOST NEW_HOST OLD_DIR NEW_DIR — in every
service of the louise-rag app, a bind mount whose host source starts with
OLD_HOST gets NEW_HOST instead, and its container path's last segment
OLD_DIR becomes NEW_DIR.

Written 2026-10-11 for the SHARD's directory (`drag/` → `shard/`, 09's
opening note): `tank/apps/louise-drag` is renamed `louise-shard` and each
collection's `/data/collections/<name>/drag` mount point `…/shard`, with the
app STOPPED. Through TrueNAS's API client in this process, as
drop_mounts.py: the config carries the admin token, so it never reaches a
command line, a log or this output. Prints the mounts it changes — host and
container paths, nothing else.

    rename_mounts.py /mnt/tank/apps/louise-drag/ /mnt/tank/apps/louise-shard/ drag shard
"""
from __future__ import annotations

import sys

APP = "louise-rag"


def apply(cfg: dict, old_host: str, new_host: str, old_dir: str, new_dir: str) -> list[str]:
    """Rename the matching volumes in place; return them as `svc: src -> dst
    => src' -> dst'`. Both volume shapes; refuses a config it does not
    recognise and a prefix too short to be a deliberate one."""
    for p in (old_host, new_host):
        if not p.startswith("/mnt/") or len(p.rstrip("/").split("/")) < 4:
            raise ValueError(f"{p!r}: give a prefix under /mnt/<pool>/<dataset>/…")
    if "/" in old_dir or "/" in new_dir or not old_dir or not new_dir:
        raise ValueError("OLD_DIR and NEW_DIR are one path segment each")
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")

    def dst_of(d: str) -> str:
        head, _, last = d.rstrip("/").rpartition("/")
        return f"{head}/{new_dir}" if last == old_dir else d

    changed: list[str] = []
    for name, svc in services.items():
        vols = (svc or {}).get("volumes")
        if not isinstance(vols, list):
            continue
        for i, v in enumerate(vols):
            if isinstance(v, str):
                src, _, rest = v.partition(":")
                dst, sep, mode = rest.partition(":")
                if not src.startswith(old_host):
                    continue
                src2, dst2 = new_host + src[len(old_host):], dst_of(dst)
                vols[i] = f"{src2}:{dst2}{sep}{mode}"
            elif isinstance(v, dict) and str(v.get("source") or "").startswith(old_host):
                src, dst = str(v["source"]), str(v.get("target") or "")
                src2, dst2 = new_host + src[len(old_host):], dst_of(dst)
                v["source"], v["target"] = src2, dst2
            else:
                continue
            changed.append(f"{name}: {src} -> {dst} => {src2} -> {dst2}")
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print(f"usage: {argv[0]} OLD_HOST NEW_HOST OLD_DIR NEW_DIR", file=sys.stderr)
        return 2
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            changed = apply(cfg, *argv[1:])
        except ValueError as e:
            print(f"rename_mounts: {e}", file=sys.stderr)
            return 2
        for line in changed:
            print(f"rename {line}")
        if not changed:
            print("nothing matches")
            return 0
        try:
            c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"rename_mounts: app.update failed ({type(e).__name__}); see /var/log/middlewared.log",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
