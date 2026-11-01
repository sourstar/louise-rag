"""This node's identity: the public half of its key, and where it is read from.

Two processes need the identity and only one needs the secret
(docs/notes/model-pool.md §13, "layer 2"; operator, 2026-09-29). The worker signs
— validations, a new collection's calibration member, bundles — and holds
the private key at NODE_KEY_PATH. The web process only has to RECOGNISE
what this node signed (its validations, its attestations, its
acknowledgements), so it is given the public half alone, at
NODE_PUBLIC_KEY_PATH, and never reads the seed: a web process that is
compromised cannot sign.

The public file is `ed25519:` and 64 lowercase hex digits (09 §9.2's
textual form), one line. It is not secret, but it is TRUSTED: whoever can
replace it makes this node recognise their key as its own, and so accept
the statements they plant. It is therefore kept where the private key is —
out of `state/` and `config/`, mounted read-only — and a file writable by
group or others is refused.

`own()` is THE resolution; nothing else decides which key is this node's.
"""
import os
import stat
from pathlib import Path

from . import config
from .hold import envelope as env

#: one line of 09 §9.2's textual form, and a newline
_MAX_BYTES = len(env.KEY_PREFIX) + 64 + 2


class IdentityError(RuntimeError):
    """The public key file is missing, malformed, loosely permissioned, or
    names another key than the private one. The message never carries file
    content beyond what `read_public` has already checked is a public key."""


def public_text(signing_key) -> str:
    return env.key_text(signing_key.verify_key)


def read_public(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise IdentityError(f"no public key at {path}")
    if os.name == "posix" and path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise IdentityError(
            f"public key at {path} is writable by group or others; whoever "
            f"can replace it decides which signatures this node calls its "
            f"own — `chmod 444` it")
    raw = path.read_bytes()[:_MAX_BYTES + 1]
    text = raw.decode("ascii", "replace").strip()
    if len(raw) > _MAX_BYTES or not text.startswith(env.KEY_PREFIX):
        raise IdentityError(f"public key at {path} is not `ed25519:` and 64 hex digits")
    try:
        env.key_bytes(text)
    except Exception:                                   # noqa: BLE001
        raise IdentityError(
            f"public key at {path} is not `ed25519:` and 64 hex digits") from None
    return text


def write_public(path: Path, signing_key) -> str:
    """Write the public half beside nothing secret. An existing file naming
    the same key is left as it is; one naming another key is refused — two
    identities on one node is a mistake to look at, not to overwrite."""
    path = Path(path)
    text = public_text(signing_key)
    if path.exists():
        if read_public(path) != text:
            raise IdentityError(
                f"{path} already names another key; remove it by hand if "
                f"it is stale")
        return text
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="ascii")
    try:
        os.chmod(tmp, 0o444)
    except (OSError, NotImplementedError):
        pass
    os.replace(tmp, path)
    return text


def own() -> str | None:
    """This node's public key, as an envelope's `key` spells it, or None on
    a node with neither file (07 §7.8: it cannot sign, and checks and
    searches all the same).

    - NODE_PUBLIC_KEY_PATH set and NODE_KEY_PATH not: the public file, and
      the seed is never read — the web process.
    - Otherwise the private key's public half; with the public file set as
      well, the two must agree or IdentityError — the worker.

    Raises what loading raises; callers that treat "no key" as None catch."""
    from .bundles import keys as node_keys
    pub = config.NODE_PUBLIC_KEY_PATH
    if pub is not None and config.NODE_KEY_PATH is None:
        return read_public(pub)
    text = public_text(node_keys.load_signing_key())
    if pub is not None and read_public(pub) != text:
        raise IdentityError(
            f"the public key at {pub} is not the public half of the key at "
            f"NODE_KEY_PATH: the web process would recognise another node's "
            f"signatures as this one's, and none of this one's")
    return text


def check_at_start() -> str | None:
    """The worker's start (docs/07 §7.8). A refusal is an IdentityError
    naming its reason; the node must not run with a web process that
    recognises another key, or none. A missing PRIVATE key is not refused
    here: that node cannot sign, as 07 §7.8 says of an unset NODE_KEY_PATH,
    and every signing path names the reason when it is reached."""
    from .bundles import keys as node_keys
    pub = config.NODE_PUBLIC_KEY_PATH
    if pub is not None:
        read_public(pub)
    if config.NODE_KEY_PATH is None:
        return read_public(pub) if pub is not None else None
    try:
        return own()
    except node_keys.NodeKeyError:
        return None
