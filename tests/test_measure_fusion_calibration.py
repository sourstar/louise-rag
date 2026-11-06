"""tools/measure/fusion_calibration.py — the cross-collection merge judged on
the node's queries (docs/notes/federated-merge.md)."""
import json
import math
import random
import sqlite3

import pytest

from tools.measure import fusion_calibration as f


def test_platt_recovers_the_sigmoid_it_was_drawn_from():
    rnd = random.Random(1)
    scores = [rnd.uniform(-6, 6) for _ in range(4000)]
    labels = [int(rnd.random() < 1 / (1 + math.exp(-(1.5 * s - 2.0)))) for s in scores]
    a, b = f.platt(scores, labels)
    assert a == pytest.approx(1.5, rel=0.12) and b == pytest.approx(-2.0, rel=0.12)


def test_platt_refuses_a_class_too_small_to_fit():
    assert f.platt([0.1] * 20, [0] * 20) is None
    assert f.platt([0.1] * 20, [1] * 3 + [0] * 17) is None


def test_mrr_and_ndcg_at_ten():
    assert f.mrr([0, 1, 2, 3]) == pytest.approx(1 / 3)
    assert f.mrr([0] * 10 + [3]) == 0.0, "past ten counts nothing"
    assert f.mrr([None, 3]) == 0.5, "an unjudged record is not relevant"
    assert f.ndcg([3, 0], [3, 0]) == pytest.approx(1.0)
    assert f.ndcg([0, 3], [3, 0]) == pytest.approx(7 / math.log2(3) / 7)
    assert f.ndcg([0, 0], [0, 0]) == 0.0


def test_the_judges_answer_is_read_in_umbrela_s_format_or_as_a_bare_digit():
    assert f.grade_of("##final score: 2") == 2
    assert f.grade_of("M: 2, T: 3\n##Final Score: 3") == 3
    assert f.grade_of("1") == 1
    assert f.grade_of("I would say 1 or 2") is None
    assert f.grade_of("") is None


def test_kappa_is_one_on_agreement_and_zero_on_chance():
    assert f.cohen_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)
    assert f.cohen_kappa([0, 0, 1, 1], [0, 1, 0, 1], k=2, weighted=False) == pytest.approx(0.0)


def test_the_queries_come_from_the_query_log_read_only_once_each(tmp_path):
    db = tmp_path / "m.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE query_log (ts INTEGER, endpoint TEXT, query TEXT)")
    con.executemany("INSERT INTO query_log VALUES (?,?,?)",
                    [(3, "search", "b"), (1, "ask", "a"), (2, "search", "a"), (4, "chat", "c"),
                     (5, "search", "  ")])
    con.commit()
    con.close()
    assert f.queries_from_log(db) == ["a", "b"]


def _lists(rows):
    """{query: {collection: [(book_id, score)]}} -> lists.jsonl rows."""
    out = []
    for pass_name, per_query in rows.items():
        for q, lists in per_query.items():
            out.append({"query": q, "pass": pass_name, "lists": {
                n: {"reranked": all(s is not None for _, s in hits), "rerank_model": "m",
                    "hits": [{"rank": i, "book_id": b, "rerank_score": s, "title": "", "text": f"{n}{b}"}
                             for i, (b, s) in enumerate(hits, 1)]}
                for n, hits in lists.items()}})
    return out


def test_to_judge_takes_each_record_once_from_every_list_s_top_ten():
    rows = _lists({"today": {"q": {"a": [(1, 1.0), (2, 0.5)], "b": [(1, None)]}},
                   "reranked": {"q": {"a": [(2, 0.9), (1, 0.4)], "b": [(1, 0.2)]}}})
    pool = f.to_judge(rows)
    assert sorted((p["collection"], p["book_id"]) for p in pool.values()) == [("a", 1), ("a", 2), ("b", 1)]


def test_score_finds_the_merge_on_relevance_better_when_one_collection_holds_nothing(tmp_path):
    """Collection `law` answers one query in three, with its first record,
    and `med` every one with its first three, both scored 5..-4 the same
    way. By rank, law's first record takes place 2 of every merged list; on
    relevance, law's calibration puts med's three first."""
    rnd = random.Random(0)
    today, reranked, judged = {}, {}, []
    for qi in range(60):
        q = f"q{qi}"
        med = [(i, 5.0 - i + rnd.uniform(-0.1, 0.1)) for i in range(10)]
        law = [(100 + i, 5.0 - i + rnd.uniform(-0.1, 0.1)) for i in range(10)]
        today[q] = reranked[q] = {"med": med, "law": law}
        for b, s in med:
            judged.append({"key": f.judge_key(q, "med", b), "grade": 3 if s > 2.5 else 0})
        for i, (b, _) in enumerate(law):
            # law's first record answers one query in three: its best score
            # means a third of what med's does
            judged.append({"key": f.judge_key(q, "law", b), "grade": 2 if i == 0 and qi % 3 == 0 else 0})
    lists = tmp_path / "lists.jsonl"
    lists.write_text("".join(json.dumps(r) + "\n" for r in _lists({"today": today, "reranked": reranked})))
    jp = tmp_path / "judged.jsonl"
    jp.write_text("".join(json.dumps(r) + "\n" for r in judged))
    got = f.score(lists, jp)
    assert got["judged_queries"] == 60
    assert got["ndcg10"]["relevance"] > got["ndcg10"]["today"]
    assert got["vs_today"]["relevance"]["ndcg"]["better"] > 40
    assert got["vs_today"]["relevance"]["ndcg"]["p"] < 0.01
    assert set(got["toml"]) == {"law", "med"}
    assert got["toml"]["med"].startswith("calibration = { a = ")
    assert got["calibration_heldout"]["med"]["points"] == 600
