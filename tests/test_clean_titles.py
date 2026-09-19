"""`tools/clean_titles.py` — the stored titles, and the catalog with them.

Parser 6 cleans what it writes next; these are the 4,648 titles already
in the manifest, which is what a reader is looking at. The catalog is the
index of app/catalog.py since 2026-09-19: the tool writes it first, as the
rdf stage does, and the index has ONE writer — the worker's.
"""
import pytest

from app import catalog, collections as collections_mod, db
from tools import clean_titles


def _collection(tmp_path):
    return collections_mod.Collection(
        name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus",
        manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lancedb",
    )


def _seeded(tmp_path, rows):
    """rows: (native_id, title) — extracted documents, and the catalog index
    the worker's backfill builds from them."""
    c = _collection(tmp_path)
    con = db.connect(collection=c)
    for native_id, title in rows:
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,"
            "subjects,bookshelves,rdf_done,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (f"gutenberg:{native_id}", "gutenberg", str(native_id), f"/c/{native_id}",
             "en", title, "A. Author", "Fiction", "Shelf", 1234))
    catalog.backfill(con, c, budget_s=60)
    return c, con


def _ids(hits):
    return [h["book_id"] for h in hits]


def test_it_cleans_the_title_and_the_catalog_together(tmp_path):
    c, con = _seeded(tmp_path, [(1, "Ties mikä lie! : $b Nelinäytöksinen huvinäytelmä"),
                                (2, "Pride and Prejudice")])
    assert _ids(catalog.search(con, c, "b", 10)) == [1], "the delimiter is searchable"

    done, seen = clean_titles.run(con, c)
    assert done == 1
    assert con.execute("SELECT title FROM documents WHERE native_id='1'"
                       ).fetchone()["title"] == "Ties mikä lie! : Nelinäytöksinen huvinäytelmä"

    # the catalog must move WITH it, or a search finds the old string and
    # shows the new one
    assert _ids(catalog.search(con, c, "Nelinäytöksinen", 10)) == [1]
    assert catalog.search(con, c, "b", 10) == [], "no catalog entry may keep the delimiter"
    # and the entry still answers on its OTHER fields: it is rewritten whole
    assert sorted(_ids(catalog.search(con, c, "Author", 10))) == [1, 2]


def test_it_changes_nothing_while_the_worker_holds_the_catalog(tmp_path, monkeypatch):
    c, con = _seeded(tmp_path, [(1, "A : $b B")])

    def busy(collection, rows):
        raise catalog.CatalogUnavailable("catalog index: LockBusy")
    monkeypatch.setattr(catalog, "write", busy)
    with pytest.raises(SystemExit, match="stop the worker"):
        clean_titles.run(con, c)
    assert con.execute("SELECT title FROM documents").fetchone()["title"] == "A : $b B"


def test_a_dollar_amount_is_not_a_candidate_to_change(tmp_path):
    c, con = _seeded(tmp_path, [(1, "The $30,000 Bequest, and Other Stories")])
    done, seen = clean_titles.run(con, c)
    assert seen == 1, "it IS examined — the probe is over-broad on purpose"
    assert done == 0, "and left alone, because $3 is money"
    assert con.execute("SELECT title FROM documents").fetchone()["title"] == \
        "The $30,000 Bequest, and Other Stories"


def test_it_is_idempotent_and_dry_run_writes_nothing(tmp_path):
    c, con = _seeded(tmp_path, [(1, "A : $b B")])

    clean_titles.run(con, c, dry_run=True)
    assert con.execute("SELECT title FROM documents").fetchone()["title"] == "A : $b B"

    assert clean_titles.run(con, c)[0] == 1
    assert clean_titles.run(con, c)[0] == 0, "a second run finds nothing"


def test_it_leaves_the_pipeline_signals_alone(tmp_path):
    """`updated_at` feeds genesis's ShardDemand.oldest, which orders which
    archive shard is fetched next. A title tidy has no business moving it,
    and no stage may be re-armed."""
    c, con = _seeded(tmp_path, [(1, "A : $b B")])
    con.execute("UPDATE documents SET embed_done=1, fts_done=1")

    clean_titles.run(con, c)
    row = con.execute("SELECT updated_at, embed_done, fts_done, rdf_done "
                      "FROM documents").fetchone()
    assert row["updated_at"] == 1234
    assert (row["embed_done"], row["fts_done"], row["rdf_done"]) == (1, 1, 1)
