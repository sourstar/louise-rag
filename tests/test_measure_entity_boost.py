"""tools/measure/entity_boost_trecpm.py — the entity boost measured on TREC
Precision Medicine's judged abstracts (task #33): TREC's files read as TREC
writes them, the metrics as trec_eval and Sakai define them, the two arms run
through the deployed search with the boost restored after."""
import math

import pytest

from tools.measure import entity_boost_trecpm as m

TOPICS_2017 = """<topics task="2017 TREC Precision Medicine">
<topic number="1">
    <disease>Liposarcoma</disease>
    <gene>CDK4 Amplification</gene>
    <demographic>38-year-old male</demographic>
    <other>GERD</other>
</topic>
<topic number="2">
    <disease>Colon cancer</disease>
    <gene>KRAS (G13D), BRAF (V600E)</gene>
    <demographic>52-year-old male</demographic>
    <other>Type II Diabetes, Hypertension</other>
</topic>
</topics>"""
TOPICS_2018 = """<topics task="2018 TREC Precision Medicine">
<topic number="1">
    <disease>melanoma</disease>
    <gene>BRAF (E586K)</gene>
    <demographic>64-year-old male</demographic>
</topic>
</topics>"""
QRELS_2017 = "1 0 10065107 0\n1 0 AACR_2012-102 2\n1 0 10073554 2\n1 0 10082456 1\n2 0 11111111 0\n"
QRELS_2018 = "1 0 22222222 1\n"


def test_topics_and_qrels_are_read_as_trec_writes_them():
    topics = m.parse_topics(TOPICS_2017, 2017)
    assert [t["qid"] for t in topics] == ["2017-1", "2017-2"]
    assert topics[1]["query"] == "Colon cancer KRAS (G13D), BRAF (V600E)", "disease, then gene"
    assert m.parse_topics(TOPICS_2018, 2018)[0]["query"] == "melanoma BRAF (E586K)"
    rels = m.parse_qrels(QRELS_2017, 2017)
    assert rels["2017-1"] == {"10065107": 0, "10073554": 2, "10082456": 1}, "AACR abstracts are not PubMed"


def test_prepare_keeps_the_topics_with_a_relevant_pubmed_abstract():
    data = m.prepare([(2017, TOPICS_2017, QRELS_2017), (2018, TOPICS_2018, QRELS_2018)])
    assert [d["qid"] for d in data] == ["2017-1", "2018-1"], "2017-2 has no relevant abstract"
    assert data[0]["rels"]["10073554"] == 2


def test_the_metrics_are_trec_eval_s_and_sakai_s():
    rels = {"a": 2, "b": 1, "c": 0}
    ranked = ["x", "b", "c", "a"]                 # x unjudged
    got = m.metrics(ranked, rels, k=10, depth=50)
    dcg = 1 / math.log2(3) + 2 / math.log2(5)
    idcg = 2 / math.log2(2) + 1 / math.log2(3)
    assert got["ndcg"] == round(dcg / idcg, 4)
    condensed = 1 / math.log2(2) + 0 + 2 / math.log2(4)      # x removed: b, c, a
    assert got["ndcg_condensed"] == round(condensed / idcg, 4)
    assert got["p"] == 0.2 and got["rr"] == 0.5 and got["judged"] == 0.3 and got["r_depth"] == 1.0
    assert m.metrics([], rels)["ndcg"] == 0.0


def test_the_summary_compares_every_arm_with_the_baseline_over_topics_all_arms_reranked():
    def row(values, applied=True, reranked=True):
        arm = lambda v: dict.fromkeys(m.METRICS, v)
        return {"arms": {a: arm(v) for a, v in values.items()}, "applied": applied,
                "reranked": {a: (reranked if a == "any" else True) for a in values},
                "ms": {a: 100 for a in values}, "error": None}
    s = m.summarise([row({"boost": 0.5, "none": 0.4, "any": 0.6, "all": 0.4}),
                     row({"boost": 0.3, "none": 0.3, "any": 0.2, "all": 0.3}, applied=False),
                     row({"boost": 0.9, "none": 0.1, "any": 0.9, "all": 0.9}, reranked=False)])
    assert (s["topics"], s["counted"], s["applied"]) == (3, 2, 1), "a topic one arm did not rerank is left out"
    assert s["all_topics"]["none"]["ndcg"] == {"mean": 0.35}
    assert s["all_topics"]["any"]["ndcg"] == {"mean": 0.4, "won": 1, "lost": 1, "tied": 0}
    assert s["applied_topics"]["boost"]["ndcg"] == {"mean": 0.5, "won": 1, "lost": 0, "tied": 0}
    assert s["all_topics"]["all"]["ms_median"] == 100


