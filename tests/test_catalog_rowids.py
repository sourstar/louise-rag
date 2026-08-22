"""catalog_fts rows are keyed on their document's rowid.

catalog_fts declares `uid UNINDEXED`, so the old `DELETE FROM catalog_fts
WHERE uid=?` scanned the entire shadow table on every rdf step — measured
live at 14.02 of rdf's 14.25 ms/doc at only 36k catalog rows, growing
linearly with the catalog. Total extraction cost for a corpus was O(N²):
pubmed would never have finished.

Now each catalog row carries its document's rowid and the write is one
INSERT OR REPLACE — a b-tree probe, size-independent. ensure_catalog_rowids
realigns catalogs written under the old scheme, whose arbitrary rowids
REPLACE would never hit (silent duplicates otherwise).
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery, retrieval, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


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


def _extract_all(con, c):
    worker._execute_step(con, c, "rdf", con.execute(
        "SELECT * FROM documents WHERE rdf_done=0 LIMIT 1").fetchone())


def test_catalog_rows_share_their_documents_rowid(tmp_path):
    c, con = _discovered(tmp_path)
    _extract_all(con, c)
    mismatched = con.execute(
        "SELECT COUNT(*) FROM catalog_fts f WHERE NOT EXISTS ("
        " SELECT 1 FROM documents d WHERE d.rowid=f.rowid AND d.uid=f.uid)"
    ).fetchone()[0]
    assert mismatched == 0


def test_rerun_replaces_instead_of_duplicating(tmp_path):
    c, con = _discovered(tmp_path)
    _extract_all(con, c)
    total = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]

    con.execute("UPDATE documents SET rdf_done=0")
    con.commit()
    _extract_all(con, c)
    again = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]
    assert again == total, f"re-extraction duplicated catalog rows: {total} -> {again}"


def test_realignment_fixes_an_old_style_catalog(tmp_path):
    c, con = _discovered(tmp_path)
    _extract_all(con, c)
    docs = con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=1").fetchone()[0]

    # forge the pre-fix state: arbitrary rowids, marker absent
    con.execute("BEGIN")
    con.execute("DELETE FROM catalog_fts")
    for i, r in enumerate(con.execute(
            "SELECT rowid, uid, title, authors, subjects, bookshelves "
            "FROM documents WHERE rdf_done=1").fetchall()):
        con.execute("INSERT INTO catalog_fts(rowid,uid,title,authors,subjects,"
                    "bookshelves) VALUES(?,?,?,?,?,?)",
                    (10_000 + i, r["uid"], r["title"], r["authors"],
                     r["subjects"], r["bookshelves"]))
    con.execute("DELETE FROM control WHERE key=?", (db.CATALOG_ALIGNED_KEY,))
    con.execute("COMMIT")

    assert db.ensure_catalog_rowids(con) is True
    assert db.ensure_catalog_rowids(con) is False       # marker set, one-time

    rows = con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0]
    assert rows == docs
    mismatched = con.execute(
        "SELECT COUNT(*) FROM catalog_fts f WHERE NOT EXISTS ("
        " SELECT 1 FROM documents d WHERE d.rowid=f.rowid AND d.uid=f.uid)"
    ).fetchone()[0]
    assert mismatched == 0

    # and a subsequent re-extraction replaces rather than duplicates
    con.execute("UPDATE documents SET rdf_done=0")
    con.commit()
    _extract_all(con, c)
    assert con.execute("SELECT COUNT(*) FROM catalog_fts").fetchone()[0] == docs


def test_catalog_search_still_matches_after_the_change(tmp_path):
    """The reader (retrieval joins d.uid = f.uid) must be oblivious."""
    c, con = _discovered(tmp_path)
    _extract_all(con, c)
    title = con.execute("SELECT title FROM documents WHERE rdf_done=1 "
                        "AND title IS NOT NULL LIMIT 1").fetchone()["title"]
    word = max(title.split(), key=len).strip('",.;:')
    hit = con.execute(
        "SELECT f.uid FROM catalog_fts f JOIN documents d ON d.uid=f.uid "
        "WHERE catalog_fts MATCH ? LIMIT 1", (f'"{word}"',)).fetchone()
    assert hit is not None, f"catalog search lost {word!r}"
