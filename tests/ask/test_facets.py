"""tests/ask/test_facets.py — `ask facets` (05 §5.7): the fields a Codex
declares, the values this peer holds with its counts — flat, tree with
every prefix path, a subtree under a value — cached per snapshot under
index/, a field the manifest does not carry said so, a collection too
large counted by the maintainer and not on request."""
import sqlite3

import pytest

from app.ask import facets
from app.hold.codex import Facet
from tests.hold.conftest import pubmed


def manifest(tmp_path):
    con = sqlite3.connect(tmp_path / "manifest.sqlite")
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, native_id TEXT NOT NULL, lang TEXT, authors TEXT, "
                "subjects TEXT, in_scope INTEGER DEFAULT 1)")
    rows = [("p:1", "1", "en", "Smith, A; Doe, B", "Neoplasms/genetics|Humans"),
            ("p:2", "2", "en", "Doe, B", "Neoplasms/therapy|Humans"),
            ("p:3", "3", "de", None, "Neoplasms/genetics"),
            ("p:4", "4", "en", "Smith, A", None),
            ("p:5", "5", "fr", "Out, O", "Gone")]
    con.executemany("INSERT INTO documents(uid, native_id, lang, authors, subjects) VALUES(?,?,?,?,?)", rows)
    con.execute("UPDATE documents SET in_scope=0 WHERE uid='p:5'")
    con.commit()
    return con


def a_codex():
    return pubmed(facets=(Facet("subjects", "tree", "/", {"name": "MeSH"}), Facet("lang", "flat"),
                          Facet("authors", "flat"), Facet("date", "range")))


def test_values_are_counted_from_the_manifest_flat_tree_and_under(tmp_path):
    con, codex = manifest(tmp_path), a_codex()
    got = facets.count(con, codex, "lang")
    assert got["documents"] == 4 and got["values"] == {"en": 3, "de": 1}, "out-of-scope rows do not count"
    tree = facets.count(con, codex, "subjects")
    assert tree["values"] == {"Neoplasms": 3, "Neoplasms/genetics": 2, "Neoplasms/therapy": 1, "Humans": 2}, \
        "a tree counts every prefix path"
    assert facets.count(con, codex, "authors")["values"] == {"Smith, A": 2, "Doe, B": 2}
    idx = tmp_path / "index"
    out = facets.values(con, codex, idx, "subjects", snapshot_id="2026-08", under="Neoplasms")
    assert list(out["values"]) == ["Neoplasms", "Neoplasms/genetics", "Neoplasms/therapy"], \
        "the subtree, its own count first, then by count"
    assert out["under"] == "Neoplasms" and out["authority"] == {"name": "MeSH"} and "this peer's" in out["note"]
    assert (idx / "facets" / "subjects.json").is_file(), "cached for the snapshot"
    listing = facets.fields(con, codex, idx, snapshot_id="2026-08")
    by_field = {f["field"]: f for f in listing["fields"]}
    assert by_field["subjects"] == {"field": "subjects", "kind": "tree", "separator": "/", "authority": {"name": "MeSH"},
                                    "values": 4, "built": True, "in_manifest": True, "filterable": True}
    assert by_field["lang"]["built"] is False and by_field["date"]["in_manifest"] is False


def test_the_cache_is_per_snapshot_and_a_large_collection_waits_for_the_maintainer(tmp_path):
    con, codex = manifest(tmp_path), a_codex()
    idx = tmp_path / "index"
    assert facets.build(con, codex, idx, snapshot_id="2026-08") == ["subjects", "lang", "authors"], \
        "every declared field the manifest carries; `date` is not"
    assert facets.cached(idx, "lang", snapshot_id="2026-08")["values"] == {"en": 3, "de": 1}
    assert facets.cached(idx, "lang", snapshot_id="2026-09") is None, "another snapshot: counted again"
    with pytest.raises(facets.NotBuilt) as e:
        facets.values(con, codex, idx, "lang", snapshot_id="2026-09", documents=facets.BUILD_BOUND + 1)
    assert e.value.exit_code == 3
    with pytest.raises(facets.FacetError, match="no column"):
        facets.values(con, codex, idx, "date", snapshot_id="2026-08")
    with pytest.raises(facets.FacetError, match="not a facet"):
        facets.count(con, codex, "colour")


