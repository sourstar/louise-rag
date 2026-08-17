"""Export: packing and signing a bundle from the local Lance table.

The load-bearing assertions here are (1) text never reaches the wire, and
(2) re-export is reproducible — the acceptance criterion.
"""
import hashlib
import json

import pyarrow.parquet as pq
import pytest

from app.bundles import export as exp
from app.bundles import format as fmt
from app.bundles.keys import NodeKeyError

from tests.bundles.helpers import DIM, seed_document


def _export(con, collection, key, tmp_path, uids=None, name="bundle"):
    if uids is None:
        uids = ["gutenberg:1"]
    return exp.export_bundle(con, collection, uids, tmp_path / name, signing_key=key)


@pytest.fixture
def one_doc(con, collection):
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    return con


# ─────────────────────────── the no-text invariant ───────────────────────────

def test_parquet_carries_only_chunk_id_and_vector(one_doc, collection, producer_key, tmp_path):
    """'no text, ever' is the whole point of the derived plane. Asserted on the
    written file rather than trusted to the exporter's docstring."""
    _export(one_doc, collection, producer_key, tmp_path)
    table = pq.read_table(tmp_path / "bundle" / exp.VECTORS_NAME)
    assert table.column_names == ["chunk_id", "vector"]


def test_no_chunk_text_appears_anywhere_in_the_bundle(one_doc, collection, producer_key,
                                                       tmp_path):
    """Belt and braces: grep every byte of every file for the text we seeded."""
    _export(one_doc, collection, producer_key, tmp_path)
    for path in (tmp_path / "bundle").iterdir():
        blob = path.read_bytes()
        assert b"chunk 0 of" not in blob, f"chunk text leaked into {path.name}"
        assert b"hello world" not in blob, f"raw document text leaked into {path.name}"


def test_header_carries_no_title_or_path(one_doc, collection, producer_key, tmp_path):
    """Titles and authors are exactly the content the no-text rule withholds;
    paths are deployment topology."""
    _export(one_doc, collection, producer_key, tmp_path)
    raw = (tmp_path / "bundle" / exp.HEADER_NAME).read_bytes()
    assert b"Doc 1" not in raw
    assert b"corpus" not in raw
    assert set(json.loads(raw)) == fmt._HEADER_KEYS


# ─────────────────────────── determinism ───────────────────────────

def test_re_export_is_byte_identical(one_doc, collection, producer_key, tmp_path,
                                      monkeypatch):
    """Acceptance: 'untampered round-trip is bit-stable across two export runs'.

    created_at is pinned because it is a wall-clock field; everything else must
    be reproducible on its own.
    """
    monkeypatch.setattr(exp.time, "time", lambda: 1_700_000_000)
    first = _export(one_doc, collection, producer_key, tmp_path, name="a")
    second = _export(one_doc, collection, producer_key, tmp_path, name="b")

    for filename in (exp.HEADER_NAME, exp.VECTORS_NAME,
                     exp.POSSESSION_NAME, exp.SIGNATURE_NAME):
        assert (tmp_path / "a" / filename).read_bytes() == \
               (tmp_path / "b" / filename).read_bytes(), f"{filename} is not reproducible"
    assert first["bundle_id"] == second["bundle_id"]


def test_content_digest_is_stable_even_if_the_container_is_not(one_doc, collection,
                                                                producer_key, tmp_path,
                                                                monkeypatch):
    """The container-independent digest is the fallback that keeps the signature
    meaningful across a pyarrow upgrade, so it must not depend on writer
    options or on wall-clock time."""
    monkeypatch.setattr(exp.time, "time", lambda: 1_700_000_000)
    _export(one_doc, collection, producer_key, tmp_path, name="a")
    monkeypatch.setattr(exp, "_PARQUET_OPTS", {**exp._PARQUET_OPTS, "row_group_size": 1})
    _export(one_doc, collection, producer_key, tmp_path, name="b")

    a = json.loads((tmp_path / "a" / exp.HEADER_NAME).read_bytes())
    b = json.loads((tmp_path / "b" / exp.HEADER_NAME).read_bytes())
    assert a["content_digest"] == b["content_digest"]
    # ...while the physical file genuinely did change, proving the test has teeth
    assert (tmp_path / "a" / exp.VECTORS_NAME).read_bytes() != \
           (tmp_path / "b" / exp.VECTORS_NAME).read_bytes()


# ─────────────────────────── signature wiring ───────────────────────────

def test_exported_bundle_verifies(one_doc, collection, producer_key, tmp_path):
    result = _export(one_doc, collection, producer_key, tmp_path)
    d = tmp_path / "bundle"
    header_bytes = (d / exp.HEADER_NAME).read_bytes()
    payload = fmt.signed_payload(
        header_bytes,
        hashlib.sha256((d / exp.VECTORS_NAME).read_bytes()).digest(),
        hashlib.sha256((d / exp.POSSESSION_NAME).read_bytes()).digest())
    header = fmt.parse_header(header_bytes)
    fmt.verify_signature(payload, (d / exp.SIGNATURE_NAME).read_bytes(),
                          header.producer_pubkey)
    assert fmt.bundle_id_of(payload) == result["bundle_id"]


