"""Collections merged on calibrated relevance (05 §5.6, amended 2026-10-31,
operator's B; docs/notes/federated-merge.md).

Each collection's reranker score becomes a probability of relevance through
that collection's own calibration (Platt scaling), and the lists of a
federated search are merged on it — when EVERY list has a calibration for
the reranker that scored it and was reranked this time. Otherwise the merge
stays by rank, as before, and the result says why. Each list keeps its own
order either way.
"""
import dataclasses

import pytest

from app import collections as collections_mod, retrieval
from tests.test_rerank_per_collection import JINA, _col, _hits, _server, _stub_pipeline

CAL = collections_mod.Calibration(a=1.0, b=0.0, rerank_model=JINA.model)


def _recipe(url, b):
    return dataclasses.replace(JINA, url=url, calibration=dataclasses.replace(CAL, b=b))


def test_the_more_relevant_collection_leads_instead_of_alternating(isolated_config, monkeypatch,
                                                                   tmp_path):
    """The fake reranker scores both lists 0..n-1. Collection b's calibration
    says its scores mean far less: under rank fusion the two would alternate,
    on relevance a's records come first."""
    a = _col(tmp_path, "a", _recipe("http://a/v1/rerank", b=0.0))
    b = _col(tmp_path, "b", _recipe("http://b/v1/rerank", b=-10.0))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 4), "b": _hits("b", 4)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=8)
    assert out["merged_by"] == "relevance" and "merge_withheld" not in out
    assert [h["source_collection"] for h in out["hits"]] == ["a"] * 4 + ["b"] * 4
    assert [h["book_id"] for h in out["hits"][:4]] == [3, 2, 1, 0], "each list's own order"
    assert out["hits"][0]["relevance"] > out["hits"][4]["relevance"]


def test_one_list_without_a_calibration_keeps_the_merge_by_rank_and_says_why(
        isolated_config, monkeypatch, tmp_path):
    a = _col(tmp_path, "a", _recipe("http://a/v1/rerank", b=0.0))
    b = _col(tmp_path, "b", dataclasses.replace(JINA, url="http://b/v1/rerank"))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=6)
    assert out["merged_by"] == "rank"
    assert out["merge_withheld"] == [{"collection": "b",
                                      "reason": "its reranker has no calibration"}]
    assert [h["source_collection"] for h in out["hits"][:2]] == ["a", "b"], "alternating, by rank"


def test_no_calibration_anywhere_is_today_s_merge_and_says_nothing_more(
        isolated_config, monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval.config, "RERANK_URL", "")
    a = _col(tmp_path, "a", JINA)
    b = _col(tmp_path, "b")
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=6)
    assert out["merged_by"] == "rank" and "merge_withheld" not in out


def test_a_calibration_for_another_model_is_not_used(isolated_config, monkeypatch, tmp_path):
    other = dataclasses.replace(CAL, rerank_model="another-reranker")
    a = _col(tmp_path, "a", dataclasses.replace(JINA, url="http://a/v1/rerank", calibration=other))
    b = _col(tmp_path, "b", _recipe("http://b/v1/rerank", b=0.0))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=6)
    assert out["merged_by"] == "rank"
    assert out["merge_withheld"] == [{"collection": "a", "reason":
                                      "calibrated for another-reranker, its reranker is "
                                      f"{JINA.model}"}]


def test_a_list_the_reranker_did_not_order_this_time_keeps_the_merge_by_rank(
        isolated_config, monkeypatch, tmp_path):
    import httpx
    a = _col(tmp_path, "a", _recipe("http://a/v1/rerank", b=0.0))
    b = _col(tmp_path, "b", _recipe("http://b/v1/rerank", b=0.0))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch, fail=httpx.ReadTimeout("over budget"))
    out = retrieval.federated_search([a, b], "q", k=6)
    assert out["merged_by"] == "rank"
    assert {w["reason"] for w in out["merge_withheld"]} == {"its list was not reranked this time"}


