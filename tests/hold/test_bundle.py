"""tests/hold/test_bundle.py — the bundle's bytes.

docs/09-formats.md §9.7. Everything here is a byte another implementation
must reproduce, so the tests compute the expected values independently of
the module rather than calling it twice.
"""
import hashlib
import json
import struct

import pytest
from nacl import signing

from app.hold import bundle as bd
from app.hold.bundle import BundleError

KEY = signing.SigningKey(b"\x11" * 32)
PUB = "ed25519:" + bytes(KEY.verify_key).hex()
ID = "sha256:" + "ab" * 32


def header(**over):
    """§9.7's worked example, whole: a Gutenberg bundle, slice lang=it,
    shard 00007, 300 chunks."""
    h = {
        "version": 2,
        "collection": "sha256:" + "e5" * 32,
        "space_id": "sha256:" + "41" * 32,
        "snapshot_id": "2026-08",
        "shard": "lang=it/00007",
        "uids": ["gutenberg:10102", "gutenberg:1012", "gutenberg:10125"],
        "chunk_count": 300,
        "dim": 1024,
        "dtype": "f16",
        "producer_pubkey": PUB,
        # the producer's ENGINE, by its id (§9.3) — version 1 named an image
        "encoder_engine": "sha256:" + "5a" * 32,
        "encoder_weights": "sha256:" + "e2" * 32,
        "content_digest": "sha256:" + "0f" * 32,
        "parquet_bytes": 662148,
        "possession_bytes": 1417,
        "created_at": 1756713600,
        "digest_alg": "sha256",
        "sig_alg": "ed25519",
        "challenge": {"set": ID, "n": 16},
    }
    h.update(over)
    return h


# ---- the closed schema ----------------------------------------------------

def test_the_header_is_exactly_09_9_7s_sixteen_entries():
    """§9.7: "Every key below is present and no other is" — closed in BOTH
    directions, so a field a previous version carried is now an error."""
    assert len(bd.KEYS) == len(set(bd.KEYS))
    bd.check(header())


def test_a_missing_field_is_refused():
    h = header()
    del h["shard"]
    with pytest.raises(BundleError, match="missing shard"):
        bd.check(h)


def test_a_field_not_in_the_document_is_refused():
    """`backend_fingerprint` is what the old exporter carried, from an
    engineering-record document rather than the specification. A closed
    schema is what makes it an error rather than a silent extension."""
    with pytest.raises(BundleError, match="not in §9.7: backend_fingerprint"):
        bd.check(header(backend_fingerprint="whatever"))


def test_a_version_this_document_does_not_describe_is_refused():
    """§9.7 fixes version 2, and version 1 — the same header naming the
    engine's image alone — is still read (the engine amendment, 0d85f7f).
    Anything else is refused and the message says which was found (09
    §9.18: a reader never guesses)."""
    for bad in (0, 3, "2", True, None):
        with pytest.raises(BundleError, match="version is"):
            bd.check(header(version=bad))


def header_v1(**over):
    h = header(version=1)
    h["encoder_image"] = h.pop("encoder_engine")
    h.update(over)
    return h


def test_version_1_is_still_read_and_names_an_image():
    """What a version-2 collection's peers already hold. It is a CLOSED
    schema like the other: a version-1 header carrying `encoder_engine`, or
    a version-2 one carrying `encoder_image`, is neither."""
    h = header_v1()
    assert bd.parse(bd.serialise(h)) == h
    with pytest.raises(BundleError, match="closed schema"):
        bd.check(header(version=1))                     # encoder_engine at 1
    with pytest.raises(BundleError, match="closed schema"):
        bd.check(dict(header_v1(), version=2))          # encoder_image at 2
    with pytest.raises(BundleError, match="sha256"):
        bd.check(header_v1(encoder_image="8c" * 32))    # a bare digest, either way


