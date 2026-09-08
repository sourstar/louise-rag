"""Bundle wire format: header schema, strict parsing, signed byte layout.

*** SUPERSEDED BY app/hold/bundle.py, WHICH IS docs/09-formats.md §9.7. ***

This module predates the specification and describes itself from
`architecture-v2.md` §5 — the engineering record, not the contract. It is
kept only until `hold vectors` replaces its callers (migration step 4);
nothing new should be built on it, and its bytes are NOT §9.7's:

  * the header is missing `snapshot_id`, `shard`, `encoder_image`,
    `encoder_weights` and a non-null `challenge`, and carries
    `backend_fingerprint`, which appears in no specification. §9.7's schema
    is closed in both directions, so each of those is an error either way.
  * `space_id`, `content_digest` and `producer_pubkey` are bare hex where
    §9.1/§9.2 fix prefixed textual forms, and `collection` carries the local
    NAME where §9.7 says the collection id.
  * `challenge` is forced to null in both directions, so this parser refuses
    every conformant bundle by construction.
  * `possession.json` maps uid to `raw_hash` — the ORIGIN bytes' hash, which
    §9.5 calls `origin_hash` — where §9.7 requires the record's
    `content_hash`. Both are 32 hex characters, so the two are
    indistinguishable on the wire and every cross-import refuses every
    record silently.
  * the parquet carries a third `seq` column; §9.7 has two columns and parses
    `seq` out of the chunk id.

The version does NOT move to 2: §9.7's table types `version` as the literal
1, so §9.7's shape IS version 1. There is no installed base to protect —
Phase 1's bundles are "built, never exercised" — so bundles this module
wrote are refused rather than migrated. (Operator decision, 2026-09-08.)

*** THE PRIMARY FILE FLAGGED FOR PERSONAL SECURITY REVIEW. ***
*** A subtle bug here is a security hole, not a crash. ***

A bundle is a directory (architecture-v2.md §5, plus one documented addition):

    bundle/
      header.json      metadata; NEVER contains text, titles or authors
      vectors.parquet  chunk_id, seq, vector[dim]   <- no text, ever
      possession.json  uid -> producer's raw_hash   <- addition, see below
      MANIFEST.sig     exactly 64 raw bytes, Ed25519

`possession.json` extends §5's three-file listing. It carries the producer's
per-document raw_hash so the importer can prove its local corpus is the SAME
REVISION the vectors were computed from — Gutenberg re-issues texts and PubMed
revises records, and without this, revision-A vectors attach silently to
revision-B text. It is a separate file rather than header fields so that it is
read only AFTER the signature verifies, keeping it out of the
pre-authentication parse surface; its digest is inside the signed payload, so
it cannot be swapped.

═══════════════════════════════════════════════════════════════════════════
RULE 1: THE WIRE BYTES ARE THE MESSAGE. NEVER RE-SERIALIZE TO VERIFY.
═══════════════════════════════════════════════════════════════════════════

`json.dumps(obj, sort_keys=True)` is a deterministic serializer for values you
construct. It is NOT a canonical form over arbitrary parsed input, and using it
as one is exploitable:

  * A lone surrogate (`"\\ud800"`) parses fine and then raises UnicodeEncodeError
    on re-encode with ensure_ascii=False — an unhandled crash reachable by any
    peer, whose message lands in db.log_event and documents.error.
  * `{"a":1,"a":2}` silently keeps the last value, so two DIFFERENT wire headers
    collapse to ONE canonical form — a signature-transplant primitive.
  * NFC vs NFD gives two byte encodings of the same visible string, both signable.
  * Non-string keys make sort_keys raise TypeError; `{1:"a","1":"b"}` emits a
    duplicate key.

So verification hashes `header.json`'s bytes EXACTLY AS RECEIVED. Parsing is for
semantics only and happens strictly AFTER the signature checks out.

═══════════════════════════════════════════════════════════════════════════
RULE 2: DOMAIN-SEPARATED, LENGTH-FRAMED SIGNED PAYLOAD.
═══════════════════════════════════════════════════════════════════════════

    msg = b"louise-rag/bundle-sig/v1\\x00"
        + u32be(len(header_bytes))     + header_bytes
        + u32be(32) + sha256(parquet_bytes)
        + u32be(32) + sha256(possession_bytes)

    bundle_id = sha256(msg).hex()

Length extension is a non-issue (Ed25519 is PureEdDSA, not Merkle-Damgard), and
this exact layout has no concatenation ambiguity. The framing is there so that
adding a fourth part later cannot silently change what a signature covers, and
the domain prefix is there because the SAME node key will also sign snapshot
manifests (§5 raw plane) and coordinator claims (§7) — nothing else binds a
signature to "bundle".

`digest_alg` and `sig_alg` appear in the header for future-proofing but are
compared against one pinned literal each and NEVER dispatched through a lookup
table. Reading an algorithm out of an attacker-supplied document and acting on
it is the JWT `alg` confusion bug. For the same reason MANIFEST.sig is exactly
64 raw bytes with no envelope and no algorithm field.
"""
import hashlib
import json
import struct
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np
from nacl import signing
from nacl.exceptions import BadSignatureError

