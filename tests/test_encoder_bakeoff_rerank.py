"""The bake-off's reranker stage (tools/encoder_bakeoff.py `rerank`; the
operator, 2026-11-02: "About gutenberg, show me again the comparison and
evaluate the effects with a reranker"): each row's chunk ranking as the
search's record list, its top records re-ordered as `_rerank_list` orders
them, every (query, chunk) pair scored once and kept."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app import collections as collections_mod
from app import retrieval
from tools import encoder_bakeoff as bake

RECIPE = collections_mod.RerankRecipe(url="http://rerank/v1/rerank", api="jina", model="rr")


def _pool(tmp_path):
    """Two queries over books 1-4, two chunks each. Query 0 is answered by
    book 2, query 1 by book 4. One row ranks book 1 first for both."""
    out = tmp_path / "pool"
    out.mkdir()
    ids = [f"gutenberg:{b}:{s}:1" for b in (1, 2, 3, 4) for s in (0, 1)]
    with (out / "corpus.jsonl").open("w") as fh:
        for i in ids:
            fh.write(json.dumps({"_id": i, "title": "", "text": f"text of {i}"}) + "\n")
    (out / "queries.jsonl").write_text(
        json.dumps({"_id": "q0", "text": "two"}) + "\n" + json.dumps({"_id": "q1", "text": "four"}) + "\n")
    with (out / "qrels.jsonl").open("w") as fh:
        for q, b in (("q0", 2), ("q1", 4)):
            for s in (0, 1):
                fh.write(json.dumps({"query-id": q, "corpus-id": f"gutenberg:{b}:{s}:1", "score": 1}) + "\n")
    (out / "SOURCE.json").write_text(json.dumps({"suite": "pool"}))
    run = out / "m"
    run.mkdir()
    # corpus positions 0..7 = books 1,1,2,2,3,3,4,4; the row ranks by
    # descending position weight: book 1 first, then 3, 2, 4
    doc = np.zeros((8, 8), dtype=np.float32)
    for i in range(8):
        doc[i, i] = 1.0
    qry = np.zeros((2, 8), dtype=np.float32)
    for q in range(2):
        qry[q] = [8, 7, 4, 3, 6, 5, 2, 1]
    np.save(run / "corpus.npy", doc)
    np.save(run / "queries-bare.npy", qry)
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}))
    return out, ids


def _reranker(calls):
    """Scores a candidate by its book: the query's own book highest. Each
    text carries its head line ("Title N") and its chunk's text."""
    def call(recipe, query, texts, timeout):
        calls.append((query, list(texts)))
        want = {"two": "2", "four": "4"}[query]
        return [10.0 if f"gutenberg:{want}:" in t else float(t.count("Title")) for t in texts]
    return call


def _heads(records):
    """Books 1 and 2 are English, 3 and 4 Italian."""
    return {r: {"head": f"Title {r.split(':')[1]}", "lang": "en" if int(r.split(":")[1]) < 3 else "it"}
            for r in records}


def test_a_ranking_becomes_records_each_once_with_its_first_chunks():
    ids = ["g:1:0:1", "g:1:1:1", "g:2:0:1", "g:1:2:1", "g:3:0:1"]
    assert bake.records_of([0, 2, 1, 3, 4], ids, depth=2, per_record=2) == [
        ("g:1", [0, 1]), ("g:2", [2])], "book 3 is past the depth; book 1 keeps two chunks"


def test_the_reranker_reorders_the_records_and_both_rows_are_record_level(tmp_path, monkeypatch):
    out, _ids = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    calls = []
    assert bake.rerank(out, 20_000, RECIPE, _heads, depths=[50], call=_reranker(calls)) == 0
    before = json.loads((out / "m" / "records-bare.json").read_text())
    after = json.loads((out / "m" / "reranked-d50-bare.json").read_text())
    # records before: 1, 3, 2, 4 -> query 0's book 2 third, query 1's book 4 fourth
    assert before["mrr@10"] == pytest.approx((1 / 3 + 1 / 4) / 2)
    assert after["mrr@10"] == pytest.approx(1.0), "the reranker put each query's book first"
    # every candidate went with its record's title above its chunk, two chunks a record
    sent = calls[0][1]
    assert len(sent) == 8 and all(t.startswith("Title ") and "\n\ntext of gutenberg:" in t for t in sent)


