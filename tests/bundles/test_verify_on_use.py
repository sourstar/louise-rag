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


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    """The hand-built `collection` is the only one: CONFIG_DIR names an
    empty folder, so nothing outside the test (the checkout's own
    collections/, a pre-Codex file now refused) becomes the default."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


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
    with pytest.raises(trust.BundleUntrusted, match="burned"):
        trust.check_admission(producer)          # the peer-wide trust file


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


# ─────────────────── cited: the worker's queue (05 §5.3, 03 §3.5; step 11) ───────────────────

def test_a_search_cites_its_imported_chunks_for_the_worker(imported, con, collection):
    """Every chunk a search returns is queued — the imported ones: a local
    chunk has nothing to verify."""
    from app import retrieval
    rows, _ = imported
    local = seed_document(con, collection, "gutenberg:9", 9, n_chunks=2, seed=2)
    hits = [{"chunk_id": r["id"], "book_id": 1} for r in rows[:2]] + [{"chunk_id": local[0]["id"], "book_id": 9}]
    retrieval._attach_metadata_and_enqueue(con, collection, hits)
    assert set(verify_on_use.queued(con, 10)) == {rows[0]["id"], rows[1]["id"]}
    retrieval._attach_metadata_and_enqueue(con, collection, hits)
    assert len(verify_on_use.queued(con, 10)) == 2, "cited twice is queued once"


def test_the_worker_drains_the_queue_once_and_marks_what_it_verified(imported, con, collection):
    rows, by_text = imported
    honest = lambda batch: [by_text[t] for t in batch]  # noqa: E731
    assert verify_on_use.cite(con, _chunk_ids(rows)) == len(rows)
    out = verify_on_use.drain(con, collection, embed_fn=honest)
    assert (out["checked"], out["verified"], out["failed"], out["inconclusive"], out["queued"]) == \
        (len(rows), len(rows), 0, 0, 0)
    states = ledger.verification_states(con, _chunk_ids(rows))
    assert all(s["state"] == "verified" for s in states.values())
    assert verify_on_use.cite(con, _chunk_ids(rows)) == 0, "verified forever: a later search queues nothing"
    assert verify_on_use.drain(con, collection, embed_fn=honest)["checked"] == 0


def test_a_pass_is_bounded_and_the_rest_waits_its_turn(imported, con, collection):
    rows, by_text = imported
    verify_on_use.cite(con, _chunk_ids(rows))
    out = verify_on_use.drain(con, collection, limit=1, embed_fn=lambda batch: [by_text[t] for t in batch])
    assert out["checked"] == 1 and out["queued"] == len(rows) - 1


def test_a_miss_is_a_forgery_only_when_it_reproduces(imported, con, collection, producer_key):
    """03 §3.5: 'a T4 miss that reproduces on a second recompute'. One that
    does not is left unverified, not condemned, and burns nobody."""
    rows, by_text = imported
    bad = unit_vectors(1, seed=4242)[0]
    calls = []

    def flaky(batch):                                   # wrong once, right on the second recompute
        calls.append(list(batch))
        return [bad if (t == rows[1]["text"] and len(calls) == 1) else by_text[t] for t in batch]
    verify_on_use.cite(con, _chunk_ids(rows))
    out = verify_on_use.drain(con, collection, embed_fn=flaky)
    assert (out["verified"], out["failed"], out["inconclusive"]) == (len(rows) - 1, 0, 1)
    assert calls[1] == [rows[1]["text"]], "only the missed chunk is recomputed again"
    assert ledger.verification_states(con, [rows[1]["id"]])[rows[1]["id"]]["state"] == "imported"
    assert out["quarantined"] == [] and out["queued"] == 0

    proofs = []
    assert verify_on_use.cite(con, _chunk_ids(rows)) == 1, "the inconclusive chunk alone is still imported"
    out = verify_on_use.drain(con, collection, on_forgery=proofs.append,
                              embed_fn=lambda batch: [bad if t == rows[1]["text"] else by_text[t] for t in batch])
    assert out["failed"] == 1 and [p.chunk_id for p in proofs] == [rows[1]["id"]]
    p = proofs[0]
    producer = bytes(producer_key.verify_key).hex()
    assert (p.producer, p.uid, p.text) == (producer, UID, rows[1]["text"]) and p.bundle_id
    assert np.allclose(p.recomputed, bad) and np.allclose(p.claimed, by_text[rows[1]["text"]])
    assert p.cosine < verify_on_use.VERIFY_FLOOR
    assert out["quarantined"] == [producer]
    assert ledger.verification_states(con, [rows[1]["id"]])[rows[1]["id"]]["state"] == "failed"


def test_an_outage_keeps_the_cited_chunks_queued(imported, con, collection):
    rows, _ = imported
    verify_on_use.cite(con, _chunk_ids(rows))

    def down(_batch):
        raise ConnectionError("encoder down")
    out = verify_on_use.drain(con, collection, embed_fn=down)
    assert out.get("unavailable") and len(verify_on_use.queued(con, 10)) == len(rows), "deferral, not failure"
    assert all(s["state"] == "imported" for s in ledger.verification_states(con, _chunk_ids(rows)).values())


def test_the_worker_verifies_cited_chunks_only_in_a_mode_that_embeds(imported, con, collection, monkeypatch):
    rows, by_text = imported
    verify_on_use.cite(con, _chunk_ids(rows))
    monkeypatch.setattr(worker.node_state, "worker_state", lambda: "acquire-only")
    assert worker._verify_cited(con, collection) is False and len(verify_on_use.queued(con, 10)) == len(rows)
    monkeypatch.setattr(worker.node_state, "worker_state", lambda: "running")
    monkeypatch.setattr(embeddings, "embed_texts", lambda batch, **kw: [by_text[t] for t in batch])
    assert worker._verify_cited(con, collection) is True
    assert verify_on_use.queued(con, 10) == []
    assert worker._verify_cited(con, collection) is False, "nothing queued: nothing done"


def test_a_reproduced_miss_burns_the_key_in_the_trust_file(imported, con, collection, producer_key, monkeypatch,
                                                            tmp_path):
    from app import collections as collections_mod
    from app.hold import trust_file
    rows, by_text = imported
    bad = unit_vectors(1, seed=99)[0]
    root = tmp_path / "louise"
    monkeypatch.setattr(collections_mod, "louise_root", lambda: root)
    monkeypatch.setattr(worker.node_state, "worker_state", lambda: "running")
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda batch, **kw: [bad if t == rows[0]["text"] else by_text[t] for t in batch])
    verify_on_use.cite(con, _chunk_ids(rows))
    assert worker._verify_cited(con, collection) is True
    producer = bytes(producer_key.verify_key).hex()
    entry, = trust_file.read(root)["quarantined"]
    assert entry["key"] == "ed25519:" + producer and entry["attestation"] is None, \
        "a collection without a Codex attests nothing; the burn stands, in 09 §9.2's key form"
    assert verify_on_use.key_text(producer) == "ed25519:" + producer
    assert verify_on_use.key_text("ed25519:" + producer) == "ed25519:" + producer
    assert verify_on_use.key_text(None) is None and verify_on_use.key_text("unknown") == "unknown"
    events = [r["message"] for r in con.execute("SELECT message FROM events WHERE stage='verify' AND level='error'")]
    assert events and "forgery" in events[0] and "key burned" in events[0]