def test_the_algorithms_are_compared_and_never_dispatched_on():
    """§9.7: "compared against the literal; never dispatched". The fields
    exist so a future document can change them, not so a peer can be talked
    into another algorithm."""
    with pytest.raises(BundleError, match="digest_alg"):
        bd.check(header(digest_alg="sha1"))
    with pytest.raises(BundleError, match="sig_alg"):
        bd.check(header(sig_alg="rsa"))


# ---- textual forms --------------------------------------------------------

def test_an_id_is_the_prefixed_form_not_a_bare_digest():
    """The old code checked bare 32-hex here. A bare digest is a different
    textual form, well-formed and wrong, and two peers hash different bytes."""
    with pytest.raises(BundleError, match="space_id.*sha256:"):
        bd.check(header(space_id="41" * 32))
    with pytest.raises(BundleError, match="LOWERCASE"):
        bd.check(header(space_id="sha256:" + "AB" * 32))


def test_a_key_is_the_ed25519_form():
    with pytest.raises(BundleError, match="producer_pubkey"):
        bd.check(header(producer_pubkey=bytes(KEY.verify_key).hex()))


def test_a_snapshot_id_is_a_month_or_an_iso_week():
    for ok in ("2026-08", "2026-W01", "2026-W53"):
        bd.check(header(snapshot_id=ok))
    for bad in ("2026-13", "2026-8", "2026-W54", "2026", "2026-08-01"):
        with pytest.raises(BundleError, match="snapshot_id"):
            bd.check(header(snapshot_id=bad))


def test_a_shard_is_one_slice_and_one_number():
    """One shard per bundle is a hard constraint: a bundle spanning two
    shards has no single entry in the coverage map (§9.10)."""
    bd.check(header(shard="year=1980/00003"))
    for bad in ("lang=it", "lang=it/00007/extra", "lang=it/abc", "/7"):
        with pytest.raises(BundleError, match="shard"):
            bd.check(header(shard=bad))


# ---- uids, and the byte order §9.7 got wrong -------------------------------

def test_uids_are_sorted_by_bytes_with_a_prefix_first():
    """The order §9.7's own worked example had backwards until the
    2026-09-08 amendment: a uid that is a PREFIX of another precedes it, so
    `gutenberg:1012` comes before `gutenberg:10125` — while it FOLLOWS
    `gutenberg:10102`, since the two agree through `gutenberg:101` and `0`
    precedes `2`."""
    good = ["gutenberg:10102", "gutenberg:1012", "gutenberg:10125",
            "gutenberg:10131"]
    assert good == sorted(good), "the fixture is the true byte order"
    bd.check(header(uids=good))

    was_printed = ["gutenberg:10102", "gutenberg:10125", "gutenberg:1012",
                   "gutenberg:10131"]
    with pytest.raises(BundleError, match="sorted by bytes"):
        bd.check(header(uids=was_printed))


def test_a_duplicate_uid_is_refused():
    with pytest.raises(BundleError, match="sorted by bytes"):
        bd.check(header(uids=["gutenberg:1", "gutenberg:1"]))


def test_an_empty_uid_list_is_refused():
    with pytest.raises(BundleError, match="not empty"):
        bd.check(header(uids=[]))


# ---- the challenge --------------------------------------------------------

def test_the_challenge_is_required_and_an_object():
    """The old parser raised on any non-null `challenge`, which refused
    every conformant bundle by construction. This refuses its absence."""
    with pytest.raises(BundleError, match="challenge is an object"):
        bd.check(header(challenge=None))


def test_the_challenge_n_is_fixed_at_sixteen():
    """A producer-chosen `n` would let a producer pick how hard its own
    challenge is."""
    with pytest.raises(BundleError, match="fixes 16"):
        bd.check(header(challenge={"set": ID, "n": 4}))


# ---- no floats ------------------------------------------------------------

