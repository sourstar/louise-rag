"""tools/measure/pipeline_eval.py — the whole search, stage by stage, on a
judged set (the operator, 2026-11-02: "Add BM25 fusion and translations too,
make separated scores … verify if it doesn't pay off using a fancy embedder
if the gap is filled by the whole retrieval pipeline")."""
from __future__ import annotations

import json

import pytest

from app import collections as collections_mod
from app import retrieval
from tools.measure import pipeline_eval as pe

RECIPE = collections_mod.RerankRecipe(url="http://r/v1/rerank", api="jina", model="rr")

DOCS = [
    pe.Doc(lang="en", title="Moby-Dick", chunks=["the white whale and captain ahab at sea",
                                                 "the crew of the pequod"]),
    pe.Doc(lang="en", title="Walden", chunks=["life in the woods by a pond"]),
    pe.Doc(lang="it", title="I Malavoglia", chunks=["una famiglia di pescatori in sicilia",
                                                    "la barca della famiglia"]),
    pe.Doc(lang="it", title="Pinocchio", chunks=["un burattino di legno che diventa un bambino"]),
]
QUESTIONS = [pe.Question(text="white whale", relevant=frozenset({0}), lang="en"),
             pe.Question(text="burattino di legno", relevant=frozenset({3}), lang="it")]
PLANS = {"white whale": {"core": "white whale", "translations": {"it": "balena bianca"},
                         "query_lang": "en", "only": [], "main": None, "status": "ok"},
         "burattino di legno": {"core": "burattino di legno",
                                "translations": {"en": "wooden puppet"},
                                "query_lang": "it", "only": [], "main": None, "status": "ok"}}
# encoder "a" ranks every question's chunks in the same wrong order; "b" right
WRONG = [(1, 0), (2, 0), (2, 1), (3, 0), (0, 0), (0, 1)]
RIGHT = {0: [(0, 0), (0, 1), (1, 0), (2, 0), (2, 1), (3, 0)],
         1: [(3, 0), (2, 0), (2, 1), (1, 0), (0, 0), (0, 1)]}


def _judge(seen):
    """The asked book's chunks highest, when judged in their own language."""
    want = {"white whale": "whale", "balena bianca": None, "wooden puppet": None,
            "burattino di legno": "burattino"}

    def call(recipe, query, texts, timeout):
        seen.append((query, list(texts)))
        word = want[query]
        return [10.0 if word and word in t else 0.0 for t in texts]
    return call


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    seen = []
    plans = pe.PlanStore(tmp_path / "plans.jsonl", lambda text: dict(PLANS[text]))
    res = pe.evaluate(tmp_path / "out", "set", DOCS, QUESTIONS,
                      {"a": [WRONG, WRONG], "b": [RIGHT[0], RIGHT[1]]},
                      recipe=RECIPE, plans=plans, depths=[2, 4], say=lambda m: None,
                      call=_judge(seen))
    return res, seen, tmp_path


def test_every_stage_has_its_own_row(run):
    res, _seen, _ = run
    names = set(res["rows"])
    for stage in ("BM25", "a", "b", "a + BM25", "b + BM25"):
        assert stage in names
        assert {f"{stage} + rerank@2", f"{stage} + rerank@4"} <= names


def test_the_better_encoder_wins_alone_and_the_reranker_closes_the_gap(run):
    res, _seen, _ = run
    rows = res["rows"]
    assert rows["b"]["mrr@10"] == 1.0
    assert rows["a"]["mrr@10"] < rows["b"]["mrr@10"]
    # the reranker over all four books finds each question's book first
    assert rows["a + rerank@4"]["hit@1"] == 1.0
    # over two it cannot reach a book "a" ranks lower than that
    assert rows["a + rerank@2"]["hit@2"] <= rows["a"]["hit@2"]


def test_bm25_finds_by_words_in_each_language_group(run):
    res, _seen, _ = run
    assert res["rows"]["BM25"]["hit@1"] == 1.0, "each question names its book's own words"


def test_each_pair_is_judged_in_its_records_language(run):
    _res, seen, _ = run
    by_variant = {}
    for q, texts in seen:
        by_variant.setdefault(q, set()).update(t.split("\n")[0] for t in texts)
    assert by_variant["white whale"] <= {"Moby-Dick", "Walden"}
    assert by_variant["balena bianca"] <= {"I Malavoglia", "Pinocchio"}
    assert by_variant["burattino di legno"] <= {"I Malavoglia", "Pinocchio"}
    assert by_variant["wooden puppet"] <= {"Moby-Dick", "Walden"}


def test_the_pairs_ask_whether_the_encoder_pays_off_after_the_pipeline(run):
    res, _seen, _ = run
    assert "a + BM25 + rerank@4  vs  BM25 + rerank@4" in res["pairs"]
    assert "a  vs  b" in res["pairs"] and "a + BM25 + rerank@2  vs  b + BM25 + rerank@2" in res["pairs"]
    assert set(res["by_language"]) == {"en", "it"}


def test_a_rerun_asks_neither_the_reranker_nor_the_plan_again(run, monkeypatch):
    res, _seen, tmp = run
    plans = pe.PlanStore(tmp / "plans.jsonl", lambda text: pytest.fail("planned again"))
    again = []
    res2 = pe.evaluate(tmp / "out", "set", DOCS, QUESTIONS,
                       {"a": [WRONG, WRONG], "b": [RIGHT[0], RIGHT[1]]},
                       recipe=RECIPE, plans=plans, depths=[2, 4], say=lambda m: None,
                       call=_judge(again))
    assert again == [] and res2["rows"] == res["rows"]
    assert json.loads((tmp / "out" / "pipeline.json").read_text())["depths"] == [2, 4]


def test_another_reranker_in_the_same_place_is_refused(run):
    _res, _seen, tmp = run
    other = collections_mod.RerankRecipe(url="http://elsewhere/v1/rerank", api="jina", model="rr")
    plans = pe.PlanStore(tmp / "plans.jsonl", lambda text: dict(PLANS[text]))
    with pytest.raises(SystemExit, match="different settings"):
        pe.evaluate(tmp / "out", "set", DOCS, QUESTIONS, {"a": [WRONG, WRONG]},
                    recipe=other, plans=plans, depths=[2], say=lambda m: None, call=_judge([]))
