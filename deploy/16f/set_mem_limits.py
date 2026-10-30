#!/usr/bin/env python3
"""deploy/16f/set_mem_limits.py — the memory caps of deploy/truenas-app.yaml
(#89) on the LIVE louise-rag app's web and worker.

Operator, 2026-10-30: "The worker's main process was killed by the kernel —
this must not happen without an automatic recovery not involving this agent.
This applies to any worker." The 14:14 kill was a GLOBAL OOM: no container
had a cap, so the kernel chose the largest process on the host. With a cap
the OOM stays inside the container that ran away, and `restart:
unless-stopped` brings it back. The runbook edits the live config in place
(flatten_mounts.py, set_platform_image.py) and never replaces it with the
YAML in the repository, so the caps arrive the same way.

Run on the node, by the runbook, between the stop job and the start, through
TrueNAS's API client IN THIS PROCESS: the app's config carries the admin
token, so it never reaches a command line, a log or this output. It prints
the two keys per service, before and after. Idempotent.
"""
from __future__ import annotations

import sys

APP = "louise-rag"
#: deploy/truenas-app.yaml's values (#89); swap equal to the cap: no swap thrash first
LIMITS = {"web": "12g", "worker": "24g"}
KEYS = ("mem_limit", "memswap_limit")


def apply(cfg: dict, limits: dict[str, str] = LIMITS) -> dict[str, tuple]:
    """Set both keys on each service, in place; return what each held before.
    Refuses a config it does not recognise rather than guessing at it."""
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")
    for name in limits:
        if not isinstance(services.get(name), dict):
            raise ValueError(f"{name}: no such service; not touched")
    before = {name: tuple(services[name].get(k) for k in KEYS) for name in limits}
    for name, cap in limits.items():
        for k in KEYS:
            services[name][k] = cap
    return before


def main(argv: list[str]) -> int:
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            before = apply(cfg)
        except ValueError as e:
            print(f"set_mem_limits: {e}", file=sys.stderr)
            return 2
        for name, cap in LIMITS.items():
            print(f"{name:7} mem_limit/memswap_limit {before[name]} -> ({cap}, {cap})")
        if all(before[name] == (cap, cap) for name, cap in LIMITS.items()):
            print("unchanged")
            return 0
        try:
            c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"set_mem_limits: app.update failed ({type(e).__name__}); "
                  f"see /var/log/middlewared.log", file=sys.stderr)
            return 1
        after = c.call("app.config", APP)
        got = {name: tuple(((after.get("services") or {}).get(name) or {}).get(k) for k in KEYS)
               for name in LIMITS}
        if any(got[name] != (cap, cap) for name, cap in LIMITS.items()):
            print(f"set_mem_limits: NOT set ({got})", file=sys.stderr)
            return 1
        print("set")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
