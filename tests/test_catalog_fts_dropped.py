"""The SQLite FTS5 catalog is retired (2026-09-19): app/catalog.py's index
is the catalog. A new manifest has no `catalog_fts`, an old manifest's is
dropped once at the worker's startup (db.drop_catalog_fts), and nothing
writes or reads it any more. It held 15.7 GiB in pubmed's manifest and
ranked every match: 57.3 s for "cancer"."""
import inspect

from app import catalog, collections as collections_mod, db, discovery, retrieval, worker


def _catalog_tables(con):
    return con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'catalog_fts%'").fetchall()


def test_a_new_manifest_has_no_sqlite_catalog(isolated_config):
    con = db.connect(collections_mod.default_collection())
    assert _catalog_tables(con) == []


def test_an_old_manifest_drops_it_once_and_keeps_its_documents(isolated_config):
    con = db.connect(collections_mod.default_collection())
    con.execute("CREATE VIRTUAL TABLE catalog_fts USING fts5("
                "uid UNINDEXED, title, authors, subjects, bookshelves)")
    con.execute("INSERT INTO documents(uid,source,native_id,path,title,updated_at) "
                "VALUES('gutenberg:1','gutenberg','1','/f/1','Moby Dick',0)")
    con.execute("INSERT INTO catalog_fts(uid,title) VALUES('gutenberg:1','Moby Dick')")
    db.set_control(con, "catalog_fts_rowid_aligned", "1")
    secure = con.execute("PRAGMA secure_delete").fetchone()[0]
    assert db.drop_catalog_fts(con) is True
    # freed pages are not zeroed through the WAL (a 14.6 GB WAL on pubmed's
    # clone), and the connection gets its own setting back
    assert con.execute("PRAGMA secure_delete").fetchone()[0] == secure
    assert _catalog_tables(con) == [], "the shadow tables go with it"
    assert db.get_control(con, "catalog_fts_rowid_aligned") is None
    assert con.execute("SELECT title FROM documents").fetchone()[0] == "Moby Dick"
    assert db.drop_catalog_fts(con) is False, "once"


def test_the_worker_drops_it_at_startup_and_no_longer_realigns_it():
    source = inspect.getsource(worker.run_forever)
    assert "db.drop_catalog_fts(cons[c.name])" in source
    assert not hasattr(db, "ensure_catalog_rowids")


def test_the_rdf_stage_the_backfill_and_search_never_touch_it(isolated_config):
    c = collections_mod.default_collection()
    con = db.connect(c)
    discovery.sync_collection(con, c)
    seen: list[str] = []
    con.set_trace_callback(seen.append)
    worker._execute_step(con, c, "rdf", con.execute(
        "SELECT * FROM documents WHERE rdf_done=0 LIMIT 1").fetchone())
    catalog.backfill(con, c, budget_s=60)
    title = con.execute("SELECT title FROM documents WHERE rdf_done=1 AND title IS NOT NULL "
                        "LIMIT 1").fetchone()[0]
    word = max((w for w in title.split() if w.isalnum()), key=len)
    hits = retrieval.catalog_search(con, word, collection=c)
    con.set_trace_callback(None)
    assert hits, f"the catalog lost {word!r}"
    assert not [s for s in seen if "catalog_fts" in s]