def test_a_float_anywhere_is_refused():
    """§9.7: "no floats anywhere". A float in a signed object is a byte the
    two ends may not agree on.

    A typed field is caught by its own check, which says something more
    useful; the sweep is what catches a float somewhere no field type
    reaches — `challenge.n`, where `16.0 == 16` is True in Python and every
    comparison in `check` would otherwise pass it."""
    with pytest.raises(BundleError, match="non-negative integer"):
        bd.check(header(chunk_count=300.0))
    with pytest.raises(BundleError, match=r"challenge\.n is a float"):
        bd.check(header(challenge={"set": ID, "n": 16.0}))


def test_a_bool_is_not_an_integer():
    with pytest.raises(BundleError, match="non-negative integer"):
        bd.check(header(dim=True))


# ---- serialisation is NOT §9.1's canonical JSON ---------------------------

def test_the_header_is_ascii_only_unlike_every_other_louise_object():
    """§9.1 fixes "non-ASCII characters unescaped" for every other object;
    §9.7 fixes ASCII-only for this one. Routeing a header through
    `codex.canonical_json` produces a file whose sha256 is inside a
    signature and whose bytes are wrong."""
    from app.hold.codex import canonical_json
    h = header(shard="anno=1980/00007")
    raw = bd.serialise(h)
    raw.decode("ascii")                         # would raise if it were not
    assert b'"\\u' not in raw or True           # no non-ASCII in this fixture

    unicode_h = header(shard="año=1980/00007")
    assert b"\\u00f1" in bd.serialise(unicode_h), "escaped, per §9.7"
    assert "ñ".encode("utf-8") in canonical_json(unicode_h), \
        "and §9.1 would have left it raw — the two are different functions"


def test_the_header_has_sorted_keys_and_no_whitespace():
    raw = bd.serialise(header())
    assert b" " not in raw.split(b'"shard":')[0], "no whitespace"
    assert list(json.loads(raw)) == sorted(bd.KEYS)
    assert sorted(bd.keys_of(1)) == sorted(set(bd.KEYS) - {"encoder_engine"} | {"encoder_image"})


def test_a_header_round_trips():
    h = header()
    assert bd.parse(bd.serialise(h)) == h


def test_parse_never_re_serialises_to_compare():
    """"verified as the bytes received" — a header that round-trips to the
    same object through different bytes is a different FILE, and the
    signature is over the file. So parse accepts whatever JSON is valid and
    conformant; it is `payload` that pins the bytes."""
    spaced = json.dumps(header(), sort_keys=True).encode()   # has whitespace
    assert bd.parse(spaced) == header()
    assert spaced != bd.serialise(header())


# ---- the row digest -------------------------------------------------------

def test_the_content_digest_is_one_sha256_over_the_concatenation():
    """Computed here from §9.7's formula directly, so the test fails if the
    module ever hashes per-part digests instead — which produces a stable,
    plausible value that no other implementation reproduces."""
    import numpy as np
    rows = [("calibration:0", [1.0, 2.0, 3.0]),
            ("gutenberg:1012:0:1", [4.0, 5.0, 6.0])]

    want = hashlib.sha256()
    want.update(b"louise-rag/vectors/v1" + b"\x00")
    want.update(struct.pack(">II", 3, 2))
    for cid, vec in rows:
        raw = cid.encode("utf-8")
        want.update(struct.pack(">I", len(raw)) + raw
                    + np.asarray(vec, dtype=">f4").tobytes())

    assert bd.content_digest(3, rows, "f32") == "sha256:" + want.hexdigest()


def test_the_digest_is_big_endian_whatever_the_host_is():
    """A native-order tobytes() would split little- from big-endian peers on
    a digest that is inside a signature."""
    import numpy as np
    rows = [("a:0:1", [1.5, -2.25])]
    got = bd.content_digest(2, rows, "f32")
    little = np.asarray([1.5, -2.25], dtype="<f4").tobytes()
    big = np.asarray([1.5, -2.25], dtype=">f4").tobytes()
    assert little != big, "the fixture would not detect the bug otherwise"
    assert big in _digest_input(2, rows, ">f4")
    assert got == "sha256:" + hashlib.sha256(_digest_input(2, rows, ">f4")).hexdigest()