from .keys import SIGNATURE_BYTES, verify_key_from_hex

FORMAT_VERSION = 1

_SIG_DOMAIN = b"louise-rag/bundle-sig/v1\x00"
_VECTORS_DOMAIN = b"louise-rag/vectors/v1\x00"

DIGEST_ALG = "sha256"
SIG_ALG = "ed25519"
DTYPES = {"f16": ">f2", "f32": ">f4"}

# Caps applied BEFORE any value is used to allocate or index. Python ints are
# arbitrary-precision, so `chunk_count: 10**30` parses happily and would
# overflow any i64 peer; `dim: 10**9` would try to allocate gigabytes.
MAX_HEADER_BYTES = 1 << 20          # 1 MiB
MAX_DIM = 8192
MAX_CHUNK_COUNT = 10 ** 8
MAX_UIDS = 10 ** 6
MAX_PART_BYTES = 1 << 33            # 8 GiB per file
MAX_STRING_LEN = 4096

# Every key that may appear in a v1 header. Unknown keys are REJECTED rather
# than ignored: if a future version adds `expires_at` or a revocation field, an
# attacker must not be able to strip it and have the old signature still verify.
_HEADER_KEYS = {
    "version", "space_id", "collection", "uids", "chunk_count", "dim", "dtype",
    "producer_pubkey", "backend_fingerprint", "content_digest",
    "parquet_bytes", "possession_bytes", "created_at",
    "digest_alg", "sig_alg", "challenge",
}


class BundleError(RuntimeError):
    """Base for every bundle failure. Subclasses RuntimeError so the worker's
    existing `except Exception` keeps working."""


class BundleFormatError(BundleError):
    """Malformed header, sidecar, or signature encoding."""


class BundleSignatureError(BundleError):
    """The signature does not verify against the producer key in the header."""


# ─────────────────────────── strict parsing ───────────────────────────

