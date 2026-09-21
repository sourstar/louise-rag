"""P0.6 acceptance: the Library page's grow-profile dry-run action calls the
exact same app.acquire.engine.plan_acquisition function app.cli's
`corpus add|grow --dry-run` calls — proven structurally (identical
AcquisitionPlan dataclass, and the CLI's own printed numbers), not by a
mock-call-count trick.
"""
from dataclasses import asdict

from app import cli, collections as collections_mod, web
from app.acquire import engine
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                               RemoteFile, Transport)
from app.plugins.base import LicenseClass


def _only(collection):
    """A stand-in for `collections.load_all` with its real signature:
    `by_name` asks with `skip_unreadable=True` (one file's fault is not
    another's), which returns `(served, unreadable)`."""
    def load_all(*, skip_unreadable=False):
        return ([collection], []) if skip_unreadable else [collection]
    return load_all


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


def test_web_grow_plan_matches_engine_plan_acquisition_directly(tmp_path, mock_server, monkeypatch):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    monkeypatch.setattr(web.collections_mod, "load_all", _only(collection))
    monkeypatch.setitem(web.ACQUIRE_REGISTRY, collection.plugin, spec)

    from app import db
    direct_plan = engine.plan_acquisition(db.connect(collection), collection, spec, "demo")

    result = web.grow_plan(collection.name, web.GrowBody(profile="demo"))

    assert result["plan"] == asdict(direct_plan)
    assert result["cli"] == "python -m app.cli corpus grow test --profile demo --dry-run"


def test_cli_and_web_grow_plan_agree(tmp_path, mock_server, monkeypatch, capsys):
    (mock_server.root / "a.txt").write_bytes(b"x" * 100)
    (mock_server.root / "b.txt").write_bytes(b"y" * 250)
    collection = _collection(tmp_path)
    spec = _spec(mock_server)
    monkeypatch.setattr(web.collections_mod, "load_all", _only(collection))
    monkeypatch.setitem(web.ACQUIRE_REGISTRY, collection.plugin, spec)
    monkeypatch.setattr(cli.collections_mod, "load_all", _only(collection))
    monkeypatch.setitem(cli.ACQUIRE_REGISTRY, collection.plugin, spec)

    rc = cli.main(["corpus", "grow", "test", "--profile", "demo", "--dry-run"])
    assert rc == 0
    cli_out = capsys.readouterr().out

    result = web.grow_plan(collection.name, web.GrowBody(profile="demo"))
    plan = result["plan"]
    assert f"{plan['files_pending']} pending / {plan['files_total']} total" in cli_out
    assert f"{plan['bytes_pending']} pending / {plan['bytes_total']} total" in cli_out
