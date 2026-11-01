"""F1's adversarial suite: the eight prescribed scenarios, each with its
defined correct outcome encoded as an assertion — not a description.

These deliberately cross module boundaries: the consumer catching what a
producer signed, verify-on-use, quarantine, restarts mid-import. Unit suites
already cover each module alone; what F1 owns is the seams. (The scenarios
that ran through architecture-v2's coordinator left with it on 2026-11-01:
04 §4.5 hands work out by rendezvous, never by a coordinator.)
"""
import io
import tarfile
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, embeddings, worker
from app.bundles import export as bundle_export
from app.bundles import importer, ledger, trust, verify_on_use
from tests.integration.conftest import (ATTACKER_KEY, PRODUCER_KEY, mirror_corpus,
                                         model_embed, seed_node)


# Both nodes are Collections built in Python, with no file, and the
# fixtures pin their space from the DEFAULT collection
# (`spaces.gather_components()` with none named) while the importer computes
# it from the node itself — so the node holds no collection file, and the
# default is the same file-less shape. Never whatever `collections/` the
# working directory holds.
@pytest.fixture(autouse=True)
def _no_collection_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-collection-files"))


DOCS = {"gutenberg:1": 1, "gutenberg:2": 2, "gutenberg:3": 3}


def _archive(directory):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(directory.iterdir()):
            if item.is_file():
                archive.add(item, arcname=item.name)
    return buffer.getvalue()


WIDE_UNIVERSE = [f"gutenberg:{i}" for i in range(100, 150)]


@pytest.fixture
def two_nodes(producer, consumer, monkeypatch):
    producer_con, producer_col = producer
    consumer_con, consumer_col = consumer
    rows = seed_node(producer_con, producer_col, DOCS)
    mirror_corpus(producer_col, consumer_con, consumer_col, DOCS)
    monkeypatch.setattr(embeddings, "_embed_texts", model_embed)
    return producer_con, producer_col, consumer_con, consumer_col, rows


def _export(producer_con, producer_col, tmp_path, uids, key=PRODUCER_KEY, name="b"):
    return bundle_export.export_bundle(producer_con, producer_col, uids,
                                        tmp_path / name, signing_key=key)


# ────────────────── 1. corrupted-space bundle ──────────────────

def test_corrupted_space_bundle(two_nodes, tmp_path, monkeypatch):
    """Correct outcome: refused as a space mismatch, ledger records it, the
    document is untouched and local embedding remains available."""
    producer_con, producer_col, consumer_con, consumer_col, _ = two_nodes
    _export(producer_con, producer_col, tmp_path, ["gutenberg:1"])

    from app import spaces
    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda *a: ("0000dead0000beef", False))   # consumer's space moved
    result = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert result["error_class"] == "BundleSpaceMismatch"
    assert ledger.recent(consumer_con)[0]["state"] == ledger.REJECTED
    doc = consumer_con.execute(
        "SELECT embed_done, error FROM documents WHERE uid='gutenberg:1'").fetchone()
    assert doc["embed_done"] == 0 and doc["error"] is None


# ────────────────── 2. wrong-model bundle, end to end ──────────────────

# ────────────────── 3. 5%-poisoned bundle ──────────────────

def test_poisoned_bundle_caught_on_use_and_producer_quarantined(two_nodes, tmp_path,
                                                                 monkeypatch):
    """Targeted poisoning: one mismapped vector inside an otherwise honest,
    correctly signed bundle.

    Import-time sampling catching it is probabilistic (§6: (1-f)^n, and with 8
    of 12 chunks sampled it usually WOULD here) — but that branch is already
    unit-covered, so the sampler is pinned to miss, forcing the interesting
    path: the bundle is ACCEPTED, the poison sits inert until retrieval
    surfaces it, and verify-on-use catches it and burns the key. That is
    exactly the §6 story, asserted end to end.
    """
    producer_con, producer_col, consumer_con, consumer_col, rows = two_nodes
    poisoned_id = rows[5]["id"]
    conn = embeddings._conn(producer_col)
    table = conn.open_table(embeddings.TABLE)
    native = rows[5]["book_id"]
    keep = [r for r in rows if r["book_id"] == native]
    table.delete(f"book_id = {native}")
    table.add([{**r, "vector": (model_embed([r["text"]], salt="poison")[0]
                                 if r["id"] == poisoned_id
                                 else np.asarray(r["vector"], dtype=np.float32))}
               for r in keep])
    summary = _export(producer_con, producer_col, tmp_path, list(DOCS))

    from app.bundles import checks
    # a NESTED context, not monkeypatch.undo(): undo() would also strip the
    # fixture's embed_texts patch and silently break everything after it
    with monkeypatch.context() as nested:
        nested.setattr(checks, "choose_sample",
                       lambda ids, size=8: [c for c in ids if c != poisoned_id][:size])
        result = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert result["ok"], f"the pinned sample should have missed: {result.get('error')}"

    chunk_ids = [r["id"] for r in rows]
    outcome = verify_on_use.verify_chunks(consumer_con, consumer_col, chunk_ids)
    assert outcome["failed"] == 1
    assert outcome["quarantined"] == [summary["producer"]]
    states = ledger.verification_states(consumer_con, [poisoned_id])
    assert states[poisoned_id]["state"] == "failed"


