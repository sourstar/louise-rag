"""The reader finds its document by index, never by walking the manifest.

`reader.document` looked a row up with `CAST(native_id AS INTEGER)=? OR
native_id=?`. The unique index is (source, native_id), so `native_id=?`
alone cannot use it, and SQLite serves an OR from indexes only when both
halves have one: every click walked the whole table. Observed live
2026-09-15 on PubMed (40M rows): an Ask hit's abstract never loaded in the
panel, and the page answered after more than a minute.

Plan-checked, as CLAUDE.md requires of a hot-path query: a behavioural
test passes on a slow query just as well.
"""
import time

from app import collections as collections_mod, db, reader


def _collection(tmp_path):
    return collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "state" / "manifest.sqlite",
        fts_dir=tmp_path / "index", lance_dir=tmp_path / "lance")


def _doc(con, native_id: str, title: str):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,title,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (f"pubmed:{native_id}", "pubmed", native_id, "/nowhere.xml.gz::0", title,
         int(time.time())))
    con.commit()


def test_the_lookup_is_an_index_probe(tmp_path):
    con = db.connect(_collection(tmp_path))
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + reader.READER_ROW_SQL, ("pubmed", "17894069")))
    assert "USING INDEX idx_documents_native" in plan or \
        "USING COVERING INDEX idx_documents_native" in plan, plan
    assert "SCAN" not in plan, plan
    con.close()


def test_a_document_opens_by_its_id(tmp_path):
    c = _collection(tmp_path)
    con = db.connect(c)
    _doc(con, "17894069", "A study of the etiology of sociopathic behavior.")
    con.close()
    doc = reader.document(c, 17894069)
    assert doc is not None
    assert doc["header"]["title"] == "A study of the etiology of sociopathic behavior."
    assert reader.document(c, 999) is None


def test_another_spelling_is_found_only_where_its_index_exists(tmp_path):
    """An id stored as "0123" is reached by its integer only through the
    worker-built expression index — the fallback never scans."""
    c = _collection(tmp_path)
    con = db.connect(c)
    _doc(con, "0123", "Zero-padded")
    assert reader.document(c, 123) is None, "no integer index: no scan either"
    db.ensure_browse_index(con)
    con.close()
    doc = reader.document(c, 123)
    assert doc is not None and doc["header"]["title"] == "Zero-padded"
    con = db.connect(c)
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + reader.READER_ROW_INT_SQL, (123,)))
    assert "idx_documents_native_int" in plan and "SCAN" not in plan, plan
    con.close()
