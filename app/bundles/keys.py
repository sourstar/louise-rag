"""Ed25519 node identity for signed vector bundles.

*** ONE OF THE TWO FILES FLAGGED FOR PERSONAL SECURITY REVIEW. ***

CLAUDE.md invariant 8: "Key material by filesystem path only; never in env
values, config values, logs, or error messages." architecture-v2.md §14 says
the same of Tier-1 config: the node key's *location*, never the key material.

Three consequences, all load-bearing:

1. THE SEED NEVER APPEARS IN AN EXCEPTION. Every failure here raises a fixed
   message and uses `raise ... from None`. The `from None` is not cosmetic:
   app/worker.py's outer handler logs `traceback.format_exc()[:1500]`, and
   db.log_event both prints to container stdout AND persists the text, so a
   chained traceback carrying file content would land in three places an
   operator later pastes into a bug report.

2. THE SIGNING KEY LOADS LAZILY, ON THE EXPORT PATH ONLY. Verification needs
   only public keys, so a consumer-role node never reads the seed at all.

3. THE PUBLIC KEY IS NOT A SECRET and is safe to log, store and display; it is
   the quarantine handle (architecture-v2.md §6) and so must be recorded.

The on-disk format is the raw 32-byte Ed25519 seed, or the same seed as 64 hex
characters (with optional trailing newline). Deliberately not PEM/OpenSSH: no
parser to get wrong, no algorithm field to confuse, no passphrase prompt to
hang a headless worker.
"""
import os
import stat
from pathlib import Path

from nacl import signing
from nacl.exceptions import CryptoError

from .. import config

SEED_BYTES = 32
PUBKEY_BYTES = 32
SIGNATURE_BYTES = 64


class NodeKeyError(RuntimeError):
    """The node key is missing, unreadable, malformed, or unsafely permissioned.

    The message NEVER includes file content — only the path, which is Tier-1
    configuration rather than key material.
    """


def _check_permissions(path: Path) -> None:
    """Refuse a group/other-readable key file, matching the posture every ssh
    client takes. POSIX only: Windows permissions do not map onto these bits,
    and os.stat there reports a synthetic mode, so the check would either
    always pass or always fail. Skipped rather than faked."""
    if os.name != "posix":
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise NodeKeyError(
            f"node key at {path} is group- or world-accessible; "
            f"tighten it with `chmod 600` before use")


def _parse_seed(raw: bytes) -> bytes:
    """Raw 32 bytes, or 64 hex characters. Never echoes the input."""
    if len(raw) == SEED_BYTES:
        return raw
    text = raw.strip()
    if len(text) == SEED_BYTES * 2:
        try:
            return bytes.fromhex(text.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            raise NodeKeyError(
                "node key is neither 32 raw bytes nor 64 hex characters") from None
    raise NodeKeyError("node key is neither 32 raw bytes nor 64 hex characters")


def load_signing_key(path: Path | None = None) -> signing.SigningKey:
    """Load the node's private signing key. EXPORT PATH ONLY.

    Call this as late as possible and never at import time — a consumer-role
    node should be able to run the whole worker without the seed ever entering
    the process.
    """
    key_path = path if path is not None else config.NODE_KEY_PATH
    if key_path is None:
        raise NodeKeyError(
            "NODE_KEY_PATH is not set — this node cannot export bundles "
            "(importing needs only public keys). Set it to a path and run "
            "`louise hold key create`")
    key_path = Path(key_path)
    if not key_path.is_file():
        raise NodeKeyError(f"no node key at {key_path}")
    _check_permissions(key_path)
    try:
        raw = key_path.read_bytes()
    except OSError:
        # Deliberately not `from e`: an OSError's str() can carry the path and,
        # for some errno paths, decoded content.
        raise NodeKeyError(f"node key at {key_path} could not be read") from None
    seed = _parse_seed(raw)
    try:
        return signing.SigningKey(seed)
    except (CryptoError, ValueError, TypeError):
        raise NodeKeyError(f"node key at {key_path} is not a valid Ed25519 seed") from None


def public_key_hex(key: signing.SigningKey) -> str:
    """The producer identity that goes in the header and the ledger. Public —
    safe to log, store and display."""
    return bytes(key.verify_key).hex()


def verify_key_from_hex(pubkey_hex: str) -> signing.VerifyKey:
    """Parse a producer pubkey from a bundle header or the trust store.

    Input is attacker-controlled on the import path, so it is length- and
    alphabet-checked before reaching nacl.
    """
    if not isinstance(pubkey_hex, str) or len(pubkey_hex) != PUBKEY_BYTES * 2:
        raise NodeKeyError("producer pubkey must be 64 hex characters")
    try:
        return signing.VerifyKey(bytes.fromhex(pubkey_hex))
    except (ValueError, CryptoError, TypeError):
        raise NodeKeyError("producer pubkey is not a valid Ed25519 public key") from None


def generate_seed() -> bytes:
    """A fresh 32-byte seed from the OS CSPRNG, for the keygen CLI verb.

    os.urandom, never random.* — and never derived from anything a peer can
    observe or influence.
    """
    return os.urandom(SEED_BYTES)
