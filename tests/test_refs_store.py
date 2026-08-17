"""app/refs/store.py: idempotent sync-by-primary-key, ref_version gating,
ensure_schema safe before any sync (graceful degeneration on an empty store).
"""
from pathlib import Path

from app import refs
from app.plugins.base import RefSyncState
from app.refs import store as refs_store

FIXTURE_REFS = Path(__file__).parent / "fixtures" / "refs-mini"


def test_ensure_schema_before_sync_returns_empty(tmp_path):
    con = refs.connect(tmp_path / "refs.db")
    schemas = [s.schema() for s in refs.REFS_REGISTRY.values()]
    for schema in schemas:
        refs.ensure_schema(con, schema)
    store = refs.RefStore(con, schemas)
    assert store.rows("hgnc_gene") == []
    assert store.get("hgnc_gene", "1101") is None


def test_sync_is_idempotent(tmp_path):
    con = refs.connect(tmp_path / "refs.db")
    state = RefSyncState(ref_root=FIXTURE_REFS)
    source = refs.REFS_REGISTRY["hgnc"]

    r1 = refs_store.sync_source(con, source, state)
    assert r1 == {"skipped": False, "rows": 4}
    assert con.execute("SELECT COUNT(*) c FROM hgnc_gene").fetchone()["c"] == 4

    r2 = refs_store.sync_source(con, source, state)
    assert r2 == {"skipped": True, "rows": 0}   # ref_version unchanged -> no-op
    assert con.execute("SELECT COUNT(*) c FROM hgnc_gene").fetchone()["c"] == 4  # no duplicates


def test_ref_version_bump_forces_resync(tmp_path, monkeypatch):
    con = refs.connect(tmp_path / "refs.db")
    state = RefSyncState(ref_root=FIXTURE_REFS)
    source = refs.REFS_REGISTRY["hgnc"]
    refs_store.sync_source(con, source, state)

    monkeypatch.setattr(source, "ref_version", source.ref_version + 1)
    r2 = refs_store.sync_source(con, source, state)
    assert r2 == {"skipped": False, "rows": 4}


def test_sync_all(tmp_path):
    con = refs.connect(tmp_path / "refs.db")
    results = refs_store.sync_all(con, FIXTURE_REFS)
    assert results["hgnc"]["rows"] == 4
    assert results["clinvar-mini"]["rows"] == 2