def test_each_list_keeps_its_own_order_on_relevance():
    """The catalogue's first record (05 §5.6) sits above records its reranker
    scored higher: it stays above them, at the value of the best below it."""
    cal = CAL
    a = [{"book_id": 9, "rerank_score": -5.0},     # moved first by the catalogue rule
         {"book_id": 1, "rerank_score": 2.0},
         {"book_id": 2, "rerank_score": 1.0},
         {"book_id": 3}]                            # below the reranker's depth
    b = [{"book_id": 7, "rerank_score": 1.5}, {"book_id": 8, "rerank_score": -1.0}]
    out = retrieval._merge_by_relevance([(a, cal), (b, cal)])
    order = [h["book_id"] for h in out]
    assert order.index(9) < order.index(1) < order.index(2) < order.index(3)
    assert order.index(8) > order.index(7)
    assert order == [9, 1, 7, 2, 8, 3]
    assert out[0]["relevance"] == out[1]["relevance"] == pytest.approx(cal.probability(2.0), abs=1e-6)
    assert out[-1]["relevance"] == 0.0, "an unscored tail follows every judged record"
    assert all("rrf" in h for h in out)


def test_ties_go_to_the_higher_place_then_the_first_list():
    cal = CAL
    a = [{"book_id": 1, "rerank_score": 0.0}, {"book_id": 2, "rerank_score": 0.0}]
    b = [{"book_id": 3, "rerank_score": 0.0}]
    assert [h["book_id"] for h in retrieval._merge_by_relevance([(a, cal), (b, cal)])] == [1, 3, 2]


def test_keep_lists_returns_each_collection_s_list(isolated_config, monkeypatch, tmp_path):
    a = _col(tmp_path, "a", _recipe("http://a/v1/rerank", b=0.0))
    b = _col(tmp_path, "b", _recipe("http://b/v1/rerank", b=0.0))
    _stub_pipeline(monkeypatch, {"a": _hits("a", 3), "b": _hits("b", 3)})
    _server(monkeypatch)
    out = retrieval.federated_search([a, b], "q", k=2, keep_lists=True)
    assert set(out["lists"]) == {"a", "b"} and len(out["hits"]) == 2
    assert out["lists"]["a"]["reranked"] is True
    assert out["lists"]["a"]["rerank_model"] == JINA.model
    assert [h["book_id"] for h in out["lists"]["a"]["hits"]] == [2, 1, 0]
    assert all("rerank_score" in h for h in out["lists"]["b"]["hits"])
    assert "lists" not in retrieval.federated_search([a, b], "q", k=2)


# ---------------- the collection file ----------------


def test_the_probability_is_platt_s_and_never_overflows():
    cal = collections_mod.Calibration(a=2.0, b=-1.0, rerank_model="m")
    assert cal.probability(0.5) == pytest.approx(0.5)
    assert cal.probability(1e6) == pytest.approx(1.0)
    assert cal.probability(-1e6) == pytest.approx(0.0)


def test_the_reranker_table_carries_its_calibration():
    base = {"url": "http://gpu:30447/v1/rerank", "api": "jina", "rerank_model": "louise-rerank-bge"}
    assert collections_mod.parse_reranker(base, where="f").calibration is None
    got = collections_mod.parse_reranker(
        base | {"calibration": {"a": 0.8, "b": -2.5, "rerank_model": "louise-rerank-bge",
                                "judged": 2140, "set": "fusion-1"}}, where="f").calibration
    assert got == collections_mod.Calibration(0.8, -2.5, "louise-rerank-bge", 2140, "fusion-1")


@pytest.mark.parametrize("bad, says", [
    ({"a": 0.0, "b": 1.0, "rerank_model": "m"}, "a > 0"),
    ({"a": -1.0, "b": 1.0, "rerank_model": "m"}, "a > 0"),
    ({"a": float("nan"), "b": 1.0, "rerank_model": "m"}, "finite"),
    ({"a": 1.0, "rerank_model": "m"}, "numbers a and b"),
    ({"a": 1.0, "b": 1.0}, "names the rerank_model"),
    ({"a": 1.0, "b": 1.0, "rerank_model": "m", "judged": -3}, "a count"),
    ("0.8,-2.5", "is a table"),
])
def test_a_calibration_that_is_not_one_is_refused(bad, says):
    base = {"url": "http://gpu:30447/v1/rerank", "api": "jina", "rerank_model": "m"}
    with pytest.raises(ValueError, match=says):
        collections_mod.parse_reranker(base | {"calibration": bad}, where="f")
