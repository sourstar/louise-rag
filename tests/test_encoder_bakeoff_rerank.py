"""The bake-off's `rerank` on a gutenberg pool: the pool's chunks as the
search's records, each row's ranking handed to tools/measure/pipeline_eval.py
(the operator, 2026-11-02: "Add BM25 fusion and translations too, make
separated scores")."""
from __future__ import annotations

import json

import numpy as np
import pytest

from app import collections as collections_mod
from app import retrieval
from tools import encoder_bakeoff as bake
from tools.measure import pipeline_eval as pe

RECIPE = collections_mod.RerankRecipe(url="http://rerank/v1/rerank", api="jina", model="rr")


def _pool(tmp_path):
    """Books 1-4, two chunks each; q0 asks for book 2 ("whale"), q1 for book
    4 ("puppet"). The row ranks book 1, 3, 2, 4 for both."""
    out = tmp_path / "pool"
    out.mkdir()
    words = {1: "pond woods", 2: "whale sea", 3: "fishermen boat", 4: "puppet wood"}
    ids = [f"gutenberg:{b}:{s}:1" for b in (1, 2, 3, 4) for s in (0, 1)]
    with (out / "corpus.jsonl").open("w") as fh:
        for i in ids:
            b = int(i.split(":")[1])
            fh.write(json.dumps({"_id": i, "title": "", "text": f"{words[b]} part {i[-3]}"}) + "\n")
    (out / "queries.jsonl").write_text(
        json.dumps({"_id": "q0", "text": "whale"}) + "\n" + json.dumps({"_id": "q1", "text": "puppet"}) + "\n")
    with (out / "qrels.jsonl").open("w") as fh:
        for q, b in (("q0", 2), ("q1", 4)):
            for s in (0, 1):
                fh.write(json.dumps({"query-id": q, "corpus-id": f"gutenberg:{b}:{s}:1", "score": 1}) + "\n")
    (out / "SOURCE.json").write_text(json.dumps({"suite": "pool"}))
    run = out / "m"
    run.mkdir()
    np.save(run / "corpus.npy", np.eye(8, dtype=np.float32))
    np.save(run / "queries-bare.npy", np.asarray([[8, 7, 4, 3, 6, 5, 2, 1]] * 2, dtype=np.float32))
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}))
    return out


def _heads(records):
    return {r: {"head": f"Title {r.split(':')[1]}", "lang": "en"} for r in records}


def _plans(out):
    return pe.PlanStore(out / "plans.jsonl", lambda text: {
        "core": text, "translations": {}, "query_lang": "en", "only": [], "main": None, "status": "ok"})


def test_the_pool_runs_through_the_whole_search_stage_by_stage(tmp_path, monkeypatch):
    out = _pool(tmp_path)
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    sent = []

    def call(recipe, query, texts, timeout):
        sent.extend(texts)
        return [10.0 if query in t else 0.0 for t in texts]
    typed = pe.PlanStore(out / "typed.jsonl", lambda text: {
        "core": text, "translations": {}, "query_lang": "en", "only": [], "main": None, "status": "off"})
    assert bake.rerank(out, 20_000, "gutenberg", RECIPE, _heads, plans=_plans(out), typed=typed,
                       depths=[2, 4], call=call) == 0
    res = json.loads((out / bake.RERANKED / "pipeline.json").read_text())
    rows = res["rows"]
    # the row alone: book 2 third, book 4 fourth
    assert rows["m [translated]"]["mrr@10"] == pytest.approx((1 / 3 + 1 / 4) / 2, abs=1e-4)
    assert rows["BM25 [translated]"]["hit@1"] == 1.0, "each question names its book's words"
    assert rows["m + BM25 [translated]"]["mrr@10"] > rows["m [translated]"]["mrr@10"]
    assert rows["m + rerank@4 [typed]"]["hit@1"] == 1.0
    assert all(t.startswith("Title ") for t in sent), "each chunk under its book's title"


def test_rerank_reads_records_from_a_pool_only(tmp_path):
    (tmp_path / "SOURCE.json").write_text(json.dumps({"suite": "bsard"}))
    with pytest.raises(SystemExit, match="pool"):
        bake.rerank(tmp_path, 20_000, "c", RECIPE, _heads, plans=None)
