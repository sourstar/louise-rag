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
                        lambda codex, index_dir, f, *, snapshot_id, all_ids=None: {3, 4})
    monkeypatch.setattr(seeder, "current_snapshot", lambda shard_tree: None)
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
