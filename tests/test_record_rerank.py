"""The reranker judges the RECORD, not one bare chunk of it (05 §5.6,
amended 2026-10-10; docs/notes/one-language-per-work.md, "Fixed
(2026-10-10)").

Found live on 906f06b: a record was represented by the leg's single best
chunk, sent bare, and a byline or heading won — "Hamlet Shakespeare" gave
eleven plays whose best chunk was the same 22 characters, "by William
Shakespeare", Hamlet nowhere in the top 10. Now each record sends its
`JUDGED_CHUNKS` best matched chunks, each under the record's title and
authors, and takes the best score. Measured live: titles MRR 0.342 → 0.660,
R@10 0.718 → 0.872 (26 better, 0 worse); descriptive MRR 0.121 → 0.223.
"""
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app import collections as collections_mod, retrieval

RECIPE = collections_mod.RerankRecipe(url="http://reranker/v1/rerank", api="jina", model="")


def h(uid: str, seq: int, source: str, text: str | None = None) -> dict:
    return {"chunk_id": f"{uid}:{seq}:1", "book_id": int(uid.split(":")[1]), "source": source,
            "text": text if text is not None else f"{uid} chunk {seq}", "chapter": f"ch{seq}"}


class Scorer:
    """A cross-encoder stand-in: one point per query word the text holds."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def post(self, url, json=None, timeout=None, headers=None, **_kw):   # noqa: A002
        self.bodies.append({"json": json, "timeout": timeout})
        words = json["query"].lower().split()
        rows = [{"index": i, "relevance_score": float(sum(w in d.lower() for w in words))}
                for i, d in enumerate(json["documents"])]
        return httpx.Response(200, request=httpx.Request("POST", url), json={"results": rows})


@pytest.fixture
def scorer(monkeypatch):
    s = Scorer()
    monkeypatch.setattr(retrieval.httpc, "post", s.post)
    return s


def test_the_fusion_keeps_each_records_best_matched_chunks():
    text = [h("g:7", 3, "fts:en"), h("g:7", 9, "fts:en"), h("g:8", 0, "fts:en"),
            h("g:7", 1, "fts:en")]
    dense = [h("g:7", 5, "dense"), h("g:8", 4, "dense")]
    seven = next(m for m in retrieval._merge_rrf([text, dense]) if m["book_id"] == 7)
    got = [c["chunk_id"] for c in seven[retrieval.MATCHED]]
    assert got[0] == seven["chunk_id"] == "g:7:3:1", "its best chunk first"
    assert got == ["g:7:3:1", "g:7:5:1"][:retrieval.JUDGED_CHUNKS], \
        "then the best-placed of the others (position 0 in the vector list)"
    assert len(got) == retrieval.JUDGED_CHUNKS


def test_a_byline_under_the_wrong_title_loses_to_the_right_book(scorer):
    """Eleven plays shared one byline chunk and the reranker could not tell
    them apart; under each record's title it can."""
    plays = []
    for nid, title in ((1790, "Troilus and Cressida"), (1524, "Hamlet")):
        hit = h(f"gutenberg:{nid}", 0, "dense", "by William Shakespeare")
        hit[retrieval.MATCHED] = [dict(hit)]
        hit[retrieval.HEAD] = f"{title} — Shakespeare, William"
        plays.append(hit)
    ranked, ok = retrieval._rerank_list("Hamlet Shakespeare", plays, RECIPE, 8.0, 50)
    assert ok is True
    assert [r["book_id"] for r in ranked] == [1524, 1790]
    sent = scorer.bodies[0]["json"]["documents"]
    assert sent[0] == "Troilus and Cressida — Shakespeare, William\n\nby William Shakespeare", \
        "the title line, a blank line, the chunk whole"


