"""app/worker.py's _acquire() — gated by Collection.acquire_enabled, off by
default so an existing hand-populated deployment sees zero behavior change
and no surprise outbound network access on restart.
"""
from app import collections as collections_mod, db, worker
from app.acquire.spec import AcquisitionSpec, Filter, Transport


def _collection(tmp_path, acquire_enabled):
    d = tmp_path / "corpus"
    d.mkdir()
    return collections_mod.Collection(
        name="test", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=d, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        acquire_enabled=acquire_enabled, license_ack=None,
    )


def test_acquire_noop_when_disabled(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(worker.acquire_engine, "run_acquisition",
                         lambda *a, **kw: called.append(1))
    collection = _collection(tmp_path, acquire_enabled=False)
    con = db.connect(collection)
    worker._acquire(con, collection, "test")
    assert called == []


def test_acquire_runs_when_enabled_and_spec_registered(tmp_path, monkeypatch):
    called = []

    def fake_run(con, collection, spec, profile, stop=None):
        called.append((profile, stop))
        return {"fetched": 0, "skipped": 0, "failed": 0, "aborted": 0, "total": 0}
    monkeypatch.setattr(worker.acquire_engine, "run_acquisition", fake_run)

    spec = AcquisitionSpec(source_id="gutenberg", transport=Transport.HTTPS, endpoints=(),
                            layout="single-file", profiles={"demo": Filter()},
                            estimated_bytes={"demo": 1})
    monkeypatch.setitem(worker.ACQUIRE_REGISTRY, "gutenberg", spec)
    collection = _collection(tmp_path, acquire_enabled=True)
    con = db.connect(collection)
    worker._acquire(con, collection, "test")
    profile, stop = called[0]
    assert profile == "demo"    # defaults to "demo" when Collection.profile is unset
    # P0.7: the worker's stop event must reach acquisition, or a SIGTERM
    # during a long fetch is invisible until the whole run finishes.
    assert stop is worker._stop


def test_acquire_noop_when_no_spec_registered(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(worker.acquire_engine, "run_acquisition",
                         lambda *a, **kw: called.append(1))
    monkeypatch.delitem(worker.ACQUIRE_REGISTRY, "gutenberg", raising=False)
    collection = _collection(tmp_path, acquire_enabled=True)
    con = db.connect(collection)
    worker._acquire(con, collection, "test")
    assert called == []