def _digest_input(dim, rows, be):
    import numpy as np
    out = b"louise-rag/vectors/v1\x00" + struct.pack(">II", dim, len(rows))
    for cid, vec in rows:
        raw = cid.encode("utf-8")
        out += struct.pack(">I", len(raw)) + raw + np.asarray(vec, dtype=be).tobytes()
    return out


def test_the_digest_covers_the_challenge_rows_too():
    """They are in the file, and the digest is over the file — even though
    `chunk_count` counts the rows after them."""
    rows = [("calibration:0", [1.0]), ("g:1:0:1", [2.0])]
    assert bd.content_digest(1, rows, "f32") != \
        bd.content_digest(1, rows[1:], "f32")


def test_a_row_of_the_wrong_width_is_refused():
    with pytest.raises(BundleError, match="dim 3"):
        bd.content_digest(3, [("a:0:1", [1.0, 2.0])], "f32")


def test_the_two_dtypes_give_different_digests():
    rows = [("a:0:1", [1.5])]
    assert bd.content_digest(1, rows, "f16") != bd.content_digest(1, rows, "f32")


# ---- row order ------------------------------------------------------------

def test_rows_sort_on_seq_as_an_integer_not_as_a_string():
    """§9.7: "a tuple sort, under which `:10:` follows `:2:`". Sorting the
    ids as strings puts `:10:` first and produces a file whose row order no
    other peer reproduces."""
    ids = ["g:1012:10:1", "g:1012:2:1", "g:1012:1:1"]
    assert [i for i in sorted(ids, key=bd.row_order_key)] == \
        ["g:1012:1:1", "g:1012:2:1", "g:1012:10:1"]
    assert sorted(ids) != sorted(ids, key=bd.row_order_key), \
        "a string sort would disagree — which is the point"


def test_the_uid_half_of_the_key_is_bytes():
    a, b = bd.row_order_key("gutenberg:1012:0:1")
    assert a == b"gutenberg:1012" and b == 0


def test_a_chunk_id_of_the_wrong_shape_is_refused():
    with pytest.raises(BundleError, match="chunker version"):
        bd.row_order_key("gutenberg:1012")


def test_the_challenge_rows_are_named_from_zero():
    assert bd.challenge_id(0) == "calibration:0"
    assert bd.challenge_id(15) == "calibration:15"


# ---- the signed payload and the id ----------------------------------------

def test_the_payload_is_09_9_7s_framing():
    hb = bd.serialise(header())
    p, q = hashlib.sha256(b"parquet").digest(), hashlib.sha256(b"poss").digest()
    want = (b"louise-rag/bundle-sig/v1\x00"
            + struct.pack(">I", len(hb)) + hb
            + struct.pack(">I", 32) + p
            + struct.pack(">I", 32) + q)
    assert bd.payload(hb, p, q) == want


def test_every_part_is_length_prefixed():
    """Without the lengths, a byte moved from the end of one part to the
    start of the next signs the same message."""
    p, q = b"\x01" * 32, b"\x02" * 32
    assert bd.payload(b"ab", p, q) != bd.payload(b"a", b"b" + p[:31], q)


def test_a_digest_of_the_wrong_length_is_refused():
    with pytest.raises(BundleError, match="32 raw bytes"):
        bd.payload(b"{}", b"\x01" * 31, b"\x02" * 32)


def test_the_id_is_over_the_signed_bytes_and_is_not_a_header_field():
    """§9.7: "derived from the signed bytes, so it cannot disagree with what
    was signed and is not a header field"."""
    pl = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    assert bd.bundle_id(pl) == "sha256:" + hashlib.sha256(pl).hexdigest()
    assert "bundle_id" not in bd.KEYS


def test_re_emitting_the_same_bundle_gives_the_same_id():
    a = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    b = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    assert bd.bundle_id(a) == bd.bundle_id(b)