def test_the_record_takes_its_best_chunk_and_shows_it(scorer):
    heading = h("g:1", 0, "dense", "The Fall of Troy")
    passage = h("g:1", 7, "dense", "the wrath of Achilles, the quarrel with Agamemnon")
    rec = dict(heading)
    rec[retrieval.MATCHED] = [heading, passage]
    other = h("g:2", 0, "dense", "Achilles")
    other[retrieval.MATCHED] = [dict(other)]
    ranked, ok = retrieval._rerank_list("wrath of Achilles", [other, rec], RECIPE, 8.0, 50)
    assert ok and [r["book_id"] for r in ranked] == [1, 2]
    top = ranked[0]
    assert top["chunk_id"] == "g:1:7:1" and top["text"].startswith("the wrath"), \
        "the result carries the chunk that scored the record"
    assert top["chapter"] == "ch7"
    assert top["rerank_score"] == 3.0
    assert len(scorer.bodies[0]["json"]["documents"]) == 3


def test_the_budget_grows_with_the_texts_sent(scorer):
    hits = []
    for i in range(4):
        a, b = h(f"g:{i}", 0, "dense"), h(f"g:{i}", 1, "dense")
        rec = dict(a)
        rec[retrieval.MATCHED] = [a, b]
        hits.append(rec)
    retrieval._rerank_list("q", hits, RECIPE, 8.0, 50)
    assert scorer.bodies[0]["timeout"] == pytest.approx(16.0), "8 texts for 4 records: twice"


def test_a_hit_from_elsewhere_with_no_matched_chunks_goes_bare_as_before(scorer):
    hits = [h("g:1", 0, "dense", "alpha"), h("g:2", 0, "dense", "beta")]
    ranked, ok = retrieval._rerank_list("beta", hits, RECIPE, 8.0, 50)
    assert ok and [r["book_id"] for r in ranked] == [2, 1]
    assert scorer.bodies[0]["json"]["documents"] == ["alpha", "beta"]


def test_heads_come_from_the_manifest_in_one_query():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE documents(source TEXT, native_id TEXT, title TEXT, authors TEXT)")
    con.executemany("INSERT INTO documents VALUES(?,?,?,?)", [
        ("gutenberg", "1524", "Hamlet", "Shakespeare, William"),
        ("gutenberg", "9", "Untitled", None),
        ("gutenberg", "77", "x" * 400, "y")])
    hits = [{"book_id": 1524}, {"book_id": 9}, {"book_id": 5}, {"book_id": 77}]
    retrieval._attach_heads(con, SimpleNamespace(plugin="gutenberg"), hits)
    assert hits[0][retrieval.HEAD] == "Hamlet — Shakespeare, William"
    assert hits[1][retrieval.HEAD] == "Untitled"
    assert retrieval.HEAD not in hits[2], "a record the manifest lacks goes bare"
    assert len(hits[3][retrieval.HEAD]) == retrieval.HEAD_CHARS


def test_a_manifest_that_cannot_answer_leaves_the_chunks_bare():
    con = sqlite3.connect(":memory:")
    hits = [{"book_id": 1}]
    retrieval._attach_heads(con, SimpleNamespace(plugin="gutenberg"), hits)
    assert hits == [{"book_id": 1}]


def test_the_bookkeeping_never_leaves_the_search(monkeypatch):
    hit = h("g:1", 0, "dense")
    hit[retrieval.MATCHED] = [dict(hit)]
    hit[retrieval.HEAD] = "t"
    hit["rrf"] = 0.1

    class Con:
        def execute(self, *a, **k):
            return iter([])

    monkeypatch.setattr(retrieval.verify_on_use, "annotate", lambda con, hits: None)
    monkeypatch.setattr(retrieval.verify_on_use, "cite", lambda con, ids: None)
    monkeypatch.setattr(retrieval.db, "get_scope", lambda con: {"embed_mode": "off"})
    retrieval._attach_metadata_and_enqueue(Con(), SimpleNamespace(plugin="g"), [hit])
    assert retrieval.MATCHED not in hit and retrieval.HEAD not in hit and "rrf" not in hit
