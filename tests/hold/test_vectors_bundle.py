"""tests/hold/test_vectors_bundle.py — the four files, written and read.

docs/09-formats.md §9.7. Every test here is about a real directory on
disk, because the module's whole job is the boundary between rows in
memory and bytes another peer will read.
"""
import hashlib
import json
from pathlib import Path

import pytest
from nacl import signing

from app.hold import bundle as bd, vectors as vec
from app.hold.bundle import BundleError

KEY = signing.SigningKey(b"\x33" * 32)
PUB = "ed25519:" + bytes(KEY.verify_key).hex()
DIM = 4
N = 16


def header(**over):
    h = {
        "version": 1,
        "collection": "sha256:" + "e5" * 32,
        "space_id": "sha256:" + "41" * 32,
        "snapshot_id": "2026-08",
        "shard": "lang=it/00007",
        "uids": ["g:1", "g:2"],
        "dim": DIM,
        "dtype": "f32",
        "producer_pubkey": PUB,
        "encoder_image": "sha256:" + "8c" * 32,
        "encoder_weights": "sha256:" + "e2" * 32,
        "created_at": 1756713600,
        "digest_alg": "sha256",
        "sig_alg": "ed25519",
        "challenge": {"set": "sha256:" + "6b" * 32, "n": N},
    }
    h.update(over)
    return h


def challenge_rows(n=N):
    return [(bd.challenge_id(i), [float(i), 0.5, -1.0, 2.0]) for i in range(n)]


def chunk_rows():
    """Deliberately NOT in §9.7's order: `:10:` sorts before `:2:` as a
    string, and the writer must fix that."""
    return [("g:2:10:1", [4.0, 0.0, 0.0, 1.0]),
            ("g:2:2:1", [3.0, 0.0, 0.0, 1.0]),
            ("g:1:1:1", [1.0, 0.0, 0.0, 1.0]),
            ("g:1:0:1", [0.0, 0.0, 0.0, 1.0])]


POSSESSION = {"g:1": "a" * 32, "g:2": "b" * 32}


def write(dest, **over):
    kw = dict(header=header(), challenge_rows=challenge_rows(),
              chunk_rows=chunk_rows(), possession=dict(POSSESSION), key=KEY)
    kw.update(over)
    return vec.write(dest, **kw)


# ---- what lands on disk ---------------------------------------------------

def test_a_bundle_is_a_directory_of_exactly_four_files(tmp_path: Path):
    d = tmp_path / "b"
    write(d)
    assert sorted(p.name for p in d.iterdir()) == sorted(bd.FILES)


def test_the_signature_is_64_raw_bytes(tmp_path: Path):
    d = tmp_path / "b"
    write(d)
    assert (d / "MANIFEST.sig").stat().st_size == 64


def test_the_id_is_returned_and_is_over_the_signed_bytes(tmp_path: Path):
    d = tmp_path / "b"
    bid = write(d)
    header_raw = (d / "header.json").read_bytes()
    payload = bd.payload(
        header_raw,
        hashlib.sha256((d / "vectors.parquet").read_bytes()).digest(),
        hashlib.sha256((d / "possession.json").read_bytes()).digest())
    assert bid == bd.bundle_id(payload)


# ---- the derived fields ---------------------------------------------------

def test_the_derived_fields_are_filled_and_may_not_be_supplied(tmp_path: Path):
    """A caller that computed them by hand could get them wrong, and they
    are inside the signature."""
    d = tmp_path / "b"
    write(d)
    h = json.loads((d / "header.json").read_bytes())
    assert h["chunk_count"] == 4
    assert h["parquet_bytes"] == (d / "vectors.parquet").stat().st_size
    assert h["possession_bytes"] == (d / "possession.json").stat().st_size
    assert h["content_digest"].startswith("sha256:")

    with pytest.raises(BundleError, match="derived from the files"):
        write(tmp_path / "c", header=header(chunk_count=99))


def test_chunk_count_excludes_the_challenge_rows(tmp_path: Path):
    """§9.7: "rows in `vectors.parquet` AFTER the challenge rows"."""
    d = tmp_path / "b"
    write(d)
    h, rows, _, _ = vec.read(d)
    assert h["chunk_count"] == 4
    assert len(rows) == 4 + N


# ---- row order ------------------------------------------------------------

def test_rows_are_written_in_9_7s_order(tmp_path: Path):
    """Challenge rows first in index order, then chunks sorted by (uid
    bytes, seq as an INTEGER) — under which `:10:` follows `:2:`. The
    fixture supplies them out of order on purpose."""
    d = tmp_path / "b"
    write(d)
    _, rows, _, _ = vec.read(d)
    ids = [r[0] for r in rows]
    assert ids[:N] == [bd.challenge_id(i) for i in range(N)]
    assert ids[N:] == ["g:1:0:1", "g:1:1:1", "g:2:2:1", "g:2:10:1"]


def test_a_string_sort_would_disagree(tmp_path: Path):
    """The property that makes the tuple sort worth specifying at all."""
    d = tmp_path / "b"
    write(d)
    _, rows, _, _ = vec.read(d)
    chunks = [r[0] for r in rows[N:]]
    assert chunks != sorted(chunks), "a string sort puts :10: before :2:"


def test_a_challenge_row_out_of_index_order_is_refused(tmp_path: Path):
    bad = challenge_rows()
    bad[3] = ("calibration:9", bad[3][1])
    with pytest.raises(BundleError, match="challenge row 3"):
        write(tmp_path / "b", challenge_rows=bad)


# ---- reading it back ------------------------------------------------------