def test_signature_file_is_exactly_64_bytes(one_doc, collection, producer_key, tmp_path):
    _export(one_doc, collection, producer_key, tmp_path)
    assert len((tmp_path / "bundle" / exp.SIGNATURE_NAME).read_bytes()) == 64


def test_header_declares_the_real_part_sizes(one_doc, collection, producer_key, tmp_path):
    """The importer checks these before opening either file, so they must be
    exact rather than approximate."""
    _export(one_doc, collection, producer_key, tmp_path)
    d = tmp_path / "bundle"
    header = fmt.parse_header((d / exp.HEADER_NAME).read_bytes())
    assert header.parquet_bytes == (d / exp.VECTORS_NAME).stat().st_size
    assert header.possession_bytes == (d / exp.POSSESSION_NAME).stat().st_size


# ─────────────────────────── contents ───────────────────────────

def test_header_summarises_the_payload(one_doc, collection, producer_key, tmp_path):
    _export(one_doc, collection, producer_key, tmp_path)
    header = fmt.parse_header((tmp_path / "bundle" / exp.HEADER_NAME).read_bytes())
    assert header.uids == ("gutenberg:1",)
    assert header.chunk_count == 3
    assert header.dim == DIM
    assert header.dtype == "f32"
    assert header.collection == "gutenberg"


def test_possession_sidecar_covers_every_uid(one_doc, collection, producer_key, tmp_path):
    _export(one_doc, collection, producer_key, tmp_path)
    mapping = fmt.parse_possession(
        (tmp_path / "bundle" / exp.POSSESSION_NAME).read_bytes())
    assert set(mapping) == {"gutenberg:1"}
    assert len(mapping["gutenberg:1"]) == 32


def test_possession_hash_is_backfilled_into_the_manifest(one_doc, collection,
                                                          producer_key, tmp_path):
    """discovery.py leaves txt_hash NULL on first discovery; export fills it."""
    before = one_doc.execute(
        "SELECT txt_hash FROM documents WHERE uid='gutenberg:1'").fetchone()
    assert before["txt_hash"] is None
    _export(one_doc, collection, producer_key, tmp_path)
    after = one_doc.execute(
        "SELECT txt_hash FROM documents WHERE uid='gutenberg:1'").fetchone()
    assert after["txt_hash"] and len(after["txt_hash"]) == 32


def test_multiple_documents_export_together(con, collection, producer_key, tmp_path):
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=2, seed=1)
    seed_document(con, collection, "gutenberg:2", 2, n_chunks=4, seed=2)
    result = _export(con, collection, producer_key, tmp_path,
                     uids=["gutenberg:1", "gutenberg:2"])
    assert result["uids"] == 2 and result["chunks"] == 6
    header = fmt.parse_header((tmp_path / "bundle" / exp.HEADER_NAME).read_bytes())
    assert header.uids == ("gutenberg:1", "gutenberg:2")


def test_rows_are_written_in_canonical_order(con, collection, producer_key, tmp_path):
    """Numeric on seq, not lexicographic — ':10:' must not precede ':2:'."""
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=12, seed=3)
    _export(con, collection, producer_key, tmp_path)
    table = pq.read_table(tmp_path / "bundle" / exp.VECTORS_NAME)
    seqs = [fmt.seq_of(c) for c in table.column("chunk_id").to_pylist()]
    assert seqs == sorted(seqs)
    assert seqs == list(range(12))


# ─────────────────────────── refusals ───────────────────────────

def test_unknown_uid_is_refused(one_doc, collection, producer_key, tmp_path):
    with pytest.raises(exp.ExportError, match="not in this node's manifest"):
        _export(one_doc, collection, producer_key, tmp_path, uids=["gutenberg:999"])


def test_export_without_a_key_is_refused(one_doc, collection, tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "NODE_KEY_PATH", None)
    with pytest.raises(NodeKeyError, match="NODE_KEY_PATH"):
        exp.export_bundle(one_doc, collection, ["gutenberg:1"], tmp_path / "b")


def test_export_without_a_pinned_space_is_refused(one_doc, collection, producer_key,
                                                   tmp_path):
    one_doc.execute("DELETE FROM space")
    with pytest.raises(exp.ExportError, match="space_id"):
        _export(one_doc, collection, producer_key, tmp_path)


def test_export_with_no_vectors_is_refused(con, collection, producer_key, tmp_path):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,updated_at) "
        "VALUES('gutenberg:7','gutenberg','7','/nope','en',0)")
    with pytest.raises(exp.ExportError):
        _export(con, collection, producer_key, tmp_path, uids=["gutenberg:7"])
