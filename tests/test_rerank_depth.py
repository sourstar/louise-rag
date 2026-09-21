"""The rerank depth reaches 300, and its time budget grows with it.

Operator, 2026-09-21, from the Ask tab: "I cannot increment the rerank from
above 100" — then "up to 300 per leg, scale the timeout proportionally"
(docs/05 §5.1, §5.4, §5.6 and 07 §7.8, amended in 336a5ee). The results per
list were not asked to change and keep their own cap of 100.
"""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import api, platform_config, retrieval, web


@pytest.fixture
def budget_8s(monkeypatch):
    monkeypatch.setattr(platform_config, "load",
                        lambda: SimpleNamespace(ask=SimpleNamespace(latency_budget_ms=8000)))


@pytest.mark.parametrize("depth, seconds", [
    (50, 8.0),        # the default depth: the budget itself
    (100, 16.0),
    (300, 48.0),      # six times as long, as 05 §5.6 now says
    (10, 8.0),        # never less: a squeezed budget only means more fused orders
])
def test_the_budget_is_given_in_proportion_to_the_depth(budget_8s, depth, seconds):
    assert retrieval._rerank_budget_s(depth) == pytest.approx(seconds)


def test_a_search_hands_the_reranker_the_budget_for_its_own_depth(budget_8s, monkeypatch):
    seen: list = []
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])
    monkeypatch.setattr(retrieval, "_search_one_collection",
                        lambda con, c, q, k, mode, tr, ids, plan=None, depth=0: {
                            "catalog": [], "merged": [], "translations": {},
                            "timings": {"catalog": 0.0}, "scope": {}})
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, hits, q: hits)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue", lambda con, c, hits: None)
    monkeypatch.setattr(retrieval, "_rerank_list",
                        lambda q, hits, recipe, budget_s, depth: seen.append((depth, budget_s))
                        or (hits, False))
    collection = SimpleNamespace(name="a", plugin="gutenberg")
    retrieval.search(None, "q", k=20, collection=collection, translate=False, rerank_depth=300)
    assert seen == [(300, pytest.approx(48.0))]


@pytest.mark.parametrize("model", [api.SearchBody, api.ChatBody, web.AskQueryBody])
def test_every_way_in_takes_a_depth_of_300_and_no_more(model):
    required = {"query": "q"} if "query" in model.model_fields else {
        "messages": [{"role": "user", "content": "q"}]}
    assert model(**required, rerank_depth=300).rerank_depth == 300
    with pytest.raises(ValidationError):
        model(**required, rerank_depth=301)


@pytest.mark.parametrize("model", [api.ChatBody, web.AskQueryBody])
def test_the_results_per_list_keep_their_own_cap_of_100(model):
    required = {"query": "q"} if "query" in model.model_fields else {
        "messages": [{"role": "user", "content": "q"}]}
    with pytest.raises(ValidationError):
        model(**required, limit=101)


def test_the_ask_page_offers_300_for_the_depth_and_100_for_the_results(isolated_config,
                                                                       monkeypatch):
    from fastapi.testclient import TestClient
    page = TestClient(api.app).get("/ask").text
    assert 'id="rerank-depth" type="number" min="1" max="300"' in page
    assert 'id="limit" type="number" min="1" max="100"' in page
