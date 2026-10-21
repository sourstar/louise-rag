"""Below the disk floor nothing writes vectors, and the refusal is said
(app/disk_guard.py; operator, 2026-10-18)."""
import shutil
from collections import namedtuple

import pytest

from app import collections as collections_mod, config, db, disk_guard, health, worker

GIB = 2 ** 30
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def collection(isolated_config, tmp_path, monkeypatch):
    disk_guard._signalled.clear()
    # the floor these tests measure against is the DEFAULT one, set here: a
    # run with VECTORS_MIN_FREE_GB=0 (a small container's disk) left nothing
    # below it and four tests failed on the environment (Perugia's gate,
    # 2026-10-21)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_GB", 32.0)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_FRACTION", 0.05)
    return collections_mod.Collection(
        name="dg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")


def _disk(monkeypatch, free_gib: float, total_gib: float = 1000):
    monkeypatch.setattr(disk_guard.shutil, "disk_usage",
                        lambda p: Usage(int(total_gib * GIB), 0, int(free_gib * GIB)))


def test_the_floor_is_the_larger_of_the_absolute_and_the_share(monkeypatch):
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_GB", 32.0)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_FRACTION", 0.05)
    assert disk_guard.floor_bytes(100 * GIB) == 32 * GIB
    assert disk_guard.floor_bytes(2000 * GIB) == 100 * GIB


def test_a_table_not_made_yet_is_judged_by_the_directory_it_will_be_made_in(collection, monkeypatch):
    seen = []
    monkeypatch.setattr(disk_guard.shutil, "disk_usage",
                        lambda p: (seen.append(p), Usage(1000 * GIB, 0, 900 * GIB))[1])
    assert not (collection.lance_dir).exists()
    assert disk_guard.vectors_short(collection) is None
    assert seen and seen[0].exists()


def test_below_the_floor_every_vector_writer_defers_and_it_is_said_once(collection, monkeypatch):
    _disk(monkeypatch, free_gib=20)
    con = db.connect(collection)
    try:
        assert worker._vector_writes_allowed(collection, con) is False
        assert worker._vector_writes_allowed(collection, con) is False
        events = con.execute("SELECT level, stage, message FROM events WHERE stage='disk'").fetchall()
    finally:
        con.close()
    assert len(events) == 1, "said, and not every round"
    assert events[0]["level"] == "warn" and "20.0 GiB free" in events[0]["message"]
    _disk(monkeypatch, free_gib=900)
    assert worker._vector_writes_allowed(collection) is True


def test_a_gauge_that_cannot_be_read_never_stops_the_work(collection, monkeypatch):
    def broken(p):
        raise OSError("no such device")
    monkeypatch.setattr(disk_guard.shutil, "disk_usage", broken)
    assert disk_guard.vectors_short(collection) is None
    assert worker._vector_writes_allowed(collection) is True


def test_a_bundle_import_waits_and_nothing_is_held_against_it(collection, monkeypatch, tmp_path):
    from app.bundles import importer, ledger
    _disk(monkeypatch, free_gib=1)
    monkeypatch.setattr(importer, "_import_checked",
                        lambda *a, **k: pytest.fail("a short disk imports nothing"))
    monkeypatch.setattr(ledger, "record_rejected",
                        lambda *a, **k: pytest.fail("the disk's, not the bundle's"))
    con = db.connect(collection)
    try:
        got = importer.import_bundle(con, collection, tmp_path / "b")
    finally:
        con.close()
    assert got["ok"] is False and got["retryable"] is True and got["error_class"] == "LowDisk"


def test_the_health_report_blocks_embedding_with_the_reason(collection, monkeypatch):
    con = db.connect(collection)
    try:
        con.execute("INSERT INTO documents(uid, source, native_id, path, in_scope, rdf_done, fts_done, "
                    "embed_done, updated_at) VALUES('gutenberg:1','gutenberg','1','x',1,1,1,0,0)")
        con.commit()
        db.set_scope(con, {**db.get_scope(con), "embed_mode": "all"})
        _disk(monkeypatch, free_gib=5)
        stage = {s.name: s for s in health.inspect(con, collection).stages}["embed"]
    finally:
        con.close()
    assert stage.state == health.BLOCKED and "short of space" in stage.reason


def test_node_show_lists_each_filesystem_under_the_vectors(collection, monkeypatch, tmp_path):
    _disk(monkeypatch, free_gib=10, total_gib=100)
    other = collections_mod.Collection(
        name="dg2", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "c2", manifest_path=tmp_path / "m2.db",
        fts_dir=tmp_path / "f2", lance_dir=tmp_path / "l2")
    rows = disk_guard.status([collection, other])
    assert len(rows) == 1, "one filesystem, both collections on it"
    assert rows[0]["collections"] == ["dg", "dg2"] and rows[0]["short"] is True
    assert rows[0]["free_gib"] == 10.0 and rows[0]["floor_gib"] == 32.0
    assert shutil  # the module the guard reads through
