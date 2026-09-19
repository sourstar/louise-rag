"""The bundle — its header, its digests, its signed payload and its id.
docs/09-formats.md §9.7.

A bundle is a DIRECTORY of four files (`header.json`, `vectors.parquet`,
`possession.json`, `MANIFEST.sig`), the unit in which embedding work
travels. This module owns the bytes: what the header may contain, how it
serialises, what `content_digest` covers, what the signature is over, and
how the id falls out. It does no I/O and knows nothing about Lance, parquet
or the manifest — those belong to `hold vectors`.

It stands BESIDE `app/bundles/format.py` rather than replacing it in place.
The old module writes and parses a different header: five of §9.7's fields
missing, one (`backend_fingerprint`) in no specification at all, three in
the wrong textual form, and `challenge` forced to null in both directions
where §9.7 makes it a required object. So no bundle it writes is
conformant, and no conformant bundle it parses.

WHY THE VERSION DOES NOT MOVE. §9.7's header table types `version` as the
literal `1`: §9.7's shape IS version 1, and a bump to 2 would contradict the
document being conformed to. There is no installed base to protect —
Phase 1's bundles are "built, never exercised" (docs/notes/STATUS.md), two
node integration tests and nothing published — so the old bundles are test
artefacts, they are refused, and NO compatibility path goes in this parser.
(Operator decision, 2026-09-08.)
"""
from __future__ import annotations

import hashlib
import json
import struct
import unicodedata
from typing import Any, Sequence

from .codex import CodexError
from .envelope import KEY_PREFIX, key_bytes

#: §9.7's domain. Not one of §9.2's six: a bundle is signed over its own
#: framed payload and never over an envelope, so `hold/envelope.py` does not
#: know this string and must not be given it.
DOMAIN = b"louise-rag/bundle-sig/v1"

#: The domain the row digest is framed under (§9.7).
VECTORS_DOMAIN = b"louise-rag/vectors/v1"

#: What §9.7 fixes today, and what is written for a collection whose Codex
#: names an engine (version 3). THE ENGINE AMENDMENT MOVED IT (0d85f7f):
#: "version 1 is this header with `encoder_image`, the engine's image digest
#: alone, in place of `encoder_engine`". Version 1 is still read and still
#: written for a version-2 collection, whose peers hold those bytes; the
#: note above is about the pre-§9.7 shape, which stays refused.
VERSION = 2
VERSIONS = (1, 2)

#: the key that names the producer's engine, by header version
ENGINE_KEY = {1: "encoder_image", 2: "encoder_engine"}

#: §9.7: "header at most 1 MiB, a string at most 4,096 characters, at most
#: 10**6 uids".
MAX_HEADER_BYTES = 1 << 20
MAX_STRING = 4096
MAX_UIDS = 10 ** 6

#: The four files, in the order §9.7 lists them.
HEADER = "header.json"
VECTORS = "vectors.parquet"
POSSESSION = "possession.json"
SIG = "MANIFEST.sig"
FILES = (HEADER, VECTORS, POSSESSION, SIG)

DTYPES = {"f16": ">f2", "f32": ">f4"}

#: §9.7's sixteen keys, in the document's order. The schema is closed in
#: BOTH directions — "Every key below is present and no other is" — so this
#: tuple is the whole of what a header may say.
KEYS = (
    "version", "collection", "space_id", "snapshot_id", "shard", "uids",
    "chunk_count", "dim", "dtype", "producer_pubkey", "encoder_engine",
    "encoder_weights", "content_digest", "parquet_bytes", "possession_bytes",
    "created_at", "digest_alg", "sig_alg", "challenge",
)


def keys_of(version: int) -> tuple[str, ...]:
    """The closed schema of a header at `version`: §9.7's, with the engine
    named as that version names it."""
    return tuple(ENGINE_KEY[version] if k == "encoder_engine" else k for k in KEYS)


_ID_KEYS = ("collection", "space_id", "content_digest", "encoder_weights")
_INT_KEYS = ("chunk_count", "dim", "parquet_bytes", "possession_bytes",
             "created_at")


class BundleError(CodexError):
    """A bundle is not what §9.7 fixes."""


# --------------------------------------------------------------------------
# serialisation
#
# NOTE this is NOT §9.1's canonical JSON, and the difference is deliberate.
# §9.1 fixes "non-ASCII characters unescaped" for every other Louise object;
# §9.7 fixes ASCII-only for the header, "every non-ASCII character escaped".
# Routeing a header through `codex.canonical_json` produces a file whose
# sha256 is inside a signature and whose bytes are wrong.

