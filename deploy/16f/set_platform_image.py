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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} sha256:<image id>", file=sys.stderr)
        return 2
    want = argv[1]
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        try:
            before = apply(cfg, want)
        except ValueError as e:
            print(f"set_platform_image: {e}", file=sys.stderr)
            return 2
        for name in SERVICES:
            print(f"{name:7} {KEY} {before[name]} -> {want}")
        if all(v == want for v in before.values()):
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
        print("set")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