def test_a_pair_is_scored_once_and_a_rerun_asks_nothing(tmp_path, monkeypatch):
    out, _ = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    calls = []
    bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker(calls))
    first = len(calls)
    bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker(calls))
    assert first == 2 and len(calls) == first, "the kept scores answer the rerun"


def test_another_reranker_in_the_same_place_is_refused(tmp_path, monkeypatch):
    out, _ = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker([]))
    other = collections_mod.RerankRecipe(url="http://elsewhere/v1/rerank", api="jina", model="rr")
    with pytest.raises(SystemExit, match="different settings"):
        bake.rerank(out, 20_000, other, _heads, call=_reranker([]))


def test_the_report_shows_the_rows_before_and_after(tmp_path, monkeypatch, capsys):
    out, _ = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker([]))
    capsys.readouterr()
    bake.report(out)
    shown = capsys.readouterr().out
    assert "m (records)" in shown and "m + rerank@50" in shown


def test_rerank_reads_records_from_a_pool_only(tmp_path):
    (tmp_path / "SOURCE.json").write_text(json.dumps({"suite": "bsard"}))
    with pytest.raises(SystemExit, match="pool"):
        bake.rerank(tmp_path, 20_000, RECIPE, _heads)


def test_a_shallower_depth_reranks_only_its_records_and_asks_nothing_more(tmp_path, monkeypatch):
    """The operator, 2026-11-02: "5 results reranked over 50 results, more
    likely over 100". Depth 2: the reranker sees books 1 and 3 only, so
    query 0's book 2 (third) stays third; depth 4 brings it first. One
    request per query serves both."""
    out, _ids = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    calls = []
    bake.rerank(out, 20_000, RECIPE, _heads, depths=[4, 2], call=_reranker(calls))
    assert len(calls) == 2, "one call per query, the deepest list's pairs"
    d2 = json.loads((out / "m" / "reranked-d2-bare.json").read_text())
    d4 = json.loads((out / "m" / "reranked-d4-bare.json").read_text())
    assert d2["hit@2"] == 0.0 and d2["mrr@5"] == pytest.approx((1 / 3 + 1 / 4) / 2)
    assert d4["mrr@5"] == pytest.approx(1.0) and d4["hit@4"] == 1.0


def test_each_pair_is_judged_against_the_question_in_its_records_language(tmp_path, monkeypatch):
    """As `_rerank_list` does with the plan's translations: Italian books
    against the Italian variant, English ones against the question typed;
    one call per variant, the plan asked once per question and kept."""
    out, _ids = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    asked = []

    def make(text):
        asked.append(text)
        return text, {"it": {"two": "due", "four": "quattro"}[text]}
    plans = bake.PlanCache(out / "rerank" / "plans.jsonl", make)
    seen = []

    def call(recipe, query, texts, timeout):
        seen.append((query, sorted({t.split(chr(10))[0] for t in texts})))
        return [1.0] * len(texts)
    bake.rerank(out, 20_000, RECIPE, _heads, depths=[50], call=call, plans=plans)
    assert ("two", ["Title 1", "Title 2"]) in seen and ("due", ["Title 3", "Title 4"]) in seen
    assert ("four", ["Title 1", "Title 2"]) in seen and ("quattro", ["Title 3", "Title 4"]) in seen
    assert asked == ["two", "four"]
    again = bake.PlanCache(out / "rerank" / "plans.jsonl", lambda text: pytest.fail("asked again"))
    assert again.get(0, "two") == ("two", {"it": "due"})


def test_a_translated_run_never_reads_an_untranslated_cache(tmp_path, monkeypatch):
    out, _ids = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker([]))
    plans = bake.PlanCache(out / "rerank" / "plans.jsonl", lambda text: (text, {}))
    with pytest.raises(SystemExit, match="different settings"):
        bake.rerank(out, 20_000, RECIPE, _heads, call=_reranker([]), plans=plans)
