"""`hold key create` (07 §7.3): this peer's signing key, made once at
NODE_KEY_PATH — a path, never a value (invariant 8) — readable by its
owner alone, never overwritten; the public half written beside it when
NODE_PUBLIC_KEY_PATH names where (app/identity.py). `hold key show`
reads the public key and the trust list (app/hold/trust_file.py).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nacl import signing

from app.hold.codex import CodexError


class KeyError_(CodexError):
    exit_code = 2


def create(path: Path | None = None, *, public_path: Path | None = None) -> str:
    """The key written; its public text. Refuses a path that exists."""
    from app import config, identity
    from app.bundles import keys as node_keys
    p = path if path is not None else config.NODE_KEY_PATH
    if p is None:
        raise KeyError_("NODE_KEY_PATH is not set: a key is referenced by its path and this node names none "
                        "(07 §7.8; invariant 8)")
    p = Path(p)
    if p.exists():
        raise KeyError_(f"a key already exists at {p}; a key is never overwritten — move it by hand first")
    seed = node_keys.generate_seed()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, seed)
    finally:
        os.close(fd)
    key = signing.SigningKey(seed)
    text = identity.public_text(key)
    pub = public_path if public_path is not None else config.NODE_PUBLIC_KEY_PATH
    if pub is not None:
        identity.write_public(Path(pub), key)
    return text


def show(root: Path | None = None) -> dict[str, Any]:
    """This peer's public key (or why none) and the trust list with each
    key's state: allow · deny · burned."""
    from app import identity
    from app.hold import trust_file
    try:
        own = identity.own()
        why = None
    except Exception as e:   # noqa: BLE001 — no key is an answer, not an error
        own, why = None, f"{type(e).__name__}: {e}"
    doc = trust_file.read(root)
    keys: dict[str, dict[str, Any]] = {}
    for k in doc["allow"]:
        keys[str(k)] = {"state": "allow"}
    for k in doc["deny"]:
        keys[str(k)] = {"state": "deny"}
    for e in doc["quarantined"]:
        if isinstance(e, dict) and e.get("key"):
            keys[str(e["key"])] = {"state": "burned", "attestation": e.get("attestation"), "time": e.get("time")}
    return {"key": own, "why": why, "trust": keys, "path": str(trust_file.path(root))}


def trust(key: str, *, deny: bool = False, remove: bool = False, restore: bool = False,
          root: Path | None = None) -> dict[str, Any]:
    """`hold key trust <key> [--deny | --remove | --restore]`: into `allow`,
    into `deny`, out of both, or a burn lifted. The key's state after."""
    from app.hold import trust_file
    if not isinstance(key, str) or not key.startswith("ed25519:") or len(key) != 8 + 64:
        raise KeyError_(f"a key is ed25519: and 64 hex characters, got {key!r}")
    if sum((deny, remove, restore)) > 1:
        raise KeyError_("one of --deny, --remove, --restore")
    if restore:
        lifted = trust_file.restore(key, root=root)
        return {"key": key, "state": show(root)["trust"].get(key, {}).get("state", "none"),
                "restored": lifted}
    doc = trust_file.read(root)
    allow = [k for k in doc["allow"] if k != key]
    deny_list = [k for k in doc["deny"] if k != key]
    if deny:
        deny_list.append(key)
    elif not remove:
        allow.append(key)
    doc["allow"], doc["deny"] = sorted(allow), sorted(deny_list)
    trust_file.write(doc, root)
    return {"key": key, "state": show(root)["trust"].get(key, {}).get("state", "none")}
