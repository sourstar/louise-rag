#!/usr/bin/env python3
"""deploy/16f/set_mem_limits.py — the memory caps of deploy/truenas-app.yaml
(#89) on the LIVE louise-rag app's web and worker, and the worker's allocator
setting (MALLOC_ARENA_MAX, 2026-11-07).

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
#: deploy/truenas-app.yaml's allocator settings: glibc's own cap on its
#: per-thread heaps (mallopt M_ARENA_MAX). München, 2026-11-07: the worker's
#: ~100 threads held 47 arenas, 2.9 GB, main 2.4 -> 7.1 GB and OOM-killed; at 2,
#: main 1.6-3.8 GB over an hour, 0 kills, the rates unchanged
ENV = {"worker": {"MALLOC_ARENA_MAX": "2"}}


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


def apply_env(cfg: dict, env: dict[str, dict[str, str]] = ENV) -> dict[str, dict]:
    """Set each service's allocator variables in its `environment`, in place;
    return what each held before (None where unset). The environment may be
    a mapping or compose's list of NAME=VALUE; a list is kept a list."""
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")
    for name in env:
        if not isinstance(services.get(name), dict):
            raise ValueError(f"{name}: no such service; not touched")
    before: dict[str, dict] = {}
    for name, want in env.items():
        e = services[name].setdefault("environment", {})
        if isinstance(e, list):
            held = dict(x.split("=", 1) if "=" in x else (x, None) for x in e)
            before[name] = {k: held.get(k) for k in want}
            kept = [x for x in e if x.split("=", 1)[0] not in want]
            services[name]["environment"] = kept + [f"{k}={v}" for k, v in want.items()]
        elif isinstance(e, dict):
            before[name] = {k: e.get(k) for k in want}
            e.update(want)
        else:
            raise ValueError(f"{name}: an environment that is neither a mapping nor a list; not touched")
    return before


def _env_of(service: dict) -> dict:
    e = service.get("environment") or {}
    if isinstance(e, list):
        return dict(x.split("=", 1) if "=" in x else (x, None) for x in e)
    return dict(e)


def main(argv: list[str]) -> int:
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            before = apply(cfg)
            env_before = apply_env(cfg)
        except ValueError as e:
            print(f"set_mem_limits: {e}", file=sys.stderr)
            return 2
        for name, cap in LIMITS.items():
            print(f"{name:7} mem_limit/memswap_limit {before[name]} -> ({cap}, {cap})")
        for name, want in ENV.items():
            for k, v in want.items():
                print(f"{name:7} {k} {env_before[name][k]} -> {v}")
        if (all(before[name] == (cap, cap) for name, cap in LIMITS.items())
                and all(env_before[name][k] == v for name, want in ENV.items() for k, v in want.items())):
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
        # the allocator variables, read back by name only: the environment
        # also carries the admin token, which is never printed
        services = after.get("services") or {}
        missing = [f"{name}.{k}" for name, want in ENV.items() for k, v in want.items()
                   if _env_of(services.get(name) or {}).get(k) != v]
        if missing:
            print(f"set_mem_limits: NOT set: {', '.join(missing)}", file=sys.stderr)
            return 1
        print("set")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
