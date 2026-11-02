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
    typed = pe.PlanStore(tmp_path / "typed.jsonl", lambda text: _typed(text))
    res = pe.evaluate(tmp_path / "out", "set", DOCS, QUESTIONS,
                      {"a": [WRONG, WRONG], "b": [RIGHT[0], RIGHT[1]]},
                      recipe=RECIPE, plans=plans, typed=typed, depths=[2, 4], say=lambda m: None,
                      call=_judge(seen))
    return res, seen, tmp_path


def _typed(text):
    return {"core": text, "translations": {}, "query_lang": PLANS[text]["query_lang"], "only": [],
            "main": None, "status": "off"}


def test_every_stage_has_its_own_row_translated_and_typed(run):
    res, _seen, _ = run
    names = set(res["rows"])
    for mode in ("translated", "typed"):
        for stage in ("BM25", "a", "b", "a + BM25", "b + BM25"):
            assert f"{stage} [{mode}]" in names
            assert {f"{stage} + rerank@2 [{mode}]", f"{stage} + rerank@4 [{mode}]"} <= names


def test_the_better_encoder_wins_alone_and_the_reranker_closes_the_gap(run):
    res, _seen, _ = run
    rows = res["rows"]
    assert rows["b [translated]"]["mrr@10"] == 1.0
    assert rows["a [translated]"]["mrr@10"] < rows["b [translated]"]["mrr@10"]
    # the reranker over all four books finds each question's book first
    assert rows["a + rerank@4 [translated]"]["hit@1"] == 1.0
    # over two it cannot reach a book "a" ranks lower than that
    assert rows["a + rerank@2 [translated]"]["hit@2"] <= rows["a [translated]"]["hit@2"]


def test_bm25_finds_by_words_in_each_language_group(run):
    res, _seen, _ = run
    assert res["rows"]["BM25 [translated]"]["hit@1"] == 1.0, "each question names its book's own words"
    assert res["rows"]["BM25 [typed]"]["hit@1"] == 1.0


def test_each_pair_is_judged_in_its_records_language(run):
    _res, seen, _ = run
    by_variant = {}
    for q, texts in seen:
        by_variant.setdefault(q, set()).update(t.split("\n")[0] for t in texts)
    # translated: a record in another language meets the question in its own
    assert by_variant["balena bianca"] <= {"I Malavoglia", "Pinocchio"}
    assert by_variant["wooden puppet"] <= {"Moby-Dick", "Walden"}


def test_the_pairs_ask_whether_the_encoder_pays_off_after_the_pipeline(run):
    res, _seen, _ = run
    assert "a + BM25 + rerank@4 [translated]  vs  BM25 + rerank@4 [translated]" in res["pairs"]
    assert "a [typed]  vs  b [typed]" in res["pairs"]
    assert "a + BM25 + rerank@2 [translated]  vs  b + BM25 + rerank@2 [translated]" in res["pairs"]
    assert "BM25 + rerank@4 [translated]  vs  BM25 + rerank@4 [typed]" in res["pairs"]
    assert {"mrr@10", "p@10", "mrr@5", "p@5"} <= set(res["pairs"]["a [typed]  vs  b [typed]"])
    assert set(res["by_language"]) == {"en", "it"}


