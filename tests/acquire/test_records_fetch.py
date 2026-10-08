"""`louise hold records fetch` — was `corpus add|grow` (07 §7.6) — at the
argv level, and its stop: a signal exits 130 after the unit in flight
(07 §7.7, invariant 2), and a stop in the listing says so.

A stop in the listing — gutenberg's rsync listing is ~12 minutes, the
likeliest moment for a Ctrl-C — ends the pass before any transfer, and the
result's counts are all zero. Printed bare, "total=0" read as an origin
with no files (review, 2026-09-13).
"""
from app import collections as collections_mod, db, louise
from app.acquire import engine
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                               RemoteFile, Transport)
from app.acquire.specs import ACQUIRE_REGISTRY
from app.hold import records_fetch
from app.plugins.base import LicenseClass


def _collection(tmp_path, **overrides):
    defaults = dict(
        name="test", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        acquire_enabled=True, license_ack=None,
    )
    defaults.update(overrides)
    defaults["corpus_dir"].mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(**defaults)


def _spec(mock_server, **overrides) -> AcquisitionSpec:
    static_files = tuple(
        RemoteFile(rel_path=p.name, size=p.stat().st_size)
        for p in sorted(mock_server.root.iterdir()) if p.is_file())
    defaults = dict(
        source_id="test", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url=mock_server.base_url,
                             static_files=static_files),),
        layout="single-file", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"demo": Filter()}, estimated_bytes={"demo": 10_000_000},
        license_class=LicenseClass.GREEN,
    )
    defaults.update(overrides)
    return AcquisitionSpec(**defaults)


def _wire(monkeypatch, collection, spec):
    def load_all(*, skip_unreadable=False):
        return ([collection], []) if skip_unreadable else [collection]
    monkeypatch.setattr(collections_mod, "load_all", load_all)
    monkeypatch.setitem(ACQUIRE_REGISTRY, collection.plugin, spec)
    # louise.main routes nothing here; the command binds its own stop
    monkeypatch.setattr(records_fetch.signal, "signal", lambda *a: None)


def test_dry_run_prints_plan_and_never_writes(tmp_path, mock_server, monkeypatch, capsys):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    collection = _collection(tmp_path)
    _wire(monkeypatch, collection, _spec(mock_server))

    rc = louise.main(["hold", "records", "fetch", "test", "--profile", "demo", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "files:" in out and "bytes:" in out and "eta:" in out and "license:" in out
    assert not any(collection.corpus_dir.iterdir())


def test_fetch_fetches_and_records_the_file(tmp_path, mock_server, monkeypatch, capsys):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    collection = _collection(tmp_path)
    _wire(monkeypatch, collection, _spec(mock_server))

    assert louise.main(["hold", "records", "fetch", "test", "--profile", "demo"]) == 0
    assert (collection.corpus_dir / "a.txt").is_file()
    con = db.connect(collection)
    row = con.execute("SELECT COUNT(*) total, SUM(fetch_done) done FROM fetch_files "
                      "WHERE source_id='test'").fetchone()
    assert (row["done"], row["total"]) == (1, 1)


def test_the_profile_defaults_to_the_collection_files(tmp_path, monkeypatch):
    collection = _collection(tmp_path, profile="full")
    seen = []
    monkeypatch.setattr(records_fetch, "fetch",
                        lambda c, con, profile, dry_run=False: seen.append(profile) or 0)
    _wire(monkeypatch, collection, object())
    assert louise.main(["hold", "records", "fetch", "test"]) == 0
    assert louise.main(["hold", "records", "fetch", "test", "--profile", "demo"]) == 0
    assert seen == ["full", "demo"]
    assert louise.main(["hold", "records", "fetch", "nope"]) == 2


def test_fetch_refused_without_license_ack(tmp_path, monkeypatch, capsys):
    """Invariant 9 at acquisition: before the first byte moves. A refusal
    is 07 §7.7's exit 2 (corpus grow said 1)."""
    terms = tmp_path / "terms.txt"
    terms.write_text("terms text")
    collection = _collection(tmp_path, license_ack=None)
    spec = AcquisitionSpec(
        source_id="test", transport=Transport.HTTPS, endpoints=(), layout="single-file",
        license_class=LicenseClass.YELLOW, terms_text_path=terms,
        profiles={"full": Filter()}, estimated_bytes={"full": 1},
    )
    _wire(monkeypatch, collection, spec)

    assert louise.main(["hold", "records", "fetch", "test", "--profile", "full"]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert not any(collection.corpus_dir.iterdir())


# ---- the stop ---------------------------------------------------------------

_ZERO = {"fetched": 0, "skipped": 0, "failed": 0, "aborted": 0, "adopted": 0, "total": 0}


def _run(monkeypatch, tmp_path, result: dict, signalled: bool) -> int:
    collection = _collection(tmp_path)
    _wire(monkeypatch, collection, object())

    def fake_run(con, collection, spec, profile, stop=None):
        assert stop is records_fetch._stop, "the stop is threaded into the run"
        if signalled:
            records_fetch._stop.set()
        return result

    monkeypatch.setattr(engine, "run_acquisition", fake_run)
    try:
        return louise.main(["hold", "records", "fetch", "test", "--profile", "demo"])
    finally:
        records_fetch._stop.clear()


def test_a_stop_in_the_listing_says_so_and_exits_130(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, tmp_path,
              {**_ZERO, "stopped": "rsync --list-only aborted by stop event"}, signalled=True)
    out = capsys.readouterr().out
    assert rc == 130
    assert "interrupted before any transfer" in out and "nothing was recorded" in out, out


def test_a_stop_mid_transfer_still_exits_130(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, tmp_path, {**_ZERO, "fetched": 3, "aborted": 2, "total": 5},
              signalled=True)
    assert rc == 130
    assert "partial downloads were checkpointed" in capsys.readouterr().out


def test_an_unstopped_run_keeps_its_exit_codes(tmp_path, monkeypatch):
    assert _run(monkeypatch, tmp_path, {**_ZERO, "fetched": 1, "total": 1}, signalled=False) == 0
    assert _run(monkeypatch, tmp_path, {**_ZERO, "failed": 1, "total": 1}, signalled=False) == 1
