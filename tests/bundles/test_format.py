"""Bundle wire format — the adversarial suite for the human-reviewed file.

Every hostile input must raise BundleFormatError (or BundleSignatureError).
It must NEVER raise UnicodeEncodeError, TypeError, MemoryError or anything
else uncaught: app/worker.py logs exception text to stdout, to the events
table, and (for other stages) into documents.error which renders in the web
UI, so an unhandled crash here is a remote log-injection and DoS surface.
"""
import hashlib
import json
import struct

import numpy as np
import pytest
from nacl import signing

from app.bundles import format as fmt
from app.bundles.keys import NodeKeyError

SEED = bytes(range(32))
KEY = signing.SigningKey(SEED)
PUBKEY = bytes(KEY.verify_key).hex()

PARQUET_SHA = hashlib.sha256(b"fake parquet").digest()
POSSESSION_SHA = hashlib.sha256(b"fake possession").digest()


def _header(**overrides) -> fmt.BundleHeader:
    base = dict(
        version=1,
        space_id="a" * 32,
        collection="gutenberg",
        uids=("gutenberg:1342",),
        chunk_count=3,
        dim=4,
        dtype="f32",
        producer_pubkey=PUBKEY,
        backend_fingerprint="cpu/llama.cpp/b1234",
        content_digest="b" * 64,
        parquet_bytes=1234,
        possession_bytes=56,
        created_at=1_700_000_000,
    )
    base.update(overrides)
    return fmt.BundleHeader(**base)


def _raw(**overrides) -> bytes:
    """Build header wire bytes, optionally overriding fields at the JSON level
    so we can inject values the dataclass would never allow."""
    obj = json.loads(_header().to_wire())
    for key, value in overrides.items():
        if value is _DELETE:
            obj.pop(key, None)
        else:
            obj[key] = value
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


_DELETE = object()


# ─────────────────────────── happy path ───────────────────────────

def test_round_trip_parses_back_to_an_equal_header():
    original = _header()
    parsed = fmt.parse_header(original.to_wire())
    assert parsed == original


def test_wire_encoding_is_byte_stable():
    assert _header().to_wire() == _header().to_wire()


def test_wire_encoding_is_pure_ascii():
    """ensure_ascii=True means no encoding step can ever fail, even for a
    collection name with astral-plane characters."""
    raw = _header(collection="bibliothèque \U0001F4DA").to_wire()
    raw.decode("ascii")   # must not raise


# ─────────────────────────── canonicalization attacks ───────────────────────────

def test_lone_surrogate_is_rejected_not_crashed():
    """Regression test for a real gap found during implementation.

    A lone surrogate escape is VALID JSON syntax, and the str it produces is
    reported as perfectly NFC-normalized — so an NFC check alone lets it
    through. It then raises UnicodeEncodeError at the first attempt to store or
    emit it (sqlite3 on the ledger insert, db.log_event's print to stdout,
    Jinja in the web UI), which is exactly the remote crash this module exists
    to prevent.

    Built by patching the parsed object so the surrogate is the ONLY thing
    wrong — an earlier version of this test did byte surgery that produced
    malformed JSON, and passed for the wrong reason.
    """
    obj = json.loads(_header().to_wire())
    obj["collection"] = json.loads(r'"\ud800"')
    assert len(obj["collection"]) == 1                      # genuinely a lone surrogate
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", "surrogatepass")
    with pytest.raises(fmt.BundleFormatError, match="surrogate"):
        fmt.parse_header(raw)


def test_surrogate_anywhere_in_the_header_is_rejected():
    for field in ("collection", "backend_fingerprint"):
        obj = json.loads(_header().to_wire())
        obj[field] = "ok" + json.loads(r'"\udfff"')
        raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode(
            "utf-8", "surrogatepass")
        with pytest.raises(fmt.BundleFormatError, match="surrogate"):
            fmt.parse_header(raw)


def test_surrogate_in_a_uid_is_rejected():
    obj = json.loads(_header().to_wire())
    obj["uids"] = ["gutenberg:" + json.loads(r'"\ud800"')]
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode(
        "utf-8", "surrogatepass")
    with pytest.raises(fmt.BundleFormatError, match="surrogate"):
        fmt.parse_header(raw)