def test_a_rerun_asks_neither_the_reranker_nor_the_plan_again(run, monkeypatch):
    res, _seen, tmp = run
    plans = pe.PlanStore(tmp / "plans.jsonl", lambda text: pytest.fail("planned again"))
    typed = pe.PlanStore(tmp / "typed.jsonl", lambda text: pytest.fail("planned again"))
    again = []
    res2 = pe.evaluate(tmp / "out", "set", DOCS, QUESTIONS,
                       {"a": [WRONG, WRONG], "b": [RIGHT[0], RIGHT[1]]},
                       recipe=RECIPE, plans=plans, typed=typed, depths=[2, 4], say=lambda m: None,
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


def test_typed_judges_every_pair_with_the_question_as_typed(run):
    """Without translation the Italian books meet "white whale" itself, and
    a pair both forms ask is scored once."""
    _res, seen, _ = run
    asked = [(q, t) for q, texts in seen for t in texts]
    titles = {(q, t.split("\n")[0]) for q, t in asked}
    assert ("white whale", "I Malavoglia") in titles or ("white whale", "Pinocchio") in titles
    assert len(asked) == len(set(asked)), "no pair asked twice"


def test_the_vector_leg_run_with_each_translation_is_its_own_row(tmp_path, monkeypatch):
    """The operator, 2026-11-02: "by using the translation model harrier is
    supposed to have a better score?" — "<encoder> per-language" embeds the
    plan's translations into the set's languages too, fused as variants of
    the one vector leg; translated rows only."""
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    asked = []

    def ranker(texts):
        asked.extend(texts)
        # a translation finds its book: "balena bianca" Moby-Dick, "wooden puppet" Pinocchio
        return [RIGHT[0] if t == "balena bianca" else RIGHT[1] for t in texts]
    plans = pe.PlanStore(tmp_path / "plans.jsonl", lambda text: dict(PLANS[text]))
    typed = pe.PlanStore(tmp_path / "typed.jsonl", lambda text: _typed(text))
    res = pe.evaluate(tmp_path / "out", "set", DOCS, QUESTIONS, {"a": [WRONG, WRONG]},
                      recipe=RECIPE, plans=plans, typed=typed, query_rankers={"a": ranker},
                      depths=[2], say=lambda m: None, call=_judge([]))
    rows = res["rows"]
    assert rows["a per-language [translated]"]["mrr@10"] > rows["a [translated]"]["mrr@10"]
    assert "a per-language [typed]" not in rows
    assert "a per-language + BM25 + rerank@2 [translated]" in rows
    assert sorted(asked) == ["balena bianca", "wooden puppet"], "only translations into held languages"
    assert "a per-language [translated]  vs  a [translated]" in res["pairs"]


def test_translated_only_embeds_the_question_in_the_sets_language_alone(tmp_path, monkeypatch):
    """The operator, 2026-11-02, on wikidata (its texts in English only): "does
    it make sense to translate the query always to english for bge?" —
    "<encoder> translated-only" embeds the question in the set's languages
    alone: the Italian question by its English translation only, the English
    question as typed."""
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    docs = [pe.Doc(lang="en", title="Moby-Dick", chunks=["the white whale and captain ahab at sea"]),
            pe.Doc(lang="en", title="Walden", chunks=["life in the woods by a pond"])]
    questions = [pe.Question(text="white whale", relevant=frozenset({0}), lang="en"),
                 pe.Question(text="balena bianca", relevant=frozenset({0}), lang="it")]
    plan = {"white whale": {"core": "white whale", "translations": {"it": "balena bianca"},
                            "query_lang": "en", "only": [], "main": None, "status": "ok"},
            "balena bianca": {"core": "balena bianca", "translations": {"en": "white whale"},
                              "query_lang": "it", "only": [], "main": None, "status": "ok"}}
    right, wrong = [(0, 0), (1, 0)], [(1, 0), (0, 0)]
    asked = []

    def ranker(texts):
        asked.extend(texts)
        return [right for _ in texts]

    def typed(text):
        return dict(plan[text], translations={}, status="off")
    res = pe.evaluate(tmp_path / "out", "set", docs, questions, {"a": [right, wrong]},
                      recipe=RECIPE, plans=pe.PlanStore(tmp_path / "p.jsonl", lambda t: dict(plan[t])),
                      typed=pe.PlanStore(tmp_path / "t.jsonl", typed), query_rankers={"a": ranker},
                      depths=[2], say=lambda m: None,
                      call=lambda recipe, q, texts, timeout: [0.0] * len(texts))
    rows = res["rows"]
    assert rows["a [translated]"]["mrr@10"] == pytest.approx(0.75, abs=1e-4)
    assert rows["a translated-only [translated]"]["mrr@10"] == pytest.approx(1.0, abs=1e-4)
    assert asked == ["white whale"], "the English question is not translated, the Italian one is"
    assert "a translated-only [typed]" not in rows
    assert "a translated-only + BM25 + rerank@2 [translated]" in rows
    assert "a translated-only [translated]  vs  a [translated]" in res["pairs"]
    assert "a per-language [translated]  vs  a translated-only [translated]" in res["pairs"]


def test_a_query_side_that_does_not_reproduce_the_rows_vectors_is_refused(tmp_path):
    import numpy as np
    stored = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    ranker = pe.QueryRanker(tmp_path / "q.jsonl", lambda texts: [[0.0, 1.0]] * len(texts),
                            np.eye(2), [(0, 0), (1, 0)], check=(["one", "two"], stored))
    with pytest.raises(SystemExit, match="not the one the row was scored with"):
        ranker(["uno"])
    good = pe.QueryRanker(tmp_path / "g.jsonl",
                          lambda texts: [{"one": [1.0, 0.0], "two": [0.0, 1.0]}.get(t, [0.0, 1.0])
                                         for t in texts],
                          np.eye(2), [(0, 0), (1, 0)], check=(["one", "two"], stored))
    assert good(["uno"]) == [[(1, 0), (0, 0)]]
    again = pe.QueryRanker(tmp_path / "g.jsonl", lambda texts: pytest.fail("embedded again"),
                           np.eye(2), [(0, 0), (1, 0)])
    assert again(["uno"]) == [[(1, 0), (0, 0)]], "kept by text"
