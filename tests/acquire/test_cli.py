"""app/cli.py — corpus add|status|grow, at the argv level."""
from app import cli, collections as collections_mod
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                               RemoteFile, Transport)
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
    monkeypatch.setattr(cli.collections_mod, "load_all", lambda: [collection])
    monkeypatch.setitem(cli.ACQUIRE_REGISTRY, collection.plugin, spec)


def test_dry_run_prints_plan_and_never_writes(tmp_path, mock_server, monkeypatch, capsys):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    collection = _collection(tmp_path)
    _wire(monkeypatch, collection, _spec(mock_server))

    rc = cli.main(["corpus", "add", "test", "--profile", "demo", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "files:" in out and "bytes:" in out and "eta:" in out and "license:" in out
    assert not any(collection.corpus_dir.iterdir())


def test_add_fetches_and_status_reports(tmp_path, mock_server, monkeypatch, capsys):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    collection = _collection(tmp_path)
    _wire(monkeypatch, collection, _spec(mock_server))

    rc = cli.main(["corpus", "add", "test", "--profile", "demo"])
    assert rc == 0
    assert (collection.corpus_dir / "a.txt").is_file()

    capsys.readouterr()
    rc2 = cli.main(["corpus", "status", "test"])
    assert rc2 == 0
    out = capsys.readouterr().out
    assert "1 done / 1 tracked" in out


def test_license_ack_flow_prints_toml_line(tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text("terms text")
    collection = _collection(tmp_path)
    spec = AcquisitionSpec(
        source_id="test", transport=Transport.HTTPS, endpoints=(), layout="single-file",
        license_class=LicenseClass.YELLOW, terms_text_path=terms,
        profiles={"demo": Filter()}, estimated_bytes={"demo": 1},
    )
    _wire(monkeypatch, collection, spec)

    rc = cli.main(["corpus", "add", "test", "--ack-terms", str(terms)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "license_ack" in out
    assert "/" in out   # timestamp/sha256 format visible in the printed toml line


def test_status_never_lists_when_acquisition_disabled(tmp_path, monkeypatch, capsys):
    """A status check on a collection that hasn't opted into acquisition
    must never call list_files() (i.e. never touch the network) — this is
    the "existing hand-populated deployment sees zero behavior change"
    guarantee. Regression test: plan_acquisition() was previously called
    unconditionally, triggering a real listing call even when disabled."""
    collection = _collection(tmp_path, acquire_enabled=False)
    spec = AcquisitionSpec(
        source_id="test", transport=Transport.HTTPS, endpoints=(), layout="single-file",
        profiles={"demo": Filter()}, estimated_bytes={"demo": 1},
    )

    def _boom(*a, **kw):
        raise AssertionError("list_files/plan_acquisition must not run when acquire_enabled=False")
    monkeypatch.setattr(cli.engine, "plan_acquisition", _boom)
    _wire(monkeypatch, collection, spec)

    rc = cli.main(["corpus", "status", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "acquire_enabled: False" in out
    assert "disabled" in out


def test_grow_refused_without_license_ack(tmp_path, monkeypatch, capsys):
    terms = tmp_path / "terms.txt"
    terms.write_text("terms text")
    collection = _collection(tmp_path, license_ack=None)
    spec = AcquisitionSpec(
        source_id="test", transport=Transport.HTTPS, endpoints=(), layout="single-file",
        license_class=LicenseClass.YELLOW, terms_text_path=terms,
        profiles={"full": Filter()}, estimated_bytes={"full": 1},
    )
    _wire(monkeypatch, collection, spec)

    rc = cli.main(["corpus", "grow", "test", "--profile", "full"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "REFUSED" in err
