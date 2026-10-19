#!/usr/bin/env python3
"""deploy/16f/add_bundles_mount.py — `/bundles` in the louise-rag app, one
mount for every collection's bundles (operator, 2026-10-19: bundles rest on
tank, not on the SSD, which keeps what queries read). Worker read-write, web
read-only, as /shards. Each collection's `state/bundles` becomes a symlink to
`/bundles/<name>` on the host, made by the runbook, so no stored path moves.

Run on the node with the app STOPPED, through TrueNAS's API client in this
process (flatten_mounts.py's reasons: the config carries the admin token, so
it never reaches a command line, a log or this output). Prints the mounts it
adds, nothing else. Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flatten_mounts as fm  # noqa: E402

BUNDLES = (("/mnt/tank/apps/louise-bundles", "/bundles", True),)


def main() -> int:
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", fm.APP)
        try:
            said = fm.apply(cfg, shared=BUNDLES, env={})
        except ValueError as e:
            print(f"add_bundles_mount: {e}", file=sys.stderr)
            return 2
        for line in said:
            print(line)
        if not said:
            print("nothing to change")
            return 0
        try:
            c.call("app.update", fm.APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"add_bundles_mount: app.update failed ({type(e).__name__}); see /var/log/middlewared.log",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
