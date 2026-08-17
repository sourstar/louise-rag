"""Verify-on-use (§6 T4) — the only layer that catches targeted poisoning.

Import's admission checks sample a handful of chunks out of thousands, so they
cannot find three deliberately mismapped ones. A poisoned vector is inert until
retrieval surfaces it; at that moment we know exactly which chunks to check.
"""
import numpy as np
import pytest

from app import embeddings, worker
from app.bundles import export as exp, importer, ledger, trust, verify_on_use
from tests.bundles.helpers import seed_document, unit_vectors

UID = "gutenberg:1"


@pytest.fixture
def imported(con, collection, producer_key, monkeypatch):
    """A document whose vectors arrived via an accepted bundle."""
    rows = seed_document(con, collection, UID, 1, n_chunks=4, seed=1)
    con.execute("UPDATE documents SET embed_done=0 WHERE uid=?", (UID,))
    inbox = importer.inbox_dir(collection)
    inbox.mkdir(parents=True, exist_ok=True)
    exp.export_bundle(con, collection, [UID], inbox / "b1", signing_key=producer_key)

    by_text = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda batch, **kw: [by_text[t] for t in batch])
    assert worker._drain_bundle_inbox(con, collection) == 1
    return rows, by_text


def _chunk_ids(rows):
    return [r["id"] for r in rows]


# ─────────────────────────── default off ───────────────────────────

def test_imported_chunks_start_unverified(imported, con):
    rows, _ = imported
    states = ledger.verification_states(con, _chunk_ids(rows))
    assert all(s["state"] == "imported" for s in states.values())


def test_locally_embedded_chunks_need_no_verification(con, collection):
    """Absence of a row means local, and re-embedding our own vectors would
    only confirm our own arithmetic."""
    rows = seed_document(con, collection, "gutenberg:9", 9, n_chunks=3, seed=2)
    assert verify_on_use.pending(con, _chunk_ids(rows)) == []
    summary = verify_on_use.verify_chunks(con, collection, _chunk_ids(rows))
    assert summary["checked"] == 0


# ─────────────────────────── honest producer ───────────────────────────

def test_honest_vectors_verify_and_stay_verified(imported, con, collection):
    rows, _ = imported
    summary = verify_on_use.verify_chunks(con, collection, _chunk_ids(rows))
    assert summary["checked"] == len(rows) and summary["failed"] == 0
    states = ledger.verification_states(con, _chunk_ids(rows))
    assert all(s["state"] == "verified" for s in states.values())

    # permanent: a second pass has nothing left to do, which is what keeps the
    # lifetime cost ~2% rather than per-query
    assert verify_on_use.verify_chunks(con, collection, _chunk_ids(rows))["checked"] == 0


# ─────────────────────────── targeted poisoning ───────────────────────────

def test_a_single_poisoned_vector_is_caught_on_use(imported, con, collection,
                                                    producer_key):
    """The attack no import-time sampling can find: most vectors are honest,
    one is not."""
    rows, by_text = imported
    poisoned_text = rows[2]["text"]
    corrupted = dict(by_text)
    corrupted[poisoned_text] = unit_vectors(1, seed=8888)[0]

    summary = verify_on_use.verify_chunks(
        con, collection, _chunk_ids(rows),
        embed_fn=lambda batch: [corrupted[t] for t in batch])
    assert summary["failed"] == 1 and summary["verified"] == len(rows) - 1

    states = ledger.verification_states(con, _chunk_ids(rows))
    assert states[rows[2]["id"]]["state"] == "failed"


def test_a_failure_quarantines_the_producer(imported, con, collection, producer_key):
    """§6: 'proof of forgery ... quarantine the key'."""
    rows, by_text = imported
    corrupted = dict(by_text)
    corrupted[rows[0]["text"]] = unit_vectors(1, seed=7777)[0]

    summary = verify_on_use.verify_chunks(
        con, collection, _chunk_ids(rows),
        embed_fn=lambda batch: [corrupted[t] for t in batch])
    producer = bytes(producer_key.verify_key).hex()
    assert summary["quarantined"] == [producer]
    with pytest.raises(trust.BundleUntrusted, match="quarantined"):
        trust.check_admission(con, producer)


def test_the_recorded_cosine_is_the_evidence(imported, con, collection):
    rows, by_text = imported
    corrupted = dict(by_text)
    corrupted[rows[1]["text"]] = unit_vectors(1, seed=6666)[0]
    verify_on_use.verify_chunks(con, collection, _chunk_ids(rows),
                                 embed_fn=lambda batch: [corrupted[t] for t in batch])
    row = ledger.verification_states(con, [rows[1]["id"]])[rows[1]["id"]]
    assert row["cosine"] < verify_on_use.VERIFY_FLOOR


# ─────────────────────────── robustness ───────────────────────────

def test_an_endpoint_outage_does_not_mark_anything_failed(imported, con, collection):
    """An outage is not evidence of forgery — chunks stay unverified rather
    than being condemned."""
    rows, _ = imported

    def down(_batch):
        raise ConnectionError("endpoint down")
    summary = verify_on_use.verify_chunks(con, collection, _chunk_ids(rows),
                                           embed_fn=down)
    assert summary.get("unavailable") and summary["failed"] == 0
    states = ledger.verification_states(con, _chunk_ids(rows))
    assert all(s["state"] == "imported" for s in states.values())


def test_verification_never_raises(imported, con, collection):
    """A verification pass must not be able to break the query that triggered
    it."""
    rows, _ = imported
    assert verify_on_use.verify_chunks(
        con, collection, _chunk_ids(rows),
        embed_fn=lambda batch: (_ for _ in ()).throw(RuntimeError("boom")))


def test_unknown_chunk_ids_are_ignored(con, collection):
    assert verify_on_use.verify_chunks(con, collection, ["nope:1:0:1"])["checked"] == 0


# ─────────────────────────── badges ───────────────────────────

def test_annotate_marks_local_and_imported_hits(imported, con, collection):
    rows, _ = imported
    seed_document(con, collection, "gutenberg:5", 5, n_chunks=2, seed=3)
    hits = [{"chunk_id": rows[0]["id"]}, {"chunk_id": "gutenberg:5:0:1"}]
    verify_on_use.annotate(con, hits)
    assert hits[0]["verification"] == "imported"
    assert hits[1]["verification"] == "local"
    assert hits[1]["producer"] == "local"


def test_annotate_reflects_a_verified_chunk(imported, con, collection):
    rows, _ = imported
    verify_on_use.verify_chunks(con, collection, _chunk_ids(rows))
    hits = [{"chunk_id": rows[0]["id"]}]
    verify_on_use.annotate(con, hits)
    assert hits[0]["verification"] == "verified"
    assert hits[0]["verification_cosine"] > verify_on_use.VERIFY_FLOOR
