#!/usr/bin/env python3
"""deploy/16f/set_timezone.py [ZONE] — the node's timezone in the louise-rag
app's web and worker (default Europe/Rome). 07 §7.8's `[budget] schedule` is
read in local time (app/transfer/clock.py `wall_minutes`), and the containers
ran in UTC: "23:00-07:00 full" meant 01:00-09:00 on the node's clock, and an
hour more after each change of summer time (2026-10-19). Everything hashed
or signed is written in UTC whatever the zone; log lines change to local.

Run on the node with the app STOPPED, through TrueNAS's API client in this
process (flatten_mounts.py's reasons: the config carries the admin token).
Prints what it sets, nothing else. Idempotent.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flatten_mounts as fm  # noqa: E402

DEFAULT_ZONE = "Europe/Rome"


def main(argv: list[str]) -> int:
    zone = argv[1] if len(argv) > 1 else DEFAULT_ZONE
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", fm.APP)
        try:
            said = fm.apply(cfg, shared=(), env={"TZ": zone})
        except ValueError as e:
            print(f"set_timezone: {e}", file=sys.stderr)
            return 2
        for line in said:
            print(line)
        if not said:
            print("nothing to change")
            return 0
        try:
            c.call("app.update", fm.APP, {"custom_compose_config": cfg}, job=True)
        except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
            print(f"set_timezone: app.update failed ({type(e).__name__}); see /var/log/middlewared.log",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