def test_surrogate_in_the_possession_sidecar_is_rejected():
    raw = json.dumps({"gutenberg:" + json.loads(r'"\ud800"'): "a" * 32},
                      sort_keys=True, separators=(",", ":")).encode(
        "utf-8", "surrogatepass")
    with pytest.raises(fmt.BundleFormatError, match="surrogate"):
        fmt.parse_possession(raw)


def test_duplicate_keys_are_rejected():
    """Silently keeping the last value would let two DIFFERENT wire headers
    collapse to one parsed form — a signature-transplant primitive."""
    raw = _raw()
    injected = raw[:1] + b'"dim":9999,' + raw[1:]
    with pytest.raises(fmt.BundleFormatError, match="duplicate key"):
        fmt.parse_header(injected)


@pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_constants_are_rejected(literal):
    """json.dumps emits these happily; they are invalid RFC 8259 and would
    split us from any non-Python verifier."""
    raw = _raw().replace(b'"dim":4', b'"dim":' + literal)
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_header(raw)


def test_float_where_an_int_is_expected_is_rejected():
    raw = _raw().replace(b'"dim":4', b'"dim":4.0')
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_header(raw)


def test_non_nfc_string_is_rejected():
    """'cafe\\u0301' and 'caf\\u00e9' render identically but are different
    signable byte strings."""
    with pytest.raises(fmt.BundleFormatError, match="NFC"):
        fmt.parse_header(_raw(collection="café"))


def test_bool_does_not_pass_as_an_integer():
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_header(_raw(chunk_count=True))


# ─────────────────────────── closed schema ───────────────────────────

def test_unknown_key_is_rejected():
    """If a later version adds an expiry or revocation field, an attacker must
    not be able to strip it and have the old signature still verify."""
    with pytest.raises(fmt.BundleFormatError, match="unknown key"):
        fmt.parse_header(_raw(expires_at=0))


@pytest.mark.parametrize("field", ["version", "space_id", "dim", "challenge", "sig_alg"])
def test_missing_key_is_rejected(field):
    """null is not absent, and absent is not {} — three different byte strings."""
    with pytest.raises(fmt.BundleFormatError, match="missing key"):
        fmt.parse_header(_raw(**{field: _DELETE}))


def test_challenge_must_be_null_in_v1():
    """A P1.1 verifier cannot check challenge triples, so it must refuse a
    bundle carrying them rather than silently under-verify it."""
    with pytest.raises(fmt.BundleFormatError, match="challenge"):
        fmt.parse_header(_raw(challenge={"probe_set_id": "x"}))


@pytest.mark.parametrize("field,value", [("digest_alg", "sha1"), ("sig_alg", "rsa")])
def test_algorithm_fields_are_pinned_never_dispatched(field, value):
    """Reading an algorithm from an attacker-supplied document and acting on it
    is the JWT `alg` confusion bug."""
    with pytest.raises(fmt.BundleFormatError, match="unsupported"):
        fmt.parse_header(_raw(**{field: value}))


# ─────────────────────────── range and size caps ───────────────────────────

@pytest.mark.parametrize("field,value", [
    ("dim", 0), ("dim", -1), ("dim", 10 ** 9), ("dim", 10 ** 30),
    ("chunk_count", -1), ("chunk_count", 10 ** 30),
    ("parquet_bytes", -1), ("created_at", -1),
])
def test_out_of_range_integers_are_rejected(field, value):
    """Python ints are arbitrary-precision: 10**30 parses fine and would
    overflow any i64 peer, and a huge dim would try to allocate gigabytes."""
    with pytest.raises(fmt.BundleFormatError, match="out of range|must be"):
        fmt.parse_header(_raw(**{field: value}))


def test_oversized_header_is_rejected_before_parsing():
    with pytest.raises(fmt.BundleFormatError, match="exceeds"):
        fmt.parse_header(b"{" + b" " * (fmt.MAX_HEADER_BYTES + 1))


def test_garbage_is_rejected_cleanly():
    for raw in (b"", b"not json", b"[]", b"null", b"\xff\xfe", b'{"a":'):
        with pytest.raises(fmt.BundleFormatError):
            fmt.parse_header(raw)


