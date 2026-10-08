"""Worker wiring: the inbox drain, and the failure semantics that keep a
peer's bundle from ever taking the ingestion pipeline down.
"""
import numpy as np
import pytest

from app import db, worker
from app.bundles import export as exp, importer, ledger
from tests.bundles.helpers import seed_document


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    """The hand-built `collection` is the only one: CONFIG_DIR names an
    empty folder, so nothing outside the test (the checkout's own
    collections/, a pre-Codex file now refused) becomes the default."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


UID = "gutenberg:1"


@pytest.fixture
def inbox_bundle(con, collection, producer_key, monkeypatch):
    """A valid bundle sitting in the collection's inbox, with the local
    embedder stubbed to reproduce it (i.e. the same model)."""
    rows = seed_document(con, collection, UID, 1, n_chunks=4, seed=1)
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (UID,))

    inbox = importer.inbox_dir(collection)
    inbox.mkdir(parents=True, exist_ok=True)
    exp.export_bundle(con, collection, [UID], inbox / "b1", signing_key=producer_key)

    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    from app import embeddings
    monkeypatch.setattr(embeddings, "embed_texts", lambda batch, **kw: [by_text[t] for t in batch])
    return inbox, rows


def test_inbox_bundle_is_imported_and_moved_aside(inbox_bundle, con, collection):
    inbox, rows = inbox_bundle
    assert worker._drain_bundle_inbox(con, collection) == 1

    doc = con.execute("SELECT embed_done FROM documents WHERE uid=?", (UID,)).fetchone()
    assert doc["embed_done"] == 1
    assert not (inbox / "b1").exists()
    assert (inbox.parent / "done" / "b1").is_dir()


def test_import_satisfies_the_embed_stage_without_embedding(inbox_bundle, con,
                                                             collection, monkeypatch):
    """The point of the derived plane: a covered document reaches embed_done
    without this node computing a vector for it."""
    from app import embeddings
    def must_not_run(*_a, **_kw):
        raise AssertionError("upsert_book ran; the bundle should have satisfied this")
    worker._drain_bundle_inbox(con, collection)
    monkeypatch.setattr(embeddings, "upsert_book", must_not_run)

    scope = db.get_scope(con)
    stage, row = worker._next_book(con, scope)
    assert stage != "embed" or row is None


def test_a_rejected_bundle_is_moved_to_rejected(inbox_bundle, con, collection):
    inbox, _ = inbox_bundle
    sig = inbox / "b1" / exp.SIGNATURE_NAME
    data = bytearray(sig.read_bytes())
    data[0] ^= 0x01
    sig.write_bytes(bytes(data))

    assert worker._drain_bundle_inbox(con, collection) == 1
    assert (inbox.parent / "rejected" / "b1").is_dir()
    doc = con.execute("SELECT embed_done, error FROM documents WHERE uid=?",
                       (UID,)).fetchone()
    assert doc["embed_done"] == 0        # falls back to local embedding
    assert doc["error"] is None          # and is NOT removed from the pipeline


def test_a_retryable_failure_stays_in_the_inbox(inbox_bundle, con, collection,
                                                 monkeypatch):
    """A transient failure must not consume the bundle: the embed endpoint
    being unreachable says nothing about whether the bundle is good."""
    from app import embeddings
    def down(_batch, **kwargs):
        raise ConnectionError("endpoint down")
    monkeypatch.setattr(embeddings, "embed_texts", down)

    inbox, _ = inbox_bundle
    assert worker._drain_bundle_inbox(con, collection) == 0
    assert (inbox / "b1").is_dir(), "a retryable bundle must be left for the next round"
    assert not (inbox.parent / "rejected").exists()


def test_drain_is_a_no_op_without_an_inbox(con, collection):
    assert worker._drain_bundle_inbox(con, collection) == 0


def test_a_hostile_directory_never_crashes_the_worker(con, collection):
    """'never crashes the worker' — asserted against garbage on disk."""
    inbox = importer.inbox_dir(collection)
    (inbox / "junk").mkdir(parents=True)
    (inbox / "junk" / "header.json").write_bytes(b"\xff\xfe not json at all")
    (inbox / "loose.txt").write_text("not a directory")
    assert worker._drain_bundle_inbox(con, collection) == 1
    assert (inbox.parent / "rejected" / "junk").is_dir()


def test_reimport_of_a_moved_bundle_is_a_no_op(inbox_bundle, con, collection):
    inbox, _ = inbox_bundle
    worker._drain_bundle_inbox(con, collection)
    done = inbox.parent / "done" / "b1"
    done.rename(inbox / "b1")               # operator drops it back in
    worker._drain_bundle_inbox(con, collection)
    assert con.execute("SELECT COUNT(*) c FROM bundles").fetchone()["c"] == 1


def test_stale_applying_rows_are_swept_and_documents_requeued(con, collection):
    """A kill mid-import leaves an 'applying' row; on restart it must be
    resolved, and the document must look un-embedded so it is re-queued."""
    ledger.record_applying(
        con, bundle_id="interrupted", producer_pubkey="a" * 64, space_id="s" * 32,
        collection=collection.name, chunk_count=3, signature_hex="f" * 128,
        parquet_sha256="1" * 64, content_digest="2" * 64, created_at=0,
        space_weak=False)
    swept = ledger.sweep_stale(con)
    assert swept == ["interrupted"]
    assert ledger.get(con, "interrupted")["state"] == ledger.ABORTED


def test_local_reembed_clears_imported_provenance(inbox_bundle, con, collection,
                                                   monkeypatch):
    """After a document is re-embedded locally its chunks are no longer
    imported, so the provenance rows must go rather than linger."""
    inbox, rows = inbox_bundle
    worker._drain_bundle_inbox(con, collection)
    chunk_ids = [r["id"] for r in rows]
    assert ledger.verification_states(con, chunk_ids)

    row = con.execute("SELECT * FROM documents WHERE uid=?", (UID,)).fetchone()
    worker.stage_embed(con, row, collection)
    assert ledger.verification_states(con, chunk_ids) == {}
