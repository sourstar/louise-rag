#!/usr/bin/env python3
"""deploy/16f/move_secrets.py — the node's secrets by path (invariant 8;
deploy/truenas-app.yaml's NODE KEY section), in the louise-rag app.

Run on the node with the app STOPPED, through TrueNAS's API client in this
process (flatten_mounts.py's reasons: the config carries the admin token, so
it never reaches a command line, a log or this output). In this order:

1. secrets/{private 0700, public 0755, admin 0700} under the app's dataset;
2. the admin token: the env VALUE both services carry (they must agree) is
   written once to secrets/admin/admin-token (0600) — never printed;
3. the node key is MOVED (os.rename, one dataset: no copy is ever made) from
   config/node.key to secrets/private/node.key (0400); its sha256 before and
   after must agree (compared here, never printed);
4. app.config: ADMIN_TOKEN leaves both services, ADMIN_TOKEN_FILE and
   NODE_PUBLIC_KEY_PATH come in, NODE_KEY_PATH leaves the web and names the
   moved key in the worker; the web mounts secrets/public and secrets/admin
   read-only, the worker the whole of secrets/ read-only.

The public half (secrets/public/node.pub, 0444) is written by the deploy
script from the moved key in the new image (identity.write_public), after
this. Any failure before app.update succeeds puts the key back.
Idempotent: a second run finds the key moved and the config done.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

APP = "louise-rag"
ROOT = Path("/mnt/ssd4/apps/louise-rag")
S = ROOT / "secrets"
OLD_KEY = ROOT / "config" / "node.key"
NEW_KEY = S / "private" / "node.key"
TOKEN = S / "admin" / "admin-token"
IN = "/run/louise"
WEB_ENV = {"NODE_PUBLIC_KEY_PATH": f"{IN}/public/node.pub",
           "ADMIN_TOKEN_FILE": f"{IN}/admin/admin-token"}
WORKER_ENV = dict(WEB_ENV, NODE_KEY_PATH=f"{IN}/private/node.key")
WEB_VOLS = (f"{S}/public:{IN}/public:ro", f"{S}/admin:{IN}/admin:ro")
WORKER_VOLS = (f"{S}:{IN}:ro",)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def edit(cfg: dict) -> list[str]:
    """The config change alone (tested without a node)."""
    said: list[str] = []
    svcs = cfg["services"]
    for name, env_new, vols_new, drop in (("web", WEB_ENV, WEB_VOLS, ("ADMIN_TOKEN", "NODE_KEY_PATH")),
                                          ("worker", WORKER_ENV, WORKER_VOLS, ("ADMIN_TOKEN",))):
        svc = svcs[name]
        e = svc["environment"]
        for k in drop:
            if k in e:
                del e[k]
                said.append(f"{name}: {k} removed")
        for k, v in env_new.items():
            if e.get(k) != v:
                e[k] = v
                said.append(f"{name}: {k}={v}")
        vols = svc["volumes"]
        for v in vols_new:
            if v not in vols:
                vols.append(v)
                said.append(f"{name}: mount {v}")
    return said


def main() -> int:
    from truenas_api_client import Client
    with Client() as c:
        cfg = c.call("app.config", APP)
        web_e, wrk_e = cfg["services"]["web"]["environment"], cfg["services"]["worker"]["environment"]
        for d, mode in ((S, 0o700), (S / "private", 0o700), (S / "public", 0o755), (S / "admin", 0o700)):
            d.mkdir(exist_ok=True)
            os.chmod(d, mode)
        if not TOKEN.exists():
            tok = web_e.get("ADMIN_TOKEN") or ""
            if not tok or tok != (wrk_e.get("ADMIN_TOKEN") or ""):
                print("move_secrets: ADMIN_TOKEN missing or different between web and worker; nothing moved",
                      file=sys.stderr)
                return 2
            fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, (tok + "\n").encode())
            finally:
                os.close(fd)
            print(f"token: written to {TOKEN} (0600; equal in web and worker; never printed)")
        else:
            print(f"token: kept {TOKEN}")
        moved = False
        if OLD_KEY.exists():
            if NEW_KEY.exists():
                print(f"move_secrets: both {OLD_KEY} and {NEW_KEY} exist; look before anything moves",
                      file=sys.stderr)
                return 2
            before = _sha(OLD_KEY)
            os.rename(OLD_KEY, NEW_KEY)
            moved = True
            os.chmod(NEW_KEY, 0o400)
            if _sha(NEW_KEY) != before:
                os.rename(NEW_KEY, OLD_KEY)
                print("move_secrets: the key's sha256 changed in the move; put back", file=sys.stderr)
                return 2
            print(f"key: moved {OLD_KEY} -> {NEW_KEY} (0400), sha256 equal before and after")
        elif NEW_KEY.exists():
            print(f"key: already at {NEW_KEY}")
        else:
            print("move_secrets: no node key at either path", file=sys.stderr)
            return 2
        said = edit(cfg)
        for line in said:
            print(line)
        if said:
            try:
                c.call("app.update", APP, {"custom_compose_config": cfg}, job=True)
            except Exception as e:   # noqa: BLE001 — its message may quote the config: the type alone
                if moved:
                    os.rename(NEW_KEY, OLD_KEY)
                print(f"move_secrets: app.update failed ({type(e).__name__}); key put back; "
                      f"see /var/log/middlewared.log", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