def test_a_bundle_round_trips(tmp_path: Path):
    d = tmp_path / "b"
    bid = write(d)
    h, rows, poss, got_id = vec.read(d)
    assert got_id == bid
    assert h["uids"] == ["g:1", "g:2"]
    assert poss == POSSESSION
    assert rows[N][0] == "g:1:0:1"
    assert rows[N][1] == pytest.approx([0.0, 0.0, 0.0, 1.0])


def test_a_missing_file_is_refused_by_name(tmp_path: Path):
    d = tmp_path / "b"
    write(d)
    (d / "possession.json").unlink()
    with pytest.raises(BundleError, match="missing possession.json"):
        vec.read(d)


def test_a_tampered_parquet_is_caught_by_the_signature(tmp_path: Path):
    d = tmp_path / "b"
    write(d)
    raw = bytearray((d / "vectors.parquet").read_bytes())
    raw[-40] ^= 0xFF
    (d / "vectors.parquet").write_bytes(bytes(raw))
    with pytest.raises(BundleError, match="does not verify"):
        vec.read(d)


def test_a_tampered_header_is_caught_by_the_signature(tmp_path: Path):
    d = tmp_path / "b"
    write(d)
    h = json.loads((d / "header.json").read_bytes())
    h["snapshot_id"] = "2026-09"
    (d / "header.json").write_bytes(bd.serialise(h))
    with pytest.raises(BundleError, match="does not verify"):
        vec.read(d)


def test_possession_is_read_only_after_the_signature_verifies(tmp_path: Path):
    """§9.7: "Read only after the signature verifies; its digest is inside
    the signed payload." A reader that parsed it first would be acting on
    bytes nobody has vouched for — so a possession file that is not even
    JSON must fail as a SIGNATURE error, not a parse error."""
    d = tmp_path / "b"
    write(d)
    (d / "possession.json").write_bytes(b"{ not json at all")
    with pytest.raises(BundleError, match="does not verify"):
        vec.read(d)


def test_a_re_signed_bundle_with_altered_rows_fails_the_content_digest(
        tmp_path: Path):
    """The attack the signature alone does not stop: a producer who
    re-signs. `content_digest` is over the logical rows, so a second
    implementation catches what a byte comparison of an unstable container
    cannot."""
    d = tmp_path / "b"
    write(d)
    h = json.loads((d / "header.json").read_bytes())
    h["content_digest"] = "sha256:" + "00" * 32
    header_raw = bd.serialise(h)
    (d / "header.json").write_bytes(header_raw)
    payload = bd.payload(
        header_raw,
        hashlib.sha256((d / "vectors.parquet").read_bytes()).digest(),
        hashlib.sha256((d / "possession.json").read_bytes()).digest())
    (d / "MANIFEST.sig").write_bytes(bd.sign(payload, KEY))
    with pytest.raises(BundleError, match="the rows hash to"):
        vec.read(d)


def test_a_first_seen_producer_key_is_admitted(tmp_path: Path):
    """03 §3.5 and 08 §8.1: no producer key need be known, trusted or seen
    before. Nothing in this path has an opinion about which key signed."""
    other = signing.SigningKey(b"\x77" * 32)
    pub = "ed25519:" + bytes(other.verify_key).hex()
    d = tmp_path / "b"
    write(d, header=header(producer_pubkey=pub), key=other)
    assert vec.read(d)[0]["producer_pubkey"] == pub


# ---- possession must cover the uids ---------------------------------------

def test_possession_must_cover_every_declared_uid(tmp_path: Path):
    with pytest.raises(BundleError, match="malformed bundle"):
        write(tmp_path / "b", possession={"g:1": "a" * 32})


def test_possession_may_not_carry_undeclared_uids(tmp_path: Path):
    with pytest.raises(BundleError, match="does not declare"):
        write(tmp_path / "b",
              possession=dict(POSSESSION, **{"g:9": "c" * 32}))


# ---- splitting the challenge off ------------------------------------------

def test_the_challenge_rows_split_off_before_any_tier_sees_them(tmp_path: Path):
    """A producer that could get challenge rows counted as chunks would
    dilute every statistic computed over the chunks."""
    d = tmp_path / "b"
    write(d)
    h, rows, _, _ = vec.read(d)
    answers = vec.challenge_of(rows, h["challenge"]["n"])
    chunks = vec.chunks_of(rows, h["challenge"]["n"])
    assert len(answers) == N
    assert len(chunks) == h["chunk_count"]
    assert all(not c[0].startswith("calibration:") for c in chunks)


def test_challenge_of_refuses_rows_that_are_not_the_challenge(tmp_path: Path):
    rows = [("g:1:0:1", [1.0])] * 20
    with pytest.raises(BundleError, match="challenge rows come"):
        vec.challenge_of(rows, N)


# ---- the parquet's shape --------------------------------------------------

def test_the_parquet_has_exactly_two_columns(tmp_path: Path):
    """§9.7 fixes `chunk_id` and `vector`. The old exporter carried a third
    `seq` column; `seq` is parsed out of the chunk id (§9.5)."""
    import pyarrow.parquet as pq
    d = tmp_path / "b"
    write(d)
    assert pq.read_table(d / "vectors.parquet").schema.names == \
        ["chunk_id", "vector"]


def test_f16_and_f32_are_both_written(tmp_path: Path):
    for dtype in ("f16", "f32"):
        d = tmp_path / dtype
        write(d, header=header(dtype=dtype))
        h, rows, _, _ = vec.read(d)
        assert h["dtype"] == dtype
        assert len(rows[0][1]) == DIM