def serialise(header: dict[str, Any]) -> bytes:
    """`header.json`'s bytes: sorted keys, no whitespace, ASCII only.

    §9.7: "Written by the producer as JSON with sorted keys, no whitespace,
    ASCII only (every non-ASCII character escaped), so the file is
    byte-stable across re-exports; verified as the bytes received."
    """
    check(header)
    raw = json.dumps(header, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_HEADER_BYTES:
        raise BundleError(
            f"the header is {len(raw)} bytes and §9.7 caps it at "
            f"{MAX_HEADER_BYTES}; a bundle with a million uids is a bundle "
            f"nobody can cheaply refuse")
    return raw


def parse(raw: bytes) -> dict[str, Any]:
    """The header, from the bytes as received.

    "verified as the bytes received" — so this never re-serialises to
    compare. A header that parses to the same object through different bytes
    is a different file, and the signature is over the file.
    """
    if len(raw) > MAX_HEADER_BYTES:
        raise BundleError(f"the header is {len(raw)} bytes, over §9.7's cap")
    try:
        header = json.loads(raw)
    except ValueError as e:
        raise BundleError(f"the header is not JSON: {e}") from None
    if not isinstance(header, dict):
        raise BundleError("the header is a JSON object")
    check(header)
    return header


# --------------------------------------------------------------------------
# what a header may say

def check(header: dict[str, Any]) -> None:
    """Refuse anything §9.7 does not fix. Input is attacker-controlled.

    Every rule here is one a malformed bundle would otherwise pass on: a
    header is read before any tier runs, so this is the cheapest place a
    peer can say no, and everything it lets through costs a signature check
    at least.
    """
    # The version first: it decides which closed schema the rest is held to
    # (09 §9.18: a reader that meets a version it does not implement refuses
    # the object and says which it found; it never guesses).
    version = header.get("version")
    if isinstance(version, bool) or version not in VERSIONS:
        raise BundleError(
            f"version is {version!r}; §9.7 fixes {VERSION} and this build also "
            f"reads {', '.join(str(v) for v in VERSIONS if v != VERSION)}, so a "
            f"bundle of another version is not a bundle this document describes")
    got, want = set(header), set(keys_of(version))
    if got != want:
        missing, extra = sorted(want - got), sorted(got - want)
        raise BundleError(
            f"the header is a closed schema — §9.7: \"Every key below is "
            f"present and no other is\" (version {version})"
            + (f"; missing {', '.join(missing)}" if missing else "")
            + (f"; not in §9.7: {', '.join(extra)}" if extra else ""))
    for k in ("digest_alg", "sig_alg"):
        want_alg = {"digest_alg": "sha256", "sig_alg": "ed25519"}[k]
        if header[k] != want_alg:
            # §9.7: "compared against the literal; never dispatched" — the
            # field exists so a future document can change it, not so that a
            # peer can be talked into another algorithm.
            raise BundleError(
                f"{k} is {header[k]!r}, and §9.7 fixes {want_alg!r}. It is "
                f"compared against the literal and never dispatched on")

    for k in _INT_KEYS:
        v = header[k]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise BundleError(f"{k} is a non-negative integer, got {v!r}")
    if header["dim"] < 1:
        raise BundleError("dim is at least 1")
    if header["dtype"] not in DTYPES:
        raise BundleError(f"dtype is one of {', '.join(sorted(DTYPES))}, "
                          f"got {header['dtype']!r}")

    for k in (*_ID_KEYS, ENGINE_KEY[version]):
        _check_id(k, header[k])
    _check_key(header["producer_pubkey"])
    _check_snapshot_id(header["snapshot_id"])
    _check_shard(header["shard"])
    _check_uids(header["uids"])
    _check_challenge(header["challenge"])

    # §9.7: "no floats anywhere". A float in a signed object is a byte the
    # two ends may not agree on.
    _refuse_floats(header)


def _refuse_floats(obj: Any, path: str = "header") -> None:
    if isinstance(obj, float):
        raise BundleError(f"{path} is a float; §9.7: \"no floats anywhere\"")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _refuse_floats(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _refuse_floats(v, f"{path}[{i}]")


def _check_string(what: str, s: Any) -> str:
    if not isinstance(s, str):
        raise BundleError(f"{what} is a string, got {type(s).__name__}")
    if len(s) > MAX_STRING:
        raise BundleError(f"{what} is {len(s)} characters and §9.7 caps a "
                          f"string at {MAX_STRING}")
    if unicodedata.normalize("NFC", s) != s:
        raise BundleError(f"{what} is not NFC-normalised (§9.7)")
    return s


def _check_id(what: str, s: Any) -> str:
    """§9.1's textual form: `sha256:` and 64 lowercase hex."""
    _check_string(what, s)
    if not s.startswith("sha256:"):
        raise BundleError(
            f"{what} is {s!r}; §9.1 writes an id as `sha256:` and 64 "
            f"lowercase hex. A bare digest is a different textual form and "
            f"two peers would hash different bytes")
    h = s[len("sha256:"):]
    if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
        raise BundleError(f"{what} is {s!r}; 64 LOWERCASE hex after the prefix")
    return s


def _check_key(s: Any) -> str:
    _check_string("producer_pubkey", s)
    if not s.startswith(KEY_PREFIX):
        raise BundleError(
            f"producer_pubkey is {s!r}; §9.2 writes a key as "
            f"`{KEY_PREFIX}` and 64 lowercase hex")
    key_bytes(s)                        # the one implementation of the form
    return s


def _check_snapshot_id(s: Any) -> str:
    """`YYYY-MM` or `YYYY-Www` (§9.7, §9.4)."""
    _check_string("snapshot_id", s)
    ok = (len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:].isdigit()
          and 1 <= int(s[5:]) <= 12)
    if not ok:
        ok = (len(s) == 8 and s[4] == "-" and s[5] == "W" and s[:4].isdigit()
              and s[6:].isdigit() and 1 <= int(s[6:]) <= 53)
    if not ok:
        raise BundleError(
            f"snapshot_id is {s!r}; §9.7 fixes `YYYY-MM` or `YYYY-Www`")
    return s


def _check_shard(s: Any) -> str:
    """`"<slice>/<n>"` — one shard per bundle (§9.7)."""
    _check_string("shard", s)
    if s.count("/") != 1:
        raise BundleError(
            f"shard is {s!r}; §9.7 fixes `\"<slice>/<n>\"`, and one shard per "
            f"bundle is a hard constraint — a bundle spanning two shards has "
            f"no single entry in the coverage map (§9.10)")
    slice_, n = s.split("/")
    if not slice_ or not n.isdigit():
        raise BundleError(f"shard is {s!r}; the part after `/` is the shard "
                          f"number")
    return s


def _check_uids(uids: Any) -> list[str]:
    """Sorted by bytes, no duplicates (§9.7).

    Byte order, not any locale's: a uid that is a PREFIX of another precedes
    it. Python's `sorted()` on `str` is code-point order, which for UTF-8 is
    byte order — §9.7's own worked example had this backwards until the
    2026-09-08 amendment, so the check is here rather than assumed.
    """
    if not isinstance(uids, list):
        raise BundleError("uids is a list of strings")
    if len(uids) > MAX_UIDS:
        raise BundleError(f"uids has {len(uids)} entries and §9.7 caps it at "
                          f"{MAX_UIDS}")
    if not uids:
        raise BundleError("uids is not empty; a bundle covers a chunk range")
    for i, u in enumerate(uids):
        _check_string(f"uids[{i}]", u)
    if any(a >= b for a, b in zip(uids, uids[1:])):
        raise BundleError(
            "uids is sorted by bytes with no duplicates (§9.7). A uid that "
            "is a prefix of another precedes it")
    return uids


def _check_challenge(c: Any) -> dict[str, Any]:
    """`{set: id, n: 16}` — the T2 challenge (§9.7).

    Required and an object. The old parser refused any non-null `challenge`,
    which refused every conformant bundle by construction; this refuses the
    absence instead.
    """
    if not isinstance(c, dict):
        raise BundleError(
            "challenge is an object `{set, n}` (§9.7) — the first `n` rows of "
            "vectors.parquet are the calibration set's first `n` texts "
            "embedded by the producer. It is required: without it T2 cannot "
            "run at all")
    if set(c) != {"set", "n"}:
        raise BundleError(f"challenge is exactly {{set, n}}, got "
                          f"{{{', '.join(sorted(map(str, c)))}}}")
    _check_id("challenge.set", c["set"])
    if c["n"] != 16:
        raise BundleError(
            f"challenge.n is {c['n']!r}; §9.7 fixes 16, matching 03 §3.3's "
            f"quorum. A producer-chosen n would let a producer pick how hard "
            f"its own challenge is")
    return c


# --------------------------------------------------------------------------
# the row digest

def content_digest(dim: int, rows: Sequence[tuple[str, Sequence[float]]],
                   dtype: str) -> str:
    """§9.7's container-independent digest of the rows, in FILE ORDER.

        sha256( "louise-rag/vectors/v1" ‖ 0x00
                ‖ u32be(dim) ‖ u32be(rows)
                ‖ for each row in file order:
                      u32be(len(chunk_id)) ‖ chunk_id ‖ vector as big-endian )

    WHY IT EXISTS BESIDE the parquet's own sha256. Parquet bytes are not
    stable across writers — pyarrow embeds its version in `created_by` and
    its row-group, dictionary and statistics behaviour has moved across
    releases — so `sha256(parquet_bytes)` proves only "the file I received
    is the file that was signed" and dies on any re-encode or a non-Python
    writer. This digest is over the LOGICAL rows and survives both, which is
    what makes the signature mean something to a Rust or Go implementation.

    The challenge rows are inside it: it covers the file, and they are in
    the file, even though `chunk_count` counts the rows after them.

    Endianness is explicit. A native-order `tobytes()` would split
    little-endian from big-endian peers on a digest that is inside a
    signature — the kind of bug that only appears when the network is
    already large.
    """
    import numpy as np

    if dtype not in DTYPES:
        raise BundleError(f"dtype is one of {', '.join(sorted(DTYPES))}")
    be = DTYPES[dtype]

    # ONE sha256 over the whole concatenation — never a hash of per-part
    # hashes. The row count precedes the rows, so `rows` must be a Sequence
    # and not a generator: streaming it would mean hashing a digest of the
    # rows in their place, which is a stable, plausible-looking value that
    # no other implementation reproduces.
    h = hashlib.sha256()
    h.update(VECTORS_DOMAIN + b"\x00")
    h.update(struct.pack(">II", dim, len(rows)))
    for chunk_id, vector in rows:
        raw = chunk_id.encode("utf-8")
        v = np.asarray(vector, dtype=be)
        if v.shape != (dim,):
            raise BundleError(
                f"{chunk_id}: the row has {v.shape} elements and the header "
                f"says dim {dim}")
        h.update(struct.pack(">I", len(raw)) + raw + v.tobytes())
    return "sha256:" + h.hexdigest()


def row_order_key(chunk_id: str) -> tuple[bytes, int]:
    """§9.7: chunk rows are "sorted by (uid UTF-8 bytes, `seq` as an
    integer) — a tuple sort, under which `:10:` follows `:2:`".

    A chunk id is `<uid>:<seq>:<chunker version>` (§9.5), so `seq` is parsed
    out of the id and never carried as a third column. Sorting the ids as
    strings instead would put `:10:` before `:2:` and produce a file whose
    row order no other peer reproduces.
    """
    parts = chunk_id.rsplit(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        raise BundleError(
            f"{chunk_id!r} is not `<uid>:<seq>:<chunker version>` (§9.5)")
    return parts[0].encode("utf-8"), int(parts[1])


def challenge_id(i: int) -> str:
    """`"calibration:<i>"`, `i` from 0 (§9.7). The challenge rows come
    first, in index order, before any chunk row."""
    return f"calibration:{i}"


# --------------------------------------------------------------------------
# the signed payload and the id

def payload(header_bytes: bytes, parquet_sha256: bytes,
            possession_sha256: bytes) -> bytes:
    """§9.7's framed payload.

        "louise-rag/bundle-sig/v1" ‖ 0x00
        ‖ u32be(len(header_bytes)) ‖ header_bytes
        ‖ u32be(32) ‖ sha256(vectors.parquet bytes)
        ‖ u32be(32) ‖ sha256(possession.json bytes)

    Each part is length-prefixed, so no two different (header, parquet,
    possession) triples frame to the same bytes — without the lengths a byte
    moved from the end of one part to the start of the next would sign the
    same message.

    `header_bytes` is the file AS RECEIVED, never a re-serialisation: the
    signature is over the bytes, and a header that round-trips to the same
    object through different bytes is a different file.
    """
    for what, d in (("vectors.parquet", parquet_sha256),
                    ("possession.json", possession_sha256)):
        if len(d) != 32:
            raise BundleError(f"the {what} digest is 32 raw bytes, got "
                              f"{len(d)}")
    return (DOMAIN + b"\x00"
            + struct.pack(">I", len(header_bytes)) + header_bytes
            + struct.pack(">I", 32) + parquet_sha256
            + struct.pack(">I", 32) + possession_sha256)


def bundle_id(payload_bytes: bytes) -> str:
    """`"sha256:" ‖ hex(sha256(payload))` (§9.7).

    Derived from the signed bytes, "so it cannot disagree with what was
    signed and is not a header field; a producer that re-emits the same
    bundle produces the same id".
    """
    return "sha256:" + hashlib.sha256(payload_bytes).hexdigest()


def sign(payload_bytes: bytes, key) -> bytes:
    """`MANIFEST.sig` — exactly 64 raw bytes, Ed25519 (§9.7)."""
    return key.sign(payload_bytes).signature


def verify(payload_bytes: bytes, sig: bytes, producer_pubkey: str) -> None:
    """Raise unless `producer_pubkey` signed exactly these bytes.

    §9.7: "`verify` proves that whoever holds `producer_pubkey` signed
    exactly these bytes; whether the vectors are any good is the ladder's
    question (03 §3.5), and whether the key may be counted is the trust
    file's (§9.2)." So there is NO trust decision here and none above it in
    the import path: no producer key need be known, trusted or seen before
    (03 §3.5, 08 §8.1). A first-seen key is an ordinary key.
    """
    from nacl import signing
    from nacl.exceptions import BadSignatureError

    if len(sig) != 64:
        raise BundleError(
            f"MANIFEST.sig is exactly 64 raw bytes (§9.7), got {len(sig)}")
    try:
        signing.VerifyKey(key_bytes(producer_pubkey)).verify(payload_bytes, sig)
    except BadSignatureError:
        raise BundleError(
            f"the signature does not verify under {producer_pubkey}. Either "
            f"the bundle was altered in transit or it was signed by another "
            f"key") from None


# --------------------------------------------------------------------------
# possession.json

def serialise_possession(mapping: dict[str, str]) -> bytes:
    """§9.7: "A JSON object, sorted keys, ASCII, mapping each uid in `uids`
    to the `content_hash` of the record whose text the producer embedded."

    `content_hash`, NOT `origin_hash`. §9.5 makes `content_hash` the
    BLAKE2b-128 of the canonical JSON of `[title, sections]` — the cleaned
    text as the chunker sees it — while `origin_hash` is over the origin
    bytes. Both are 32 hex characters, so the two are indistinguishable on
    the wire and a peer sending one where the other is meant refuses every
    record silently. They are not even equivalent in meaning: two origin
    files that clean to identical text share a `content_hash` and differ in
    `origin_hash`, and a red-class pointer record has no origin bytes on the
    importing peer at all.
    """
    for uid, h in mapping.items():
        _check_string(f"possession[{uid}]", uid)
        if len(h) != 32 or any(c not in "0123456789abcdef" for c in h):
            raise BundleError(
                f"possession[{uid}] is {h!r}; a content_hash is BLAKE2b-128 "
                f"as 32 lowercase hex (§9.5)")
    return json.dumps(mapping, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def refused_uids(possession: dict[str, str],
                 mine: dict[str, str]) -> list[str]:
    """The uids whose rows this peer refuses, checking a bundle's possession
    map against its own records.

    §9.7 as amended 2026-09-08: a uid whose `content_hash` differs has THAT
    RECORD'S ROWS refused and the refusal recorded; the rest of the bundle
    is imported and the import exits 0 (03 §3.5, 07 §7.3). A mismatch is the
    ordinary case of a record re-issued since the producer embedded it, not
    a bad bundle.

    A uid in `uids` with NO entry in the map is a different thing — the
    bundle is malformed — and `check_possession_covers` refuses it whole.
    A uid this peer does not hold is NEITHER: it is simply not ours to
    check, and it is not refused.
    """
    return sorted(u for u, h in possession.items()
                  if u in mine and mine[u] != h)


def check_possession_covers(uids: Sequence[str],
                            possession: dict[str, str]) -> None:
    """Every uid in `uids` has an entry. A hole is a malformed bundle and
    refuses it whole at T0 — unlike a mismatch, which costs one record."""
    holes = [u for u in uids if u not in possession]
    if holes:
        raise BundleError(
            f"possession.json has no entry for {len(holes)} of the "
            f"{len(uids)} uids the header declares (first: {holes[0]!r}). "
            f"That is a malformed bundle, not a re-issued record: a uid with "
            f"no entry is refused whole at T0 (§9.7)")
    extra = [u for u in possession if u not in set(uids)]
    if extra:
        raise BundleError(
            f"possession.json carries {len(extra)} uid(s) the header does not "
            f"declare (first: {extra[0]!r}); the two must name the same set")
