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


def test_the_summary_counts_only_topics_both_arms_reranked_and_the_applied_apart():
    def row(boost, none, applied=True, reranked=True):
        arm = lambda v: dict.fromkeys(("ndcg", "ndcg_condensed", "p", "rr", "judged", "r_depth"), v)
        return {"arms": {"boost": arm(boost), "none": arm(none)}, "applied": applied,
                "reranked": {"boost": reranked, "none": True}, "error": None}
    s = m.summarise([row(0.5, 0.4), row(0.3, 0.3, applied=False), row(0.9, 0.1, reranked=False)])
    assert (s["topics"], s["counted"], s["boost_applied"]) == (3, 2, 1)
    assert s["all"]["ndcg"] == {"boost": 0.4, "none": 0.35, "boost_won": 1, "boost_lost": 0, "tied": 1}
    assert s["applied"]["ndcg"]["boost"] == 0.5


def test_the_arms_run_through_the_search_and_the_boost_is_restored(monkeypatch, capsys):
    from app import query_service, retrieval
    deployed = retrieval.ENTITY_BOOST
    calls = []

    def search(collections, query, k, mode, translate):
        calls.append(retrieval.ENTITY_BOOST)
        retrieval._tagged_among(None, None, [], [])
        retrieval._reranked(query, [], None, 8.0, 50)
        order = ["10073554", "10065107"] if retrieval.ENTITY_BOOST else ["10065107", "10073554"]
        return {"hits": [{"chunk_id": f"pubmed:{p}:0:1"} for p in order]}
    monkeypatch.setattr(query_service, "run_search", search)
    monkeypatch.setattr(retrieval, "_tagged_among", lambda *a, **k: {10073554})
    monkeypatch.setattr(retrieval, "_reranked", lambda q, hits, *a, **k: (hits, True, None))
    data = m.prepare([(2017, TOPICS_2017, QRELS_2017), (2018, TOPICS_2018, QRELS_2018)])
    rows = m.run(data)
    assert calls == [deployed, 0.0, 0.0, deployed], "the deployed boost, then none; the order alternates"
    assert retrieval.ENTITY_BOOST == deployed, "restored"
    assert rows[0]["applied"] and all(rows[0]["reranked"].values())
    assert rows[0]["arms"]["boost"]["rr"] == 1.0 and rows[0]["arms"]["none"]["rr"] == 0.5
    assert '"counted": 2' in capsys.readouterr().err
