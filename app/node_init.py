"""A node's first boot on any Docker host — `python -m app.node_init`, the
one-shot `init` service of deploy/docker/compose.yaml (2026-10-07: Louise
runs outside TrueNAS too).

What a fresh node needs before its web and worker start, made once and
never overwritten — re-running it on a node that has everything does
nothing:

- the node key at NODE_KEY_PATH (`hold key create`, 07 §7.3), and its
  public half at NODE_PUBLIC_KEY_PATH; when the key exists and the half
  does not, the half is written from the key — never a new key (a new key
  is a new identity: peers stop recognising this node's signatures);
- the admin token at ADMIN_TOKEN_FILE: 32 random bytes as hex, readable by
  its owner alone. A path, never a value (invariant 8): it is not printed —
  read it from the file;
- `CONFIG_DIR/collections/` and `<LOUISE_ROOT>/collections/`;
- `CONFIG_DIR/platform.toml`, ONLY when absent: `[node] address` and
  `peer_port` from LOUISE_NODE_ADDRESS / LOUISE_PEER_PORT when given, else
  the block commented. This seeds the desired-state file once (07 §7.8);
  it is never rewritten from the environment afterwards — edit the file.

Every line printed names a path and what was done to it, never a secret.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Mapping


def _platform_text(address: str | None, peer_port: str | None) -> str:
    lines = [
        "# platform.toml — this node's desired state (07 §7.8). Written once by",
        "# app/node_init.py on first boot; edit it, it is never rewritten.",
        "",
        "[node]",
    ]
    if address:
        lines.append(f'address = "{address}"      # where THIS node answers pulls, as peers reach it')
    else:
        lines.append('# address = "http://<this host>:8742"   # where THIS node answers pulls, as peers reach it')
    if peer_port:
        lines.append(f"peer_port = {int(peer_port)}            # where this node's swarm listens, as peers reach it")
    lines += [
        "",
        "[peers]",
        "addresses = []                 # `louise share peers add <address> [--key]` writes here",
        "",
        "[dht]",
        "enabled = true",
        "",
    ]
    return "\n".join(lines)


def run(env: Mapping[str, str] | None = None, *, say=print) -> list[str]:
    """Make what is missing; return one line per thing made or kept."""
    env = os.environ if env is None else env
    done: list[str] = []

    def note(text: str) -> None:
        done.append(text)
        say(text)

    # FIRST, before anything of the app is imported: app/config.py reads
    # ADMIN_TOKEN_FILE at import and refuses a path that is set but missing,
    # which is exactly the state of a first boot (found on the first one,
    # 2026-10-07)
    token = Path(env["ADMIN_TOKEN_FILE"]) if env.get("ADMIN_TOKEN_FILE") else None
    if token is not None and not token.exists():
        token.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, (secrets.token_hex(32) + "\n").encode())
        finally:
            os.close(fd)
        note(f"token: made {token} — read it from the file; it is never printed")
    elif token is not None:
        note(f"token: kept {token}")

    from nacl import signing

    from app import identity
    from app.hold import node_key

    key_path = Path(env["NODE_KEY_PATH"]) if env.get("NODE_KEY_PATH") else None
    pub_path = Path(env["NODE_PUBLIC_KEY_PATH"]) if env.get("NODE_PUBLIC_KEY_PATH") else None
    if key_path is None:
        note("key: NODE_KEY_PATH unset — no key made (a node without one cannot sign)")
    elif not key_path.exists():
        text = node_key.create(key_path, public_path=pub_path)
        note(f"key: made {key_path} ({text})")
    else:
        # write_public leaves a half naming the same key as it is and
        # refuses one naming another — two identities on one node is a
        # mistake to look at, not to overwrite (the worker refuses it too)
        existed = pub_path is not None and pub_path.exists()
        text = (identity.write_public(pub_path, signing.SigningKey(key_path.read_bytes()))
                if pub_path is not None else None)
        if pub_path is not None and not existed:
            note(f"key: public half written to {pub_path} from the existing key ({text})")
        else:
            note(f"key: kept {key_path}")


    config_dir = Path(env.get("CONFIG_DIR") or ".")
    root = Path(env.get("LOUISE_ROOT") or env.get("DATA_DIR") or "/data")
    for d in (config_dir / "collections", root / "collections"):
        d.mkdir(parents=True, exist_ok=True)

    platform = config_dir / "platform.toml"
    if not platform.exists():
        tmp = platform.with_suffix(".toml.tmp")
        tmp.write_text(_platform_text(env.get("LOUISE_NODE_ADDRESS") or None,
                                      env.get("LOUISE_PEER_PORT") or None), encoding="utf-8")
        os.replace(tmp, platform)
        note(f"platform: made {platform}")
    else:
        note(f"platform: kept {platform}")
    return done


def main() -> int:
    try:
        run()
    except Exception as e:  # noqa: BLE001 — the message names a path, never a secret
        print(f"node_init: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