# ────────────────── 4. replayed old bundle ──────────────────

def test_replayed_bundle_is_a_no_op(two_nodes, tmp_path):
    producer_con, producer_col, consumer_con, consumer_col, rows = two_nodes
    _export(producer_con, producer_col, tmp_path, ["gutenberg:1"])
    first = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert first["ok"]
    replay = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert replay["ok"] and replay.get("skipped")
    assert consumer_con.execute("SELECT COUNT(*) c FROM bundles").fetchone()["c"] == 1

    table = embeddings._conn(consumer_col).open_table(embeddings.TABLE)
    ids = table.search().where("book_id IN (1)").limit(100).to_arrow().column("id")
    assert len(ids) == len(set(ids.to_pylist()))


def test_replayed_bundle_for_a_revised_document_is_refused(two_nodes, tmp_path):
    """The sharper replay: the document was revised locally after the bundle
    was cut. Possession binding must refuse stale vectors rather than attach
    them to text they were not computed from."""
    producer_con, producer_col, consumer_con, consumer_col, _ = two_nodes
    _export(producer_con, producer_col, tmp_path, ["gutenberg:1"])

    revised = consumer_col.corpus_dir / "1" / "pg1.txt"
    revised.write_text(revised.read_text(encoding="utf-8") + "\n\nREVISED EDITION.",
                        encoding="utf-8")
    consumer_con.execute("UPDATE documents SET txt_hash=NULL WHERE uid='gutenberg:1'")

    result = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert not result["ok"]
    assert result["error_class"] in ("PossessionError", "BundleFormatError")


# ────────────────── 5. expired-lease double completion ──────────────────

# ────────────────── 6. import racing local embedding ──────────────────

def test_import_local_race_is_covered_at_module_level():
    """Both orders are asserted in tests/bundles/test_import.py::
    test_concurrent_import_and_local_embed_leave_one_consistent_copy, using the
    real cross-process writer lock. Re-running a thread race here would add
    flake surface without adding coverage; the seam-level artifact of the race
    (the crash between delete and add) is asserted below instead."""


def test_worker_restart_after_crash_mid_import_recovers(two_nodes, tmp_path,
                                                         monkeypatch):
    producer_con, producer_col, consumer_con, consumer_col, _ = two_nodes
    _export(producer_con, producer_col, tmp_path, ["gutenberg:1"])

    class Boom(RuntimeError):
        pass

    def explode(*_args, **_kw):
        raise Boom("crash after embed_done was cleared, before the write landed")
    # _write_rows, not _book_filter: a FRESH consumer has no Lance table yet, so
    # its first import takes the create path and never touches the delete
    # filter — patching that would (and did) miss entirely.
    with monkeypatch.context() as nested:
        nested.setattr(importer, "_write_rows", explode)
        with pytest.raises(Boom):
            importer._import_checked(consumer_con, consumer_col, tmp_path / "b",
                                      embed_fn=model_embed, strict=False,
                                      stop=None)

    swept = ledger.sweep_stale(consumer_con)      # what worker startup does
    assert swept
    doc = consumer_con.execute(
        "SELECT embed_done FROM documents WHERE uid='gutenberg:1'").fetchone()
    assert doc["embed_done"] == 0                  # _next_book will re-embed locally

    monkeypatch.setattr(embeddings, "_embed_texts", model_embed)
    retry = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert retry["ok"], retry.get("error")


# ────────────────── 8. consumer SIGTERM mid-import ──────────────────

def test_consumer_sigterm_mid_import_finishes_on_restart(two_nodes, tmp_path,
                                                          monkeypatch):
    """Stop mid-bundle: nothing corrupts, partial progress is visible as
    un-embedded documents, and the next drain completes the remainder."""
    import threading
    producer_con, producer_col, consumer_con, consumer_col, _ = two_nodes
    inbox = importer.inbox_dir(consumer_col)
    inbox.mkdir(parents=True, exist_ok=True)
    _export(producer_con, producer_col, tmp_path, list(DOCS))
    (tmp_path / "b").rename(inbox / "b")

    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(worker, "_stop", stop)
    assert worker._drain_bundle_inbox(consumer_con, consumer_col) == 0
    embedded = consumer_con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"]
    assert embedded == 0

    fresh = threading.Event()
    monkeypatch.setattr(worker, "_stop", fresh)
    assert worker._drain_bundle_inbox(consumer_con, consumer_col) == 1
    embedded = consumer_con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"]
    assert embedded == len(DOCS)
    ids = embeddings._conn(consumer_col).open_table(embeddings.TABLE) \
        .search().limit(1000).to_arrow().column("id").to_pylist()
    assert len(ids) == len(set(ids))
