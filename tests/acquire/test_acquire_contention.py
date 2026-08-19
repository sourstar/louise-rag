"""A busy manifest defers a fetch; it does not fail it.

Live symptom after per-collection pipeline threads shipped: a discovery
pass streaming millions of rows starved the acquire thread past the busy
timeout, and "corpus fetch failed: database is locked" was logged as an
ERROR — a transient loss dressed up as a failure. Nothing had moved and
nothing was inconsistent: the claim was never taken.
"""
import sqlite3

from app import collections as collections_mod, db, worker
from app.acquire.specs import GUTENBERG_SPEC


def _collection(tmp_path):
    c = collections_mod.Collection(
        name="c", plugin="gutenberg", profile="demo", embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance",
        acquire_enabled=True)
    c.corpus_dir.mkdir(parents=True, exist_ok=True)
    return c


def test_locked_manifest_is_a_deferral_not_a_failure(isolated_config, tmp_path,
                                                      monkeypatch):
    collection = _collection(tmp_path)
    db.connect(collection).close()

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(worker.acquire_engine, "run_acquisition", locked)

    worker._acquire_worker(collection, GUTENBERG_SPEC, "demo", "test")

    con = db.connect(collection)
    rows = con.execute(
        "SELECT level, message FROM events WHERE stage='acquire' "
        "ORDER BY id DESC LIMIT 1").fetchall()
    assert rows, "the deferral must be reported, not swallowed"
    assert rows[0]["level"] == "warn", "a deferral is not an error"
    assert "deferred" in rows[0]["message"]
    assert "retrying" in rows[0]["message"]
    # and the collection is still queued for another attempt
    assert collection.name in worker._acquire_done
    worker._acquire_done.discard(collection.name)


def test_a_real_failure_is_still_an_error(isolated_config, tmp_path, monkeypatch):
    collection = _collection(tmp_path)
    db.connect(collection).close()

    def boom(*a, **kw):
        raise RuntimeError("mirror refused the connection")
    monkeypatch.setattr(worker.acquire_engine, "run_acquisition", boom)

    worker._acquire_worker(collection, GUTENBERG_SPEC, "demo", "test")

    con = db.connect(collection)
    row = con.execute(
        "SELECT level, message FROM events WHERE stage='acquire' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["level"] == "error" and "failed" in row["message"]
    worker._acquire_done.discard(collection.name)


def test_busy_timeout_gives_writers_room(isolated_config, tmp_path):
    con = db.connect(_collection(tmp_path))
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
