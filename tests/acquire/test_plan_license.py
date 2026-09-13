"""A dry run lists nothing for a collection whose terms are not acknowledged.

`plan_acquisition` LISTED the origin over the network — and for a peer
source resolved an index torrent from the swarm — before any license
check, contradicting the engine's own promise of a gate "before any
listing or transport call" (audit, 2026-09-13). It never raises: the plan
reports `license_state='missing'` and lists nothing, and the CLI prints
"BLOCKS acquisition".
"""
from app import collections as collections_mod, db, licensing
from app.acquire import engine
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint,
                              Filter, Transport)
from app.plugins.base import LicenseClass


def _spec(terms, cls):
    return AcquisitionSpec(
        source_id="g", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="https://origin.invalid"),),
        layout="single-file", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1}, license_class=cls, terms_text_path=terms)


def test_a_dry_run_lists_nothing_without_the_acknowledgement(isolated_config, tmp_path,
                                                              monkeypatch):
    terms = tmp_path / "TERMS.txt"
    terms.write_bytes(b"terms\n")
    c = collections_mod.Collection(
        name="y", plugin="gutenberg", profile="demo", embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")
    listed = []
    monkeypatch.setattr(engine, "_list_and_filter",
                        lambda *a, **k: listed.append(1) or [])

    plan = engine.plan_acquisition(db.connect(c), c, _spec(terms, LicenseClass.YELLOW), "demo")
    assert plan.license_state == licensing.MISSING
    assert plan.files_total == 0 and listed == [], "nothing listed before the gate"

    plan = engine.plan_acquisition(db.connect(c), c, _spec(terms, LicenseClass.GREEN), "demo")
    assert plan.license_state == licensing.NOT_REQUIRED
    assert listed == [1], "control: a green source is listed"