def _reject_number(value: str) -> Any:
    """json.loads hook for floats and for the non-standard NaN/Infinity/-Infinity
    constants. The header must contain no floats at all: float round-tripping is
    the classic canonicalization hazard, and NaN/Infinity are invalid RFC 8259
    that json.dumps will happily emit — which would split us from any Go or Rust
    verifier."""
    raise BundleFormatError(f"header contains a float or non-finite constant: {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise BundleFormatError("header contains a duplicate key")
    return dict(pairs)


def _check_text(value: Any, field: str) -> str:
    """Strings must be str, bounded, UTF-8 encodable, and NFC-normalized.

    ENCODABILITY IS CHECKED FIRST AND IS NOT REDUNDANT. `json.loads` accepts a
    lone surrogate escape ("\\ud800") and yields a Python str that
    unicodedata.is_normalized() reports as perfectly normalized — so NFC alone
    lets it straight through. That string then raises UnicodeEncodeError the
    moment anything tries to store or emit it: sqlite3 on the ledger insert,
    db.log_event's print to container stdout, or Jinja rendering it in the web
    UI. That is precisely the remote crash-and-log-injection this module exists
    to prevent, so it is rejected here, at the boundary.

    NFC matters for a different reason: 'café' has two valid UTF-8 encodings
    that render identically, so without it two different signed byte strings
    look like the same collection name to a human reading a ledger.
    """
    if not isinstance(value, str):
        raise BundleFormatError(f"header field {field!r} must be a string")
    if len(value) > MAX_STRING_LEN:
        raise BundleFormatError(f"header field {field!r} is too long")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BundleFormatError(
            f"header field {field!r} contains unpaired surrogates") from None
    if not unicodedata.is_normalized("NFC", value):
        raise BundleFormatError(f"header field {field!r} is not NFC-normalized")
    return value


def _check_int(value: Any, field: str, low: int, high: int) -> int:
    # bool is a subclass of int; `True` must not pass as 1 for a count.
    if not isinstance(value, int) or isinstance(value, bool):
        raise BundleFormatError(f"header field {field!r} must be an integer")
    if not low <= value <= high:
        raise BundleFormatError(f"header field {field!r} out of range: {value}")
    return value


def _check_hex(value: Any, field: str, nbytes: int) -> str:
    text = _check_text(value, field)
    if len(text) != nbytes * 2:
        raise BundleFormatError(f"header field {field!r} must be {nbytes * 2} hex characters")
    try:
        bytes.fromhex(text)
    except ValueError:
        raise BundleFormatError(f"header field {field!r} is not valid hex") from None
    return text.lower()


@dataclass(frozen=True)
class BundleHeader:
    """The parsed, validated header. Construct only via parse_header()."""
    version: int
    space_id: str
    collection: str
    uids: tuple[str, ...]
    chunk_count: int
    dim: int
    dtype: str
    producer_pubkey: str
    backend_fingerprint: str
    content_digest: str
    parquet_bytes: int
    possession_bytes: int
    created_at: int

    def to_wire(self) -> bytes:
        """Producer side only. ensure_ascii=True so the output is pure ASCII and
        no encoding step can fail; sort_keys + tight separators make repeated
        exports byte-identical. The verifier never calls this."""
        payload = {
            "version": self.version,
            "space_id": self.space_id,
            "collection": self.collection,
            "uids": list(self.uids),
            "chunk_count": self.chunk_count,
            "dim": self.dim,
            "dtype": self.dtype,
            "producer_pubkey": self.producer_pubkey,
            "backend_fingerprint": self.backend_fingerprint,
            "content_digest": self.content_digest,
            "parquet_bytes": self.parquet_bytes,
            "possession_bytes": self.possession_bytes,
            "created_at": self.created_at,
            "digest_alg": DIGEST_ALG,
            "sig_alg": SIG_ALG,
            "challenge": None,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True).encode("ascii")


def parse_header(raw: bytes) -> BundleHeader:
    """Parse and validate header bytes. Raises BundleFormatError on anything
    unexpected — never UnicodeEncodeError, TypeError or MemoryError.

    Call this only AFTER verify_signature() has accepted `raw`.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise BundleFormatError("header must be bytes")
    if len(raw) > MAX_HEADER_BYTES:
        raise BundleFormatError(f"header exceeds {MAX_HEADER_BYTES} bytes")
    try:
        obj = json.loads(raw.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_keys,
                         parse_float=_reject_number,
                         parse_constant=_reject_number)
    except BundleFormatError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BundleFormatError(f"header is not valid UTF-8 JSON: {type(e).__name__}") from None

    if not isinstance(obj, dict):
        raise BundleFormatError("header must be a JSON object")

    # Closed schema, both directions: no unknown keys, no missing keys.
    # `null` is not the same as absent, and absent is not the same as {} —
    # all three are different byte strings and only one is valid.
    unknown = set(obj) - _HEADER_KEYS
    if unknown:
        raise BundleFormatError(f"header has unknown key(s): {sorted(unknown)}")
    missing = _HEADER_KEYS - set(obj)
    if missing:
        raise BundleFormatError(f"header is missing key(s): {sorted(missing)}")

    version = _check_int(obj["version"], "version", 1, 1)

    # Pinned literals, never dispatched through a table.
    if obj["digest_alg"] != DIGEST_ALG:
        raise BundleFormatError(f"unsupported digest_alg (only {DIGEST_ALG!r} is accepted)")
    if obj["sig_alg"] != SIG_ALG:
        raise BundleFormatError(f"unsupported sig_alg (only {SIG_ALG!r} is accepted)")

    # The T2 challenge slot belongs to P1.2. Refusing a non-null value stops a
    # P1.2 bundle from being half-understood — and silently under-verified — by
    # a P1.1 verifier that cannot check challenge triples.
    if obj["challenge"] is not None:
        raise BundleFormatError(
            "header carries challenge data, which this version cannot verify "
            "(challenge triples land with the T2 verifier)")

    dtype = _check_text(obj["dtype"], "dtype")
    if dtype not in DTYPES:
        raise BundleFormatError(f"dtype must be one of {sorted(DTYPES)}")

    uids_raw = obj["uids"]
    if not isinstance(uids_raw, list):
        raise BundleFormatError("header field 'uids' must be a list")
    if len(uids_raw) > MAX_UIDS:
        raise BundleFormatError(f"header lists more than {MAX_UIDS} uids")
    uids = tuple(_check_text(u, "uids[]") for u in uids_raw)
    if len(set(uids)) != len(uids):
        raise BundleFormatError("header field 'uids' contains duplicates")
    if list(uids) != sorted(uids):
        raise BundleFormatError("header field 'uids' must be sorted")

    return BundleHeader(
        version=version,
        space_id=_check_hex(obj["space_id"], "space_id", 16),
        collection=_check_text(obj["collection"], "collection"),
        uids=uids,
        chunk_count=_check_int(obj["chunk_count"], "chunk_count", 0, MAX_CHUNK_COUNT),
        dim=_check_int(obj["dim"], "dim", 1, MAX_DIM),
        dtype=dtype,
        producer_pubkey=_check_hex(obj["producer_pubkey"], "producer_pubkey", 32),
        backend_fingerprint=_check_text(obj["backend_fingerprint"], "backend_fingerprint"),
        content_digest=_check_hex(obj["content_digest"], "content_digest", 32),
        parquet_bytes=_check_int(obj["parquet_bytes"], "parquet_bytes", 0, MAX_PART_BYTES),
        possession_bytes=_check_int(obj["possession_bytes"], "possession_bytes", 0, MAX_PART_BYTES),
        created_at=_check_int(obj["created_at"], "created_at", 0, 1 << 62),
    )


def parse_possession(raw: bytes) -> dict[str, str]:
    """uid -> raw_hash (blake2b digest_size=16, so 32 hex chars).

    Read ONLY after the signature verifies — its digest is inside the signed
    payload, so by this point the bytes are authenticated.
    """
    if len(raw) > MAX_PART_BYTES:
        raise BundleFormatError("possession sidecar is implausibly large")
    try:
        obj = json.loads(raw.decode("utf-8"),
                         object_pairs_hook=_reject_duplicate_keys,
                         parse_float=_reject_number,
                         parse_constant=_reject_number)
    except BundleFormatError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise BundleFormatError(
            f"possession sidecar is not valid UTF-8 JSON: {type(e).__name__}") from None
    if not isinstance(obj, dict):
        raise BundleFormatError("possession sidecar must be a JSON object")
    if len(obj) > MAX_UIDS:
        raise BundleFormatError(f"possession sidecar lists more than {MAX_UIDS} uids")
    return {_check_text(k, "possession key"): _check_hex(v, "possession value", 16)
            for k, v in obj.items()}


def dump_possession(mapping: dict[str, str]) -> bytes:
    """Producer side. Sorted and ASCII for byte-stable re-export."""
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


# ─────────────────────────── the signed payload ───────────────────────────

def _framed(part: bytes) -> bytes:
    return struct.pack(">I", len(part)) + part


def signed_payload(header_bytes: bytes, parquet_sha256: bytes,
                   possession_sha256: bytes) -> bytes:
    """The exact bytes covered by MANIFEST.sig. See RULE 2 in the module
    docstring. `header_bytes` MUST be the wire bytes, never a re-serialization."""
    if len(parquet_sha256) != 32 or len(possession_sha256) != 32:
        raise BundleFormatError("digests must be 32 bytes")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise BundleFormatError(f"header exceeds {MAX_HEADER_BYTES} bytes")
    return (_SIG_DOMAIN
            + _framed(header_bytes)
            + _framed(parquet_sha256)
            + _framed(possession_sha256))


def bundle_id_of(payload: bytes) -> str:
    """Derived from the signed payload, so it cannot disagree with what was
    signed. Not a header field — that would be circular."""
    return hashlib.sha256(payload).hexdigest()


def sign(payload: bytes, key: signing.SigningKey) -> bytes:
    """Returns exactly 64 raw signature bytes — no envelope, no alg field."""
    return key.sign(payload).signature


def verify_signature(payload: bytes, sig: bytes, producer_pubkey_hex: str) -> None:
    """Raises BundleSignatureError if the signature does not verify.

    NOTE FOR THE REVIEWER: this proves only that whoever holds the private key
    for `producer_pubkey_hex` signed exactly these bytes. The pubkey comes from
    the header the producer themselves signed, so this establishes INTEGRITY,
    not AUTHENTICITY. Deciding whether that key may write to the index is
    app/bundles/trust.py's job, and it is a separate gate.
    """
    if not isinstance(sig, (bytes, bytearray)) or len(sig) != SIGNATURE_BYTES:
        raise BundleFormatError(
            f"MANIFEST.sig must be exactly {SIGNATURE_BYTES} raw bytes")
    verify_key = verify_key_from_hex(producer_pubkey_hex)
    try:
        verify_key.verify(bytes(payload), bytes(sig))
    except BadSignatureError:
        raise BundleSignatureError("bundle signature does not verify") from None


# ─────────────────────────── content digest ───────────────────────────

def content_digest(rows: list[tuple[str, int, np.ndarray]], dim: int, dtype: str) -> str:
    """Container-independent digest over the logical vector content.

    Why this exists alongside sha256(parquet_bytes): the parquet digest proves
    "the file I received is the file that was signed", which is all transport
    integrity needs. But pyarrow's output is NOT byte-stable across versions
    (`created_by` embeds the Arrow version and has no Python knob; row-group,
    dictionary and statistics behavior have all moved historically). This digest
    is stable across writers and versions, so it is what makes the signature
    meaningful to a non-Python implementation and survivable across an upgrade.

    Two traps, both handled here:
      * ndarray.tobytes() is NATIVE-endian, so the dtype is pinned big-endian.
      * Rows are sorted on (uid utf-8 bytes, int seq) — a tuple sort, NOT a
        lexicographic sort of chunk_id, under which ':10:' precedes ':2:'.
    """
    if dtype not in DTYPES:
        raise BundleFormatError(f"dtype must be one of {sorted(DTYPES)}")
    wire_dtype = DTYPES[dtype]
    h = hashlib.sha256()
    h.update(_VECTORS_DOMAIN)
    h.update(struct.pack(">II", dim, len(rows)))
    for chunk_id, _seq, vector in rows:
        cid = chunk_id.encode("utf-8")
        h.update(struct.pack(">I", len(cid)))
        h.update(cid)
        vec = np.asarray(vector)
        if vec.shape != (dim,):
            raise BundleFormatError(
                f"vector for {chunk_id!r} has shape {vec.shape}, expected ({dim},)")
        h.update(vec.astype(wire_dtype, copy=False).tobytes())
    return h.hexdigest()


def sort_rows(rows: list[tuple[str, int, np.ndarray]]) -> list[tuple[str, int, np.ndarray]]:
    """Canonical row order: (uid utf-8 bytes, seq). Used by both the writer and
    the digest so they cannot drift apart."""
    def key(row):
        chunk_id, seq, _ = row
        uid = uid_of(chunk_id)
        return (uid.encode("utf-8"), int(seq))
    return sorted(rows, key=key)


def uid_of(chunk_id: str) -> str:
    """chunk_id is '{uid}:{seq}:{CHUNK_VERSION}' (app/chunking.py), and uid is
    itself '{source}:{native_id}' — so exactly two fields split off the right."""
    parts = chunk_id.rsplit(":", 2)
    if len(parts) != 3 or not parts[0]:
        raise BundleFormatError(f"malformed chunk id: {chunk_id!r}")
    return parts[0]


def seq_of(chunk_id: str) -> int:
    parts = chunk_id.rsplit(":", 2)
    if len(parts) != 3:
        raise BundleFormatError(f"malformed chunk id: {chunk_id!r}")
    try:
        return int(parts[1])
    except ValueError:
        raise BundleFormatError(f"malformed chunk id (seq): {chunk_id!r}") from None