@pytest.mark.parametrize("field", ["space_id", "producer_pubkey", "content_digest"])
def test_malformed_hex_fields_are_rejected(field):
    for bad in ["", "zz", "a" * 63, "g" * 64]:
        with pytest.raises(fmt.BundleFormatError):
            fmt.parse_header(_raw(**{field: bad}))


def test_uids_must_be_sorted_unique_strings():
    with pytest.raises(fmt.BundleFormatError, match="sorted"):
        fmt.parse_header(_raw(uids=["b:2", "a:1"]))
    with pytest.raises(fmt.BundleFormatError, match="duplicates"):
        fmt.parse_header(_raw(uids=["a:1", "a:1"]))
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_header(_raw(uids=[123]))
    with pytest.raises(fmt.BundleFormatError, match="must be a list"):
        fmt.parse_header(_raw(uids={"a": 1}))


def test_dtype_is_constrained():
    with pytest.raises(fmt.BundleFormatError, match="dtype"):
        fmt.parse_header(_raw(dtype="f64"))


# ─────────────────────────── signature ───────────────────────────

def _payload(header_bytes=None):
    return fmt.signed_payload(header_bytes or _header().to_wire(),
                              PARQUET_SHA, POSSESSION_SHA)


def test_signature_round_trip():
    payload = _payload()
    fmt.verify_signature(payload, fmt.sign(payload, KEY), PUBKEY)


def test_signature_is_exactly_64_bytes():
    assert len(fmt.sign(_payload(), KEY)) == fmt.SIGNATURE_BYTES


@pytest.mark.parametrize("length", [0, 63, 65, 128])
def test_wrong_length_signature_is_rejected(length):
    """No envelope, no alg field — anything but 64 raw bytes is malformed."""
    with pytest.raises(fmt.BundleFormatError, match="64 raw bytes"):
        fmt.verify_signature(_payload(), b"\x00" * length, PUBKEY)


def test_one_bit_flipped_in_the_header_breaks_the_signature():
    header_bytes = _header().to_wire()
    payload = _payload(header_bytes)
    sig = fmt.sign(payload, KEY)
    tampered = bytearray(header_bytes)
    tampered[10] ^= 0x01
    with pytest.raises(fmt.BundleSignatureError):
        fmt.verify_signature(_payload(bytes(tampered)), sig, PUBKEY)


def test_signature_does_not_transfer_to_a_different_parquet():
    payload = fmt.signed_payload(_header().to_wire(), PARQUET_SHA, POSSESSION_SHA)
    sig = fmt.sign(payload, KEY)
    other = fmt.signed_payload(_header().to_wire(),
                                hashlib.sha256(b"different").digest(), POSSESSION_SHA)
    with pytest.raises(fmt.BundleSignatureError):
        fmt.verify_signature(other, sig, PUBKEY)


def test_signature_does_not_transfer_to_a_different_possession_sidecar():
    payload = fmt.signed_payload(_header().to_wire(), PARQUET_SHA, POSSESSION_SHA)
    sig = fmt.sign(payload, KEY)
    other = fmt.signed_payload(_header().to_wire(), PARQUET_SHA,
                                hashlib.sha256(b"swapped").digest())
    with pytest.raises(fmt.BundleSignatureError):
        fmt.verify_signature(other, sig, PUBKEY)


def test_a_different_key_does_not_verify():
    payload = _payload()
    sig = fmt.sign(payload, signing.SigningKey(bytes(range(32, 64))))
    with pytest.raises(fmt.BundleSignatureError):
        fmt.verify_signature(payload, sig, PUBKEY)


def test_malformed_pubkey_is_rejected_before_reaching_nacl():
    with pytest.raises(NodeKeyError):
        fmt.verify_signature(_payload(), b"\x00" * 64, "not-a-key")


def test_signed_payload_is_domain_separated():
    """The same node key will also sign snapshot manifests and coordinator
    claims; a signature must not be replayable across protocols."""
    assert _payload().startswith(b"louise-rag/bundle-sig/v1\x00")


