"""tests/hold/test_envelope.py — a signature made under one domain verifies
under no other. docs/09-formats.md §9.2.
"""
import hashlib

import pytest
from nacl import signing

from app.hold import envelope as env
from app.hold.codex import canonical_json
from app.hold.envelope import SignatureError


def a_key(seed: bytes = b"\x01" * 32) -> signing.SigningKey:
    return signing.SigningKey(seed)


def a_body(key: signing.SigningKey, **over) -> dict:
    b = {"kind": "louise-calibration", "version": 1,
         "key": env.key_text(key.verify_key), "time": "2026-08-01T00:00:00Z",
         "set": "sha256:" + "6b" * 32}
    b.update(over)
    return b


# ---- the key's textual form ----------------------------------------------

def test_a_key_is_ed25519_and_64_lowercase_hex():
    k = a_key()
    t = env.key_text(k.verify_key)
    assert t.startswith("ed25519:") and len(t) == 8 + 64
    assert t == t.lower()
    assert env.key_bytes(t) == bytes(k.verify_key)


def test_a_bare_hex_string_is_not_a_key():
    """09 §9.2 fixes the textual form, and every signed body's `key` uses
    it. `app/bundles/keys.py::public_key_hex` returns the bare form, which
    is why this is asserted rather than assumed."""
    raw = bytes(a_key().verify_key).hex()
    with pytest.raises(SignatureError, match="ed25519"):
        env.key_bytes(raw)
    for bad in ("ed25519:" + "AB" * 32, "ed25519:tooshort", "", None):
        with pytest.raises(SignatureError):
            env.key_bytes(bad)


# ---- the message -----------------------------------------------------------

def test_the_message_is_the_documents_construction():
    """09 §9.2: `<domain> ‖ 0x00 ‖ SHA-256(canonical JSON of the body)`."""
    body = a_body(a_key())
    m = env.message(env.CALIBRATION, body)
    assert m == (b"louise/calibration/v1\x00"
                 + hashlib.sha256(canonical_json(body)).digest())
    assert len(m) == len("louise/calibration/v1") + 1 + 32


def test_a_domain_invented_at_the_call_site_is_refused():
    with pytest.raises(SignatureError, match="not one of 09 §9.2's domains"):
        env.message("louise/whatever/v1", a_body(a_key()))


def test_all_seven_domains_of_the_table_are_present():
    assert set(env.DOMAINS) == {
        "louise/snapshot/v1", "louise/attestation/v1", "louise/digest/v1",
        "louise/event/v1", "louise/pointer/v1", "louise/calibration/v1",
        "louise/rage-settings/v1"}


def test_the_bundle_domain_is_deliberately_absent():
    """A bundle is signed over its own framed payload (09 §9.7), not over an
    envelope; `app/bundles/` owns it."""
    assert "louise-rag/bundle-sig/v1" not in env.DOMAINS


# ---- the separation this exists for --------------------------------------

def test_a_signature_under_one_domain_verifies_under_no_other():
    """The whole point (09 §9.2): "a key that signs bundles, snapshots and
    digests can never be made to sign one thing while meaning another"."""
    k = a_key()
    signed = env.sign(env.CALIBRATION, a_body(k), k)
    assert env.verify(env.CALIBRATION, signed)["kind"] == "louise-calibration"
    for other in (env.SNAPSHOT, env.ATTESTATION, env.DIGEST, env.EVENT,
                  env.POINTER):
        with pytest.raises(SignatureError, match="does not verify"):
            env.verify(other, signed)


def test_two_domains_never_produce_the_same_message():
    body = a_body(a_key())
    seen = {env.message(d, body) for d in env.DOMAINS}
    assert len(seen) == len(env.DOMAINS)


# ---- the envelope ----------------------------------------------------------

def test_an_envelope_is_exactly_body_and_sig():
    k = a_key()
    e = env.sign(env.DIGEST, a_body(k, kind="louise-digest"), k)
    assert set(e) == {"body", "sig"}
    assert len(e["sig"]) == 128 and e["sig"] == e["sig"].lower()
    with pytest.raises(SignatureError, match="exactly"):
        env.verify(env.DIGEST, dict(e, extra=1))


def test_a_body_always_carries_kind_version_key_and_time():
    k = a_key()
    for missing in ("kind", "version", "key", "time"):
        body = a_body(k)
        del body[missing]
        with pytest.raises(SignatureError, match=missing):
            env.sign(env.CALIBRATION, body, k)


def test_a_body_naming_another_key_is_refused_at_signing():
    """It would verify against neither: the verifier reads the key from the
    body, and that key did not make this signature."""
    mine, theirs = a_key(b"\x01" * 32), a_key(b"\x02" * 32)
    body = a_body(mine)
    body["key"] = env.key_text(theirs.verify_key)
    with pytest.raises(SignatureError, match="verifies against\\s+neither"):
        env.sign(env.CALIBRATION, body, mine)


def test_a_tampered_body_does_not_verify():
    k = a_key()
    e = env.sign(env.CALIBRATION, a_body(k), k)
    e["body"]["set"] = "sha256:" + "ff" * 32
    with pytest.raises(SignatureError, match="does not verify"):
        env.verify(env.CALIBRATION, e)


def test_a_malformed_signature_is_refused_before_nacl_sees_it():
    k = a_key()
    e = env.sign(env.CALIBRATION, a_body(k), k)
    for bad in ("", "zz" * 64, "ab" * 63, ("AB" * 64), 12345):
        with pytest.raises(SignatureError, match="128 lowercase"):
            env.verify(env.CALIBRATION, dict(e, sig=bad))


def test_verify_never_returns_an_unverified_body():
    k, other = a_key(b"\x01" * 32), a_key(b"\x03" * 32)
    e = env.sign(env.CALIBRATION, a_body(k), k)
    forged = {"body": a_body(other), "sig": e["sig"]}
    with pytest.raises(SignatureError):
        env.verify(env.CALIBRATION, forged)


# ---- the id ----------------------------------------------------------------

def test_the_id_is_over_the_body_and_never_the_envelope():
    """09 §9.2: "so two peers that receive the same body compute the same id
    whatever byte string carried it, and an id is never a field of the body
    it names"."""
    k = a_key()
    body = a_body(k)
    e = env.sign(env.CALIBRATION, body, k)
    assert env.body_id(body) == "sha256:" + hashlib.sha256(
        canonical_json(body)).hexdigest()
    assert env.body_id(e["body"]) == env.body_id(body)
    assert "id" not in body


def test_the_id_ignores_how_the_body_was_carried():
    k = a_key()
    body = a_body(k)
    scrambled = dict(reversed(list(body.items())))
    assert env.body_id(scrambled) == env.body_id(body)
