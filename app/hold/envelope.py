"""Signed objects — the envelope, and the domains that keep signatures apart.
docs/09-formats.md §9.2.

Every signed object other than the bundle is signed over

    message = <domain> ‖ 0x00 ‖ SHA-256(canonical JSON of the body)

and travels as `{"body": {…}, "sig": "<128 hex>"}`.

WHY THE DOMAIN. One key signs snapshots, attestations, digests, events,
pointers and calibration statements. To Ed25519 a message is bytes, so
without a prefix a signature given over one kind of object could be
presented as a valid signature over another whose bytes happened to
coincide — or obtained deliberately by getting a peer to sign the one it
signs freely. 09 §9.2: "A signature made under one domain verifies under
no other, so a key that signs bundles, snapshots and digests can never be
made to sign one thing while meaning another."

That is also why this module exists rather than each site framing its own
message: a mistyped or omitted domain is silent — the signature verifies,
against the wrong domain, and nothing ever says so.
"""
from __future__ import annotations

import hashlib
from typing import Any

from nacl import signing
from nacl.exceptions import BadSignatureError

from .codex import CodexError, canonical_json

#: 09 §9.2's table, complete. `louise-rag/bundle-sig/v1` is NOT here: a
#: bundle is signed over its own framed payload (§9.7), not over an
#: envelope, and `app/bundles/` owns it.
SNAPSHOT = "louise/snapshot/v1"
ATTESTATION = "louise/attestation/v1"
DIGEST = "louise/digest/v1"
EVENT = "louise/event/v1"
POINTER = "louise/pointer/v1"
CALIBRATION = "louise/calibration/v1"
RAGE_SETTINGS = "louise/rage-settings/v1"

DOMAINS = (SNAPSHOT, ATTESTATION, DIGEST, EVENT, POINTER, CALIBRATION, RAGE_SETTINGS)

#: 09 §9.2: the textual form of a public key.
KEY_PREFIX = "ed25519:"


class SignatureError(CodexError):
    """A signed object is not what §9.2 fixes, or does not verify."""


def key_text(verify_key: signing.VerifyKey | bytes) -> str:
    """`ed25519:` and 64 lowercase hex (09 §9.2). Every signed body's `key`
    field uses this form, so a bare hex string is not a key."""
    raw = bytes(verify_key)
    if len(raw) != 32:
        raise SignatureError(f"an ed25519 public key is 32 bytes, got {len(raw)}")
    return KEY_PREFIX + raw.hex()


def key_bytes(text: str) -> bytes:
    """The inverse, refusing anything that is not the textual form. Input is
    attacker-controlled wherever a body arrives from a peer."""
    if not isinstance(text, str) or not text.startswith(KEY_PREFIX):
        raise SignatureError(f"a key is {KEY_PREFIX}… and 64 hex characters, "
                             f"got {text!r}")
    hexpart = text[len(KEY_PREFIX):]
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        raise SignatureError(f"a key is {KEY_PREFIX}… and 64 LOWERCASE hex "
                             f"characters, got {text!r}")
    return bytes.fromhex(hexpart)


def message(domain: str, body: dict[str, Any]) -> bytes:
    """09 §9.2: `<domain> ‖ 0x00 ‖ SHA-256(canonical JSON of the body)`.

    The 0x00 is a real separator byte here, and is not the concatenation of
    §9.1 — §9.2 writes this construction out explicitly and it is the only
    place a NUL appears in a signed message.
    """
    if domain not in DOMAINS:
        raise SignatureError(
            f"{domain!r} is not one of 09 §9.2's domains: "
            f"{', '.join(DOMAINS)}. A domain invented at the call site is a "
            f"signature nobody else can verify")
    digest_ = hashlib.sha256(canonical_json(body)).digest()
    return domain.encode("ascii") + b"\x00" + digest_


def body_id(body: dict[str, Any]) -> str:
    """`H(canonical JSON of body)` — never of the envelope, "so two peers
    that receive the same body compute the same id whatever byte string
    carried it, and an id is never a field of the body it names" (09 §9.2)."""
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


def sign(domain: str, body: dict[str, Any],
         key: signing.SigningKey) -> dict[str, Any]:
    """The envelope `{"body": …, "sig": "<128 hex>"}`.

    The body must already carry `kind`, `version`, `key` and `time`
    (09 §9.2), and its `key` must be this signer's — a body naming one key
    and signed by another verifies against neither.
    """
    for field in ("kind", "version", "key", "time"):
        if field not in body:
            raise SignatureError(
                f"a signed body always carries kind, version, key and time "
                f"(09 §9.2); {field!r} is missing")
    mine = key_text(key.verify_key)
    if body["key"] != mine:
        raise SignatureError(
            f"the body names {body['key']!r} and the signing key is {mine!r}; "
            f"a body naming one key and signed by another verifies against "
            f"neither")
    sig = key.sign(message(domain, body)).signature
    return {"body": body, "sig": sig.hex()}


def verify(domain: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """The body, if the envelope verifies under this domain and its own
    `key`. Raises otherwise; never returns an unverified body."""
    if set(envelope) != {"body", "sig"}:
        raise SignatureError("an envelope is exactly {body, sig} (09 §9.2)")
    body, sig = envelope["body"], envelope["sig"]
    if not isinstance(body, dict):
        raise SignatureError("an envelope's body is an object")
    if not isinstance(sig, str) or len(sig) != 128 or \
            any(c not in "0123456789abcdef" for c in sig):
        raise SignatureError("a signature is 128 lowercase hexadecimal digits")
    if "key" not in body:
        raise SignatureError("a signed body carries the signer's key (09 §9.2)")
    vk = signing.VerifyKey(key_bytes(body["key"]))
    try:
        vk.verify(message(domain, body), bytes.fromhex(sig))
    except BadSignatureError:
        raise SignatureError(
            f"the signature does not verify under {domain!r}. A signature "
            f"made under one domain verifies under no other (09 §9.2), so "
            f"this is either a forgery or an object of another kind") from None
    return body
