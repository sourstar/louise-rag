#!/usr/bin/env python3
"""deploy/16f/set_platform_image.py IMAGE_ID — tell the louise-rag app's web
and worker the image they start in, as LOUISE_PLATFORM_IMAGE.

07 §7.3: `create` pins "the digest of the platform the command runs in", and
a container cannot see its own image id, so the deploy hands it over and
checks it against the running image (STATUS, 2026-09-14). The runbook
stopped doing it: from 2026-09-21 to 2026-10-07 the node declared
`sha256:85fd59f1…` (pubmed's Codex image) while it ran every later build,
and every Codex created meanwhile names an image that did not build it —
a version-4 one, an image that cannot even read it (operator's yes,
2026-10-07: fix it).

It also CARRIES the web's `GPU_PROBE_URL` to the worker (CARRIED): the
worker's card recorder (app/card_record.py, #164) asks the same probe the
web's GPU panel does, and the node's app config gave it to the web alone
(München, 2026-11-01). The web's value is the configured truth; nobody
types it twice (RULE 2), and it rides the same `app.update`.

Run on the node, by the runbook, between the stop job and the start. It
goes through TrueNAS's API client IN THIS PROCESS: the app's config carries
the admin token, so it never reaches a command line, a log or this output.
What it prints is the variable alone, before and after.
"""
from __future__ import annotations

import re
import sys

APP = "louise-rag"
KEY = "LOUISE_PLATFORM_IMAGE"
SERVICES = ("web", "worker")
#: what the worker takes from the web's environment: addresses, never secrets
CARRIED = ("GPU_PROBE_URL",)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def apply(cfg: dict, want: str) -> dict[str, str | None]:
    """Set the variable in each service's environment (a mapping), in place;
    return what each held before. Refuses a config it does not recognise
    rather than guessing at it."""
    if not _DIGEST.match(want):
        raise ValueError(f"an image id is sha256:<64 hex>, got {want!r}")
    services = cfg.get("services")
    if not isinstance(services, dict):
        raise ValueError("the app config has no services mapping; not touched")
    before: dict[str, str | None] = {}
    for name in SERVICES:
        env = (services.get(name) or {}).get("environment")
        if not isinstance(env, dict):
            raise ValueError(f"{name}: environment is not a mapping; not touched")
        before[name] = env.get(KEY)
    for name in SERVICES:
        services[name]["environment"][KEY] = want
    return before


def carry(cfg: dict) -> list[str]:
    """Give the worker each CARRIED variable the web has, in place; one line
    per change ("GPU_PROBE_URL None -> http://gpu-probe:8080"). A variable
    the web lacks is left as the worker has it. Call after `apply`, which
    has checked the shape."""
    services = cfg["services"]
    web, worker = services["web"]["environment"], services["worker"]["environment"]
    changed = []
    for key in CARRIED:
        want = web.get(key)
        if want is not None and worker.get(key) != want:
            changed.append(f"{key} {worker.get(key)} -> {want}")
            worker[key] = want
    return changed


def main(argv: list[str]) -> int:
    """`set_platform_image.py sha256:<id> [OLD_HOST NEW_HOST OLD_DIR NEW_DIR]`:
    with the four, rename_mounts.py's change rides in the SAME `app.update`
    (2026-10-11, `drag/` → `shard/`): an update may start the app, and two
    of them would start it once with half the change."""
    if len(argv) not in (2, 6):
        print(f"usage: {argv[0]} sha256:<image id> [OLD_HOST NEW_HOST OLD_DIR NEW_DIR]",
              file=sys.stderr)
        return 2
    want = argv[1]
    renamed: list[str] = []
    carried: list[str] = []
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            before = apply(cfg, want)
            carried = carry(cfg)
            if len(argv) == 6:
                import importlib.util
                from pathlib import Path
                spec = importlib.util.spec_from_file_location(
                    "rename_mounts", Path(__file__).with_name("rename_mounts.py"))
                rm = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(rm)
                renamed = rm.apply(cfg, *argv[2:6])
        except ValueError as e:
            print(f"set_platform_image: {e}", file=sys.stderr)
            return 2
        for name in SERVICES:
            print(f"{name:7} {KEY} {before[name]} -> {want}")
        for line in carried:
            print(f"worker  {line}")
        for line in renamed:
            print(f"rename {line}")
        if all(v == want for v in before.values()) and not renamed and not carried:
            print("unchanged")
            return 0
        try:
            c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"set_platform_image: app.update failed ({type(e).__name__}); "
                  f"see /var/log/middlewared.log", file=sys.stderr)
            return 1
        after = c.call("app.config", APP)
        got = {name: ((after.get("services") or {}).get(name) or {}).get("environment", {}).get(KEY)
               for name in SERVICES}
        if any(v != want for v in got.values()):
            print(f"set_platform_image: NOT set ({got})", file=sys.stderr)
            return 1
        envs = {name: ((after.get("services") or {}).get(name) or {}).get("environment") or {}
                for name in SERVICES}
        missing = [k for k in CARRIED
                   if k in envs["web"] and envs["worker"].get(k) != envs["web"][k]]
        if missing:
            print(f"set_platform_image: the worker did not take {missing}", file=sys.stderr)
            return 1
        print("set")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
