"""One hit per work (05 §5.3, amended 2026-10-09; app/ask/works.py).

The measurement (docs/notes/one-language-per-work.md, 451 queries): every
rule that keeps ONE language per work lost the work the reader wanted on
57–83 queries; showing each work once, at its best edition's position and
in the query's language where the work has a book in it, was better on 50
and worse on none. Work groups come from held claims (02 §2.10), never from
a guess at search time; nothing is dropped — the other editions are listed
under the hit."""
from types import SimpleNamespace

from app import collections as collections_mod, retrieval
from app.ask import works
from tests.ask.test_annotation_facts import setup


def hit(uid: str, lang: str, seq: int = 0) -> dict:
    return {"chunk_id": f"{uid}:{seq}:1", "book_id": int(uid.split(":")[1]), "lang": lang,
            "text": uid}


E = works.Edition
DQ = [E("g:1", "Don Quijote", "es", True), E("g:2", "Don Quixote", "en", True),
      E("g:3", "Don Quichotte", "fr", True), E("g:4", "Don Quijote II", "es", False)]
WORK_OF = {e.uid: "g:1" for e in DQ}
MEMBERS = {"g:1": DQ}


def test_a_work_is_shown_once_where_its_best_edition_ranked_in_the_preferred_language():
    hits = [hit("g:9", "de"), hit("g:2", "en"), hit("g:8", "en"), hit("g:3", "fr"), hit("g:1", "es")]
    out = works.collapse(hits, WORK_OF, MEMBERS, works.preference(None, "fr"))
    assert [retrieval.record_of(h) for h in out] == ["g:9", "g:3", "g:8"], \
        "the work at rank 2, its best edition's place, shown in French; nothing else moves"
    ed = out[1]["editions"]
    assert [e["uid"] for e in ed] == ["g:2", "g:1", "g:4"], "English next (the fallback), then by rank"
    assert ed[0] == {"uid": "g:2", "title": "Don Quixote", "lang": "en", "held": True,
                     "rank": 2, "chunk_id": "g:2:0:1"}
    assert ed[2]["rank"] is None and ed[2]["held"] is False, "an edition not found is still listed"
    assert "editions" not in out[0], "a record of no work is untouched"


def test_the_picker_comes_first_then_the_query_s_language_then_english_then_the_best_ranked():
    assert works.preference("de", "fr") == ("de", "fr", "en")
    assert works.preference(None, None) == ("en",)
    assert works.preference("original", "EN") == ("en",)
    hits = [hit("g:3", "fr"), hit("g:2", "en"), hit("g:1", "es")]
    shown = lambda prefer: retrieval.record_of(works.collapse(hits, WORK_OF, MEMBERS, prefer)[0])
    assert shown(works.preference("es", "fr")) == "g:1"
    assert shown(works.preference(None, "it")) == "g:2", "no Italian edition: English"
    assert shown(()) == "g:3", "no preference met: the best-ranked"


def test_a_search_fills_its_page_with_distinct_works(monkeypatch):
    """The grouping runs after the reranker and BEFORE the cut to k: a page
    of three is three works."""
    hits = [hit("g:2", "en"), hit("g:3", "fr"), hit("g:7", "en"), hit("g:1", "es"), hit("g:8", "en")]
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda con, c, q, k, mode, tr, ids, plan=None, depth=0: {
                            "catalog": [], "merged": [dict(x) for x in hits], "translations": {},
                            "timings": {}, "scope": {}})
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, h: None)
    monkeypatch.setattr(retrieval, "_rerank_list", lambda q, h, r, b, d: (h, False))
    monkeypatch.setattr(works, "work_groups", lambda c, uids, **kw: (WORK_OF, MEMBERS))
    c = SimpleNamespace(name="g", plugin="g", codex=object())
    out = retrieval.search(None, "q", k=3, collection=c, translate=False, show_in="fr")
    assert [retrieval.record_of(h) for h in out["hits"]] == ["g:3", "g:7", "g:8"]
    assert out["query_lang"] is None


def test_a_grouping_that_cannot_run_returns_the_list_as_ranked(monkeypatch):
    hits = [hit("g:2", "en"), hit("g:3", "fr")]
    monkeypatch.setattr(works, "work_groups",
                        lambda c, uids, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    c = SimpleNamespace(name="g", codex=object())
    assert works.one_hit_per_work(hits, c, ("en",)) == hits
    assert works.one_hit_per_work(hits, SimpleNamespace(name="g", codex=None), ("en",)) == hits


def test_the_work_groups_are_read_from_the_held_claims(tmp_path):
    """pubmed:1 (la) same-work pubmed:2, and pubmed:3 translation-of
    pubmed:1: one work of three records, joined through pubmed:1 even when
    only pubmed:2 and pubmed:3 are among the hits."""
    target, tcoll, held, codex = setup(tmp_path)
    work_of, members = works.work_groups(tcoll, ["pubmed:2", "pubmed:4"], root=tmp_path,
                                         annotations=[held])
    assert work_of == {"pubmed:1": "pubmed:1", "pubmed:2": "pubmed:1", "pubmed:3": "pubmed:1"}
    assert members["pubmed:1"] == [E("pubmed:1", "T1", "la", True), E("pubmed:2", "T2", "en", True),
                                   E("pubmed:3", "T3", "en", True)]
    hits = [hit("pubmed:2", "en"), hit("pubmed:4", "en"), hit("pubmed:1", "la")]
    out = works.one_hit_per_work(hits, tcoll, works.preference(None, "la"), root=tmp_path,
                                 annotations=[held])
    assert [retrieval.record_of(h) for h in out] == ["pubmed:1", "pubmed:4"]
    assert [e["uid"] for e in out[0]["editions"]] == ["pubmed:2", "pubmed:3"]


def test_a_withheld_annotations_copy_groups_nothing(tmp_path, monkeypatch):
    from app import copy_state
    target, tcoll, held, codex = setup(tmp_path)
    monkeypatch.setattr(copy_state, "withholds", lambda c: "quarantine: roots not yet run")
    assert works.work_groups(tcoll, ["pubmed:1"], root=tmp_path, annotations=[held]) == ({}, {})


def test_no_annotations_collection_about_it_costs_only_the_listing(tmp_path, monkeypatch):
    target, tcoll, held, codex = setup(tmp_path)
    monkeypatch.setattr(collections_mod, "load_annotations", lambda: [])
    assert works.work_groups(tcoll, ["pubmed:1"], root=tmp_path) == ({}, {})
