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


def test_the_store_is_opened_once_per_thread_not_once_per_batch(tmp_path, monkeypatch):
    """A COST test. `get_store()` opened a connection and ran every
    registered source's `ensure_schema` on EVERY fts batch — inside the
    manifest's BEGIN IMMEDIATE, where every other writer of that manifest
    queues behind it. Measured on the node, 2026-09-22: 4.5 s of fixed cost
    per fts batch, of which this was a part.

    Kept per THREAD, because a SQLite connection may not cross one, and
    keyed on the database's PATH, because a test's refs.db comes and goes
    with its DATA_DIR while the thread stays."""
    import threading

    from app import config

    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "a" / "refs.db")
    opened = []
    real = refs_store.connect
    monkeypatch.setattr(refs, "connect", lambda p=None: opened.append(p) or real(p))

    first, second = refs.get_store(), refs.get_store()
    assert first is second, "the same store, not a new connection per call"
    assert len(opened) == 1, f"opened {len(opened)} times"

    # another database is another store, even on the same thread
    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "b" / "refs.db")
    assert refs.get_store() is not first and len(opened) == 2

    # and another thread gets its own, because the connection cannot travel
    seen = []
    t = threading.Thread(target=lambda: seen.append(refs.get_store()))
    t.start(); t.join()
    assert seen and seen[0] is not first and len(opened) == 3


def test_refs_db_follows_the_data_dir_at_call_time(tmp_path, monkeypatch):
    """The default refs.db is beside DATA_DIR as it is WHEN asked, never as
    it was at import: three tests that moved DATA_DIR still created the real
    /data/refs.db (2026-10-08). An explicit REFS_DB_PATH still wins."""
    from app import config
    monkeypatch.setattr(config, "REFS_DB_PATH", None)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assert config.refs_db_path() == tmp_path / "data" / "refs.db"
    refs.get_store()
    assert (tmp_path / "data" / "refs.db").is_file()
    monkeypatch.setattr(config, "REFS_DB_PATH", tmp_path / "explicit.db")
    assert config.refs_db_path() == tmp_path / "explicit.db"


def test_a_table_whose_shape_moved_is_recreated_only_while_empty(tmp_path):
    """`get_store()` creates every registered table at the first open, so a
    source whose schema changed before any sync leaves an EMPTY table of the
    old shape behind (wikidata-refs, 2026-10-20: `key` → `id` as the primary
    key). Empty, it is re-created; holding rows, it is refused by name."""
    from app.plugins.base import RefColumn, RefSchema, RefTable
    con = refs.connect(tmp_path / "refs.db")
    con.execute("CREATE TABLE t_moved (id TEXT, lang TEXT, label TEXT, key TEXT PRIMARY KEY)")
    schema = RefSchema(source_id="moved", tables=(RefTable(
        name="t_moved", columns=(RefColumn("id", "TEXT"), RefColumn("label", "TEXT")), primary_key="id"),))
    refs_store.ensure_schema(con, schema)
    assert [r[1] for r in con.execute("PRAGMA table_info('t_moved')")] == ["id", "label"]
    con.execute("INSERT INTO t_moved (id, label) VALUES ('a', 'b')")
    other = RefSchema(source_id="moved", tables=(RefTable(
        name="t_moved", columns=(RefColumn("id", "TEXT"), RefColumn("kind", "TEXT")), primary_key="id"),))
    import pytest
    with pytest.raises(refs_store.RefSchemaError, match="t_moved.*1 row"):
        refs_store.ensure_schema(con, other)
    assert con.execute("SELECT count(*) FROM t_moved").fetchone()[0] == 1, "nothing dropped"


def test_a_sync_cut_off_keeps_its_batches_and_is_not_recorded_as_done(tmp_path):
    """One transaction per batch: a 10⁸-row sync (wikidata) that dies keeps
    what it wrote — every row an upsert by key, so the next sync writes the
    same rows again — and ref_meta, written last, does not say it finished,
    so the next sync is not skipped."""
    from app.plugins.base import RefColumn, RefRecordBatch, RefSchema, RefSyncState as S, RefTable

    class Dies:
        source_id, ref_version = "dies", 1

        def schema(self):
            return RefSchema(source_id="dies", tables=(RefTable(
                name="t_dies", columns=(RefColumn("id", "TEXT"), RefColumn("v", "TEXT")), primary_key="id"),))

        def sync(self, state):
            yield RefRecordBatch(table="t_dies", rows=({"id": "1", "v": "a"}, {"id": "2", "v": "b"}))
            raise OSError("the disk went away")

    import pytest
    con = refs.connect(tmp_path / "refs.db")
    with pytest.raises(OSError):
        refs_store.sync_source(con, Dies(), S(ref_root=tmp_path))
    assert con.execute("SELECT count(*) FROM t_dies").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM ref_meta WHERE source_id='dies'").fetchone()[0] == 0

    class Whole(Dies):
        def sync(self, state):
            yield RefRecordBatch(table="t_dies", rows=({"id": "1", "v": "a"}, {"id": "2", "v": "b"}, {"id": "3", "v": "c"}))
    assert refs_store.sync_source(con, Whole(), S(ref_root=tmp_path)) == {"skipped": False, "rows": 3}
    assert con.execute("SELECT count(*) FROM t_dies").fetchone()[0] == 3
    assert refs_store.sync_source(con, Whole(), S(ref_root=tmp_path))["skipped"] is True