def test_every_cost_is_bounded_a_field_too_wide_is_said_not_counted(tmp_path, monkeypatch):
    """2026-10-07: the pass over pubmed reached 34 GB. A field with more
    distinct values than MAX_DISTINCT is not counted and says so; the
    postings a filter reads are written only for values narrow enough to
    use, and not at all past POSTINGS_ROWS_BOUND; the listing repeats why."""
    from app.ask import filters
    con, codex = manifest(tmp_path), a_codex()
    idx = tmp_path / "index"
    monkeypatch.setattr(facets, "MAX_DISTINCT", 2)
    said = []
    assert facets.build(con, codex, idx, snapshot_id="2026-08", log=said.append) == ["subjects", "lang", "authors"]
    subjects = facets.cached(idx, "subjects", snapshot_id="2026-08")
    assert subjects["values"] is None and "more than 2 distinct values" in subjects["why"]
    assert subjects["postings"] == {"built": False, "why": subjects["why"]}
    lang = facets.cached(idx, "lang", snapshot_id="2026-08")
    assert lang["values"] == {"en": 3, "de": 1} and lang["postings"]["built"] is True
    assert any("subjects" in m and "not counted" in m for m in said)
    listing = facets.fields(con, codex, idx, snapshot_id="2026-08")
    by_field = {f["field"]: f for f in listing["fields"]}
    assert by_field["subjects"]["values"] is None and "distinct" in by_field["subjects"]["why"]
    assert by_field["lang"]["filterable"] is True and by_field["subjects"]["filterable"] is False
    got = facets.values(con, codex, idx, "subjects", snapshot_id="2026-08")
    assert got["values"] is None and "not counted" in got["why"]
    with pytest.raises(filters.FilterError, match="distinct values"):
        filters.book_ids_for(codex, idx, {"subjects": "Humans"}, snapshot_id="2026-08")
    # only narrow values get postings: with MAX_IDS 2, `en` (3 records) has none and is refused by its count
    monkeypatch.setattr(facets, "MAX_DISTINCT", 200_000)
    monkeypatch.setattr(filters, "MAX_IDS", 2)
    facets.build(con, codex, idx, snapshot_id="2026-08")
    pcon = filters.connect(idx)
    assert {r[0] for r in pcon.execute("SELECT DISTINCT value FROM postings WHERE field='lang'")} == {"de"}
    assert {r[0] for r in pcon.execute("SELECT DISTINCT value FROM postings WHERE field='subjects'")} == \
        {"Humans", "Neoplasms/genetics", "Neoplasms/therapy"}, "Neoplasms (3) is broader than 2; the rest are not"
    pcon.close()
    assert facets.cached(idx, "lang", snapshot_id="2026-08")["postings"] == {"built": True, "rows": 1, "values": 1,
                                                                             "broad": 1}
    with pytest.raises(filters.FilterError, match="narrow it"):
        filters.book_ids_for(codex, idx, {"lang": "en"}, snapshot_id="2026-08")
    assert filters.book_ids_for(codex, idx, {"lang": "de"}, snapshot_id="2026-08") == {3}
    # past the rows bound no postings are written for the field, and the cache says why
    monkeypatch.setattr(filters, "MAX_IDS", 20_000)
    monkeypatch.setattr(facets, "POSTINGS_ROWS_BOUND", 3)
    facets.build(con, codex, idx, snapshot_id="2026-08", fields=["lang"])
    lang = facets.cached(idx, "lang", snapshot_id="2026-08")
    assert lang["postings"]["built"] is False and "more than the 3" in lang["postings"]["why"]
    with pytest.raises(filters.FilterError, match="more than the 3"):
        filters.book_ids_for(codex, idx, {"lang": "de"}, snapshot_id="2026-08")
