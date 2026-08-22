"""The browse-ordering expression index.

Every user-facing listing orders by CAST(native_id AS INTEGER) — /api/books,
the CLI listing, the bundle-export default. The CAST defeats every plain
index, so SQLite built a TEMP B-TREE over the whole filtered set to return
one page: ~10s per page view on a 33M-row manifest. An expression index on
exactly that expression makes pagination an index walk.

It is built by the WORKER at startup (db.ensure_browse_index), never by the
web process: the first build reads the whole table, and a page view must not
pay minutes for it.
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"

# the /api/books default view, verbatim shape
_BROWSE = ("SELECT CAST(native_id AS INTEGER) AS id, title FROM documents "
           "WHERE 1=1 ORDER BY CAST(native_id AS INTEGER) LIMIT ? OFFSET ?")


def _discovered(tmp_path, name="alpha"):
    corpus = tmp_path / f"corpus_{name}"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    c = collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / name / "manifest.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def _plan(con, sql, params):
    return " ".join(str(r[-1]) for r in con.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_build_is_idempotent(tmp_path):
    c, con = _discovered(tmp_path)
    assert db.ensure_browse_index(con) is True
    assert db.ensure_browse_index(con) is False


def test_browse_pagination_stops_sorting(tmp_path):
    c, con = _discovered(tmp_path)
    before = _plan(con, _BROWSE, (50, 0)).upper()
    assert "TEMP B-TREE" in before, f"fixture does not reproduce the cost: {before}"

    db.ensure_browse_index(con)
    after = _plan(con, _BROWSE, (50, 0)).upper()
    assert "TEMP B-TREE" not in after, f"index not picked up: {after}"
    assert "IDX_DOCUMENTS_NATIVE_INT" in after


def test_ordering_stays_numeric(tmp_path):
    """The index must serve the same order the CAST produced — 2 before 10,
    which is the whole reason the CAST exists instead of text ordering."""
    c, con = _discovered(tmp_path)
    db.ensure_browse_index(con)
    con.execute("DELETE FROM documents")
    for nid in ("10", "2", "100", "1"):
        con.execute(
            "INSERT INTO documents(uid, source, native_id, path) VALUES (?,?,?,?)",
            (f"gutenberg:{nid}", "gutenberg", nid, f"/x/{nid}"))
    con.commit()
    ids = [r["id"] for r in con.execute(_BROWSE, (10, 0))]
    assert ids == [1, 2, 10, 100], ids


def test_web_connect_does_not_build_it(tmp_path):
    """connect() alone must leave the index absent — the build belongs to the
    worker's startup, never to a web request."""
    c, con = _discovered(tmp_path)
    assert con.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                       "AND name='idx_documents_native_int'").fetchone() is None
