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
                                    "values": 4, "built": True, "in_manifest": True}
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