def test_the_restriction_is_the_records_tagged_with_any_or_all_of_the_identifiers():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.executescript("""
        CREATE TABLE documents (uid TEXT PRIMARY KEY, source TEXT, native_id TEXT);
        CREATE TABLE doc_keys (uid TEXT, entity_id TEXT, PRIMARY KEY (uid, entity_id));
        CREATE INDEX idx_doc_keys_entity ON doc_keys(entity_id);
        INSERT INTO documents VALUES ('pubmed:1','pubmed','1'), ('pubmed:2','pubmed','2'),
                                     ('pubmed:3','pubmed','3'), ('other:4','other','4');
        INSERT INTO doc_keys VALUES ('pubmed:1','hgnc:KRAS'), ('pubmed:1','hgnc:BRAF'),
                                    ('pubmed:2','hgnc:BRAF'), ('other:4','hgnc:BRAF');
    """)
    assert m.tagged(con, "pubmed", ["hgnc:KRAS", "hgnc:BRAF"], "any") == {1, 2}
    assert m.tagged(con, "pubmed", ["hgnc:KRAS", "hgnc:BRAF"], "all") == {1}
    assert m.tagged(con, "pubmed", ["hgnc:TP53"], "any") == set()


def test_the_arms_run_through_the_search_and_everything_is_restored(monkeypatch, capsys):
    from app import query_service, retrieval
    deployed, real_one = retrieval.ENTITY_BOOST, retrieval._search_one_collection
    seen = []

    def one_collection(con, coll, query, k, mode, translate, entity_ids, *args, **kw):
        seen.append((retrieval.ENTITY_BOOST, kw.get("book_ids")))
        return {}

    class Coll:
        plugin = "pubmed"

    def search(collections, query, k, mode, translate):
        retrieval._search_one_collection(None, Coll(), query, k, mode, translate, ["hgnc:CDK4"])
        book_ids = seen[-1][1]
        if book_ids is not None and not book_ids:
            return {"hits": []}
        retrieval._reranked(query, [], None, 8.0, 50)
        order = ["10073554", "10065107"] if retrieval.ENTITY_BOOST or book_ids else ["10065107", "10073554"]
        return {"hits": [{"chunk_id": f"pubmed:{p}:0:1"} for p in order]}
    monkeypatch.setattr(retrieval, "_search_one_collection", one_collection)
    monkeypatch.setattr(retrieval, "_reranked", lambda q, hits, *a, **k: (hits, True, None))
    monkeypatch.setattr(m, "tagged", lambda con, plugin, ids, mode: {10073554} if mode == "any" else set())
    monkeypatch.setattr(query_service, "run_search", search)
    data = m.prepare([(2017, TOPICS_2017, QRELS_2017), (2018, TOPICS_2018, QRELS_2018)])
    rows = m.run(data)
    assert [b for b, _ in seen[:4]] == [deployed, 0.0, 0.0, 0.0], "boost as deployed, then none, any, all"
    assert seen[2][1] == {10073554} and seen[3][1] == set(), "any: the tagged records; all: none tagged"
    assert retrieval.ENTITY_BOOST == deployed and retrieval._search_one_collection is one_collection, \
        "restored to what was there before the run"
    first = rows[0]
    assert first["applied"] and all(first["reranked"].values()), "an arm restricted to nothing counts"
    assert first["arms"]["boost"]["rr"] == 1.0 and first["arms"]["none"]["rr"] == 0.5
    assert first["arms"]["any"]["rr"] == 1.0 and first["arms"]["all"]["ndcg"] == 0.0
    assert first["restricted_to"] == {"boost": None, "none": None, "any": 1, "all": 0}
    assert rows[1]["arms"] and list(rows[1]["ms"]) == ["none", "any", "all", "boost"], "the order rotates"
    assert '"counted": 2' in capsys.readouterr().err
    assert real_one is not None