def test_signed_payload_is_length_framed():
    header_bytes = _header().to_wire()
    payload = _payload(header_bytes)
    offset = len(b"louise-rag/bundle-sig/v1\x00")
    assert struct.unpack(">I", payload[offset:offset + 4])[0] == len(header_bytes)


def test_framing_prevents_boundary_ambiguity():
    """Without length prefixes, moving bytes between adjacent variable-length
    parts could produce the same signed payload."""
    a = fmt.signed_payload(b'{"x":1}', PARQUET_SHA, POSSESSION_SHA)
    b = fmt.signed_payload(b'{"x":1} ', PARQUET_SHA, POSSESSION_SHA)
    assert a != b


def test_bundle_id_is_derived_from_the_signed_payload():
    payload = _payload()
    assert fmt.bundle_id_of(payload) == hashlib.sha256(payload).hexdigest()
    assert fmt.bundle_id_of(payload) != fmt.bundle_id_of(payload + b"x")


# ─────────────────────────── content digest ───────────────────────────

def _rows(n=3, dim=4, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        v = rng.normal(size=dim).astype(np.float32)
        out.append((f"gutenberg:1342:{i}:1", i, v / np.linalg.norm(v)))
    return out


def test_content_digest_is_stable_and_order_independent():
    rows = _rows()
    first = fmt.content_digest(fmt.sort_rows(rows), dim=4, dtype="f32")
    shuffled = [rows[2], rows[0], rows[1]]
    second = fmt.content_digest(fmt.sort_rows(shuffled), dim=4, dtype="f32")
    assert first == second


def test_content_digest_changes_when_a_vector_changes():
    rows = _rows()
    base = fmt.content_digest(fmt.sort_rows(rows), dim=4, dtype="f32")
    rows[1][2][0] += np.float32(0.5)
    assert fmt.content_digest(fmt.sort_rows(rows), dim=4, dtype="f32") != base


def test_content_digest_is_endian_pinned():
    """ndarray.tobytes() is native-endian; the digest must not depend on the
    machine that computed it."""
    rows = _rows()
    native = [(cid, seq, v.astype("<f4")) for cid, seq, v in rows]
    swapped = [(cid, seq, v.astype(">f4")) for cid, seq, v in rows]
    assert (fmt.content_digest(fmt.sort_rows(native), 4, "f32")
            == fmt.content_digest(fmt.sort_rows(swapped), 4, "f32"))


def test_content_digest_rejects_a_wrong_shaped_vector():
    rows = [("gutenberg:1:0:1", 0, np.zeros(3, dtype=np.float32))]
    with pytest.raises(fmt.BundleFormatError, match="shape"):
        fmt.content_digest(rows, dim=4, dtype="f32")


def test_sort_is_numeric_on_seq_not_lexicographic():
    """A lexicographic sort of chunk_id puts ':10:' before ':2:'."""
    rows = [(f"gutenberg:1342:{i}:1", i, np.zeros(4, dtype=np.float32))
            for i in (2, 10, 1)]
    ordered = [seq for _, seq, _ in fmt.sort_rows(rows)]
    assert ordered == [1, 2, 10]


def test_uid_and_seq_extraction():
    assert fmt.uid_of("gutenberg:1342:7:1") == "gutenberg:1342"
    assert fmt.seq_of("gutenberg:1342:7:1") == 7
    assert fmt.uid_of("pubmed:38112345:0:2") == "pubmed:38112345"
    for bad in ("nocolons", "only:two", ":1:1"):
        with pytest.raises(fmt.BundleFormatError):
            fmt.uid_of(bad)


# ─────────────────────────── possession sidecar ───────────────────────────

def test_possession_round_trip():
    mapping = {"gutenberg:1342": "a" * 32, "gutenberg:11": "b" * 32}
    assert fmt.parse_possession(fmt.dump_possession(mapping)) == mapping


def test_possession_rejects_malformed_hashes_and_duplicates():
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_possession(b'{"gutenberg:1":"tooshort"}')
    with pytest.raises(fmt.BundleFormatError, match="duplicate key"):
        fmt.parse_possession(b'{"a":"' + b"a" * 32 + b'","a":"' + b"b" * 32 + b'"}')
    with pytest.raises(fmt.BundleFormatError):
        fmt.parse_possession(b"[]")