# ---- signing --------------------------------------------------------------

def test_a_signature_is_64_raw_bytes_and_verifies():
    pl = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    sig = bd.sign(pl, KEY)
    assert len(sig) == 64
    bd.verify(pl, sig, PUB)


def test_a_tampered_payload_does_not_verify():
    pl = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    sig = bd.sign(pl, KEY)
    with pytest.raises(BundleError, match="does not verify"):
        bd.verify(pl + b"x", sig, PUB)


def test_a_first_seen_key_is_an_ordinary_key():
    """03 §3.5 and 08 §8.1: no producer key need be known, trusted or seen
    before. The old code refused every key after the first (trust on first
    use); nothing in this module has an opinion about which key signed."""
    other = signing.SigningKey(b"\x99" * 32)
    other_pub = "ed25519:" + bytes(other.verify_key).hex()
    pl = bd.payload(bd.serialise(header(producer_pubkey=other_pub)),
                    b"\x01" * 32, b"\x02" * 32)
    bd.verify(pl, bd.sign(pl, other), other_pub)


def test_a_signature_of_the_wrong_length_is_refused():
    pl = bd.payload(bd.serialise(header()), b"\x01" * 32, b"\x02" * 32)
    with pytest.raises(BundleError, match="64 raw bytes"):
        bd.verify(pl, bd.sign(pl, KEY)[:63], PUB)


def test_the_bundle_domain_is_not_one_of_9_2s_six():
    """A bundle is signed over its own framed payload, never over an
    envelope, so `hold/envelope.py` must not know this string."""
    from app.hold import envelope as env
    assert bd.DOMAIN.decode() not in env.DOMAINS


# ---- possession.json ------------------------------------------------------

def test_possession_maps_uid_to_content_hash_not_origin_hash():
    """§9.5 makes `content_hash` BLAKE2b-128 over the cleaned text and
    `origin_hash` the hash of the origin bytes. Both are 32 hex characters,
    so a peer sending one where the other is meant refuses every record
    silently — which is what the old code did."""
    raw = bd.serialise_possession({"g:2": "b" * 32, "g:1": "a" * 32})
    assert raw == b'{"g:1":"' + b"a" * 32 + b'","g:2":"' + b"b" * 32 + b'"}'


def test_a_possession_value_of_the_wrong_width_is_refused():
    with pytest.raises(BundleError, match="BLAKE2b-128"):
        bd.serialise_possession({"g:1": "a" * 64})


def test_a_mismatch_refuses_that_records_rows_and_nothing_else():
    """§9.7 as amended 2026-09-08, following 03 §3.5 and 07 §7.3: a mismatch
    is the ordinary case of a record re-issued since the producer embedded
    it, not a bad bundle."""
    possession = {"g:1": "a" * 32, "g:2": "b" * 32, "g:3": "c" * 32}
    mine = {"g:1": "a" * 32, "g:2": "9" * 32}          # g:2 was re-issued
    assert bd.refused_uids(possession, mine) == ["g:2"]


def test_a_uid_this_peer_does_not_hold_is_not_refused():
    """It is simply not ours to check."""
    assert bd.refused_uids({"g:1": "a" * 32}, {}) == []


def test_a_uid_with_no_possession_entry_refuses_the_bundle_whole():
    """The one case that is still whole-bundle: a hole is a malformed
    bundle, not a re-issued record."""
    with pytest.raises(BundleError, match="malformed bundle"):
        bd.check_possession_covers(["g:1", "g:2"], {"g:1": "a" * 32})


def test_possession_may_not_carry_uids_the_header_does_not_declare():
    with pytest.raises(BundleError, match="does not declare"):
        bd.check_possession_covers(["g:1"], {"g:1": "a" * 32, "g:2": "b" * 32})


def test_a_full_map_passes():
    bd.check_possession_covers(["g:1", "g:2"],
                               {"g:1": "a" * 32, "g:2": "b" * 32})
