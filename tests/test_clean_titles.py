"""`tools/clean_titles.py` — the stored titles, and the catalog with them.

Parser 6 cleans what it writes next; these are the 4,648 titles already
in the manifest, which is what a reader is looking at.
"""
import pytest

from app import collections as collections_mod, db
from tools import clean_titles


def _collection(tmp_path):
    return collections_mod.Collection(
        name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus",
        manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lancedb",
    )


def _seed(con, rows):
    """rows: (native_id, title). Mirrors worker._rdf_one, catalog included."""
    for native_id, title in rows:
        uid = f"gutenberg:{native_id}"
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,"
            "subjects,bookshelves,rdf_done,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (uid, "gutenberg", str(native_id), f"/c/{native_id}", "en", title,
             "A. Author", "Fiction", "Shelf", 1234))
        con.execute(
            "INSERT OR REPLACE INTO catalog_fts"
            "(rowid,uid,title,authors,subjects,bookshelves) "
            "VALUES((SELECT rowid FROM documents WHERE uid=?),?,?,?,?,?)",
            (uid, uid, title, "A. Author", "Fiction", "Shelf"))


def test_it_cleans_the_title_and_the_catalog_together(tmp_path):
    con = db.connect(collection=_collection(tmp_path))
    _seed(con, [(1, "Ties mikä lie! : $b Nelinäytöksinen huvinäytelmä"),
                (2, "Pride and Prejudice")])

    done, seen = clean_titles.run(con)
    assert done == 1
    assert con.execute("SELECT title FROM documents WHERE native_id='1'"
                       ).fetchone()["title"] == "Ties mikä lie! : Nelinäytöksinen huvinäytelmä"

    # the catalog must move WITH it, or a search finds the old string and
    # shows the new one
    hit = con.execute("SELECT uid FROM catalog_fts WHERE catalog_fts MATCH "
                      "'Nelinäytöksinen'").fetchall()
    assert [r["uid"] for r in hit] == ["gutenberg:1"]
    stale = con.execute("SELECT rowid FROM catalog_fts WHERE title LIKE '%$b%'").fetchall()
    assert not stale, "no catalog row may keep the delimiter"

    # and the catalog row still answers on its OTHER columns — the REPLACE
    # rewrites the whole row, so authors/subjects must be carried over
    assert con.execute("SELECT uid FROM catalog_fts WHERE catalog_fts MATCH "
                       "'Author'").fetchall()


def test_a_dollar_amount_is_not_a_candidate_to_change(tmp_path):
    con = db.connect(collection=_collection(tmp_path))
    _seed(con, [(1, "The $30,000 Bequest, and Other Stories")])
    done, seen = clean_titles.run(con)
    assert seen == 1, "it IS examined — the probe is over-broad on purpose"
    assert done == 0, "and left alone, because $3 is money"
    assert con.execute("SELECT title FROM documents").fetchone()["title"] == \
        "The $30,000 Bequest, and Other Stories"


def test_it_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    con = db.connect(collection=_collection(tmp_path))
    _seed(con, [(1, "A : $b B")])

    clean_titles.run(con, dry_run=True)
    assert con.execute("SELECT title FROM documents").fetchone()["title"] == "A : $b B"

    assert clean_titles.run(con)[0] == 1
    assert clean_titles.run(con)[0] == 0, "a second run finds nothing"


def test_it_leaves_the_pipeline_signals_alone(tmp_path):
    """`updated_at` feeds genesis's ShardDemand.oldest, which orders which
    archive shard is fetched next. A title tidy has no business moving it,
    and no stage may be re-armed."""
    con = db.connect(collection=_collection(tmp_path))
    _seed(con, [(1, "A : $b B")])
    con.execute("UPDATE documents SET embed_done=1, fts_done=1")

    clean_titles.run(con)
    row = con.execute("SELECT updated_at, embed_done, fts_done, rdf_done "
                      "FROM documents").fetchone()
    assert row["updated_at"] == 1234
    assert (row["embed_done"], row["fts_done"], row["rdf_done"]) == (1, 1, 1)
