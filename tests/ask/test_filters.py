"""tests/ask/test_filters.py — facet filters (05 §5.2, §5.7): the postings
the facet index keeps per snapshot, the records a filter admits (flat,
tree with its subtree, exclusions, several facets intersected), a facet
not declared or not indexed refused, a set too large refused; and the
restriction reaching both legs as a prefilter through the query service."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import query_service, retrieval
from app.ask import facets, filters
from app.hold.codex import Facet
from tests.ask.test_facets import manifest
from tests.hold.conftest import pubmed


def a_codex():
    return pubmed(facets=(Facet("subjects", "tree", "/", {"name": "MeSH"}), Facet("lang", "flat"),
                          Facet("authors", "flat"), Facet("date", "range")))


def test_the_build_writes_postings_and_a_filter_reads_them(tmp_path):
    con, codex = manifest(tmp_path), a_codex()
    idx = tmp_path / "index"
    facets.build(con, codex, idx, snapshot_id="2026-08")
    pcon = filters.connect(idx)
    assert filters.built_for(pcon, "subjects") == "2026-08" and filters.built_for(pcon, "date") is None
    assert pcon.execute("SELECT COUNT(*) FROM postings WHERE field='lang'").fetchone()[0] == 4, "one per record with a value"
    pcon.close()
    ids = lambda f: filters.book_ids_for(codex, idx, f, snapshot_id="2026-08")  # noqa: E731
    assert ids(None) is None and ids({}) is None
    assert ids({"lang": "en"}) == {1, 2, 4}
    assert ids({"subjects": "Neoplasms"}) == {1, 2, 3}, "a tree value admits its subtree"
    assert ids({"subjects": "Neoplasms/genetics"}) == {1, 3}
    assert ids({"subjects": "Humans"}) == {1, 2}
    assert ids({"lang": "en", "subjects": "Neoplasms"}) == {1, 2}, "two facets intersect"
    assert ids({"lang": "en", "subjects": {"not": "Neoplasms"}}) == {4}
    assert ids({"subjects": "Nowhere"}) == set(), "nothing admitted is an answer"
    assert filters.book_ids_for(codex, idx, {"lang": {"not": "en"}}, snapshot_id="2026-08",
                                all_ids=lambda: [1, 2, 3, 4]) == {3}
    with pytest.raises(filters.FilterError, match="exclusions"):
        ids({"lang": {"not": "en"}})
    with pytest.raises(filters.FilterError, match="not a facet"):
        ids({"colour": "red"})
    with pytest.raises(filters.FilterError, match="range"):
        ids({"date": "2020"})
    with pytest.raises(filters.FilterError, match="not indexed"):
        filters.book_ids_for(codex, idx, {"lang": "en"}, snapshot_id="2026-09")
    with pytest.raises(filters.FilterError, match="not indexed"):
        filters.book_ids_for(codex, tmp_path / "nowhere", {"lang": "en"}, snapshot_id="2026-08")


def test_a_set_too_large_for_the_legs_is_refused_not_cut(tmp_path, monkeypatch):
    con, codex = manifest(tmp_path), a_codex()
    idx = tmp_path / "index"
    facets.build(con, codex, idx, snapshot_id="2026-08")
    monkeypatch.setattr(filters, "MAX_IDS", 2)
    with pytest.raises(filters.FilterError, match="narrow it"):
        filters.book_ids_for(codex, idx, {"lang": "en"}, snapshot_id="2026-08")


def test_the_restriction_reaches_the_legs_through_the_query_service(monkeypatch):
    c = SimpleNamespace(name="pubmed", plugin="pubmed", codex=a_codex())
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [c])
    monkeypatch.setattr(query_service, "withheld_from", lambda names, **kw: [])
    monkeypatch.setattr(query_service.cancel, "watch", lambda con: con)
    monkeypatch.setattr(query_service.db, "connect", lambda collection: object())
    seen = {}

    def fake_search(con, query, k, mode, translate, collection, rerank_depth, all_languages, book_ids=None):
        seen["book_ids"] = book_ids
        return {"hits": [], "timings": {"total": 0}}
    monkeypatch.setattr(retrieval, "search", fake_search)
    monkeypatch.setattr(query_service, "book_ids_for", lambda f: (lambda collection: {7, 9}) if f else None)
    query_service.run_search(["pubmed"], "q", 5, "hybrid", False, filters={"lang": "en"})
    assert seen["book_ids"] == {7, 9}
    query_service.run_search(["pubmed"], "q", 5, "hybrid", False)
    assert seen["book_ids"] is None, "no filter: no prefilter"

    def refuse(collection):
        raise filters.FilterError("'colour' is not a facet of this collection")
    monkeypatch.setattr(query_service, "book_ids_for", lambda f: refuse)
    with pytest.raises(filters.FilterError, match="not a facet"):
        query_service.run_search(["pubmed"], "q", 5, "hybrid", False, filters={"colour": "red"})
    assert seen["book_ids"] is None, "refused before any leg ran"


def test_a_filtered_federation_is_restricted_per_collection_and_says_how_many(monkeypatch):
    seen = {}

    def fake_federated(participants, query, k, mode, translate, rerank_depth, all_languages, book_ids_for=None):
        seen.update({c.name: book_ids_for(c) for c in participants})
        return {"hits": [], "timings": {"total": 0}}
    a = SimpleNamespace(name="a", plugin="a", codex=a_codex())
    b = SimpleNamespace(name="b", plugin="b", codex=None)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [a, b])
    monkeypatch.setattr(query_service, "withheld_from", lambda names, **kw: [])
    monkeypatch.setattr(retrieval, "federated_search", fake_federated)
    from app.hold import collection as col
    from app.share import seeder
    monkeypatch.setattr(filters, "book_ids_for",
                        lambda codex, index_dir, f, *, snapshot_id, all_ids=None, tagged=None: {3, 4})
    monkeypatch.setattr(seeder, "current_snapshot", lambda shard_tree: None)
    monkeypatch.setattr(seeder, "cached_snapshot", lambda shard_tree: None)
    monkeypatch.setattr(col, "shard_dir_of", lambda root, name: None)
    query_service.run_search(None, "q", 5, "hybrid", False, filters={"lang": "en"})
    assert seen == {"a": {3, 4}, "b": set()}, "a collection without a Codex has no facets: it admits nothing"


def test_an_empty_restriction_returns_nothing_before_any_leg_runs(monkeypatch):
    called = []
    monkeypatch.setattr(retrieval.fts, "existing_groups", lambda c: called.append("fts") or [])
    monkeypatch.setattr(retrieval, "catalog_leg", lambda *a, **k: (called.append("catalog") or [], None))
    monkeypatch.setattr(retrieval.db, "get_scope", lambda con: {"embed_mode": "off"})
    plan = SimpleNamespace(core="q", translations={}, only=(), searches=lambda g: True,
                           lang_filter=lambda g: None, variants=lambda g, h: ["q"])
    out = retrieval._search_one_collection(None, SimpleNamespace(name="x", plugin="x"), "q", 5, "hybrid", False,
                                           [], plan, 0, book_ids=set())
    assert out["merged"] == [] and out["filtered"] == 0 and called == []


# ---- the identifier filter (05 §5.2, amended 2026-10-25: an identifier restricts only when asked) ----

def test_an_identifier_restricts_only_as_a_filter_the_reader_gives(tmp_path):
    con, codex = manifest(tmp_path), a_codex()
    idx = tmp_path / "index"
    facets.build(con, codex, idx, snapshot_id="2026-08")
    tags = {"hgnc:1101": [1, 3], "hgnc:1100": [3]}
    tagged = lambda value, limit: (len(tags.get(value, [])), tags.get(value, []))  # noqa: E731
    ids = lambda f: filters.book_ids_for(codex, idx, f, snapshot_id="2026-08", tagged=tagged,  # noqa: E731
                                         all_ids=lambda: [1, 2, 3, 4])
    assert ids({"keys": "hgnc:1101"}) == {1, 3}
    assert ids({"keys": "hgnc:1101", "lang": "en"}) == {1}, "with a facet, they intersect"
    assert ids({"keys": {"not": "hgnc:1100"}}) == {1, 2, 4}
    assert ids({"keys": "hgnc:9999"}) == set(), "tagged nowhere here: nothing, which is an answer"
    assert filters.book_ids_for(codex, idx, {"keys": "hgnc:1101"}, snapshot_id="2026-08") == set(), \
        "a collection with no tagged records admits nothing"


def test_an_identifier_too_broad_for_the_legs_is_refused_not_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(filters, "MAX_IDS", 2)
    asked = []

    def tagged(value, limit):
        asked.append(limit)
        return limit, []
    with pytest.raises(filters.FilterError, match="admits more than 2"):
        filters.book_ids_for(a_codex(), tmp_path, {"keys": "hgnc:5172"}, snapshot_id=None, tagged=tagged)
    assert asked == [3], "counted one past the bound, never the whole entity"


def test_the_tagged_records_are_read_by_the_entity_index_alone(tmp_path):
    """The heaviest gene tags 265,237 pubmed documents. The count is read
    from `idx_doc_keys_entity` alone, and `documents` (40 M rows on pubmed)
    is never touched: the native id is the uid's."""
    from app import collections as collections_mod, db
    c = collections_mod.Collection(
        name="pubmedish", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb")
    con = db.connect(c)
    con.executemany("INSERT INTO documents(uid, source, native_id, path, in_scope) VALUES (?,?,?,?,?)",
                    [(f"pubmed:{i}", "pubmed", str(i), "x", 1 if i != 3 else 0) for i in range(1, 5)])
    con.executemany("INSERT INTO doc_keys(uid, entity_id) VALUES (?,?)",
                    [("pubmed:1", "hgnc:1101"), ("pubmed:3", "hgnc:1101"), ("pubmed:2", "hgnc:1100")])
    con.commit()
    for sql, covering in ((query_service.TAGGED_COUNT_SQL, True), (query_service.TAGGED_SQL, False)):
        plan = [str(r[-1]).upper() for r in con.execute("EXPLAIN QUERY PLAN " + sql, ("hgnc:1101", 10))]
        assert any("IDX_DOC_KEYS_ENTITY" in row for row in plan), plan
        assert not any("DOCUMENTS" in row or "TEMP B-TREE" in row for row in plan), plan
        assert not covering or any("COVERING INDEX" in row for row in plan), plan
        assert not any(row.startswith("SCAN") and "SCAN K" not in row and "SUBQUERY" not in row
                       and "COVERING" not in row for row in plan), plan
    assert con.execute(query_service.TAGGED_COUNT_SQL, ("hgnc:1101", 1)).fetchone()[0] == 1, "stopped at the limit"
    assert sorted(r[0] for r in con.execute(query_service.TAGGED_SQL, ("hgnc:1101", 10))) == ["pubmed:1", "pubmed:3"]
