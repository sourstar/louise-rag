"""Every rerank fallback carries its reason (05 §5.6, amended 2026-10-10).

Found live on 906f06b: 4 of 980 searches kept the fused order and none of
them said why — `_rerank_list`'s generic `except` returned no reason, so
`rerank_withheld` was missing exactly where a reader needed it. Now a
timeout, a refusal, an unreachable reranker, an unreadable answer and a
partial answer each say which they were."""
import httpx
import pytest

from app import collections as collections_mod, retrieval
from app.rage import dispatch

RECIPE = collections_mod.RerankRecipe(url="http://reranker/v1/rerank", api="jina", model="")
HITS = [{"chunk_id": f"g:{i}:0:1", "book_id": i, "text": f"t{i}"} for i in range(3)]
REQ = httpx.Request("POST", RECIPE.url)


def _raise(exc):
    def post(*a, **k):
        raise exc
    return post


@pytest.mark.parametrize("post, reason", [
    (_raise(httpx.ReadTimeout("slow", request=REQ)),
     "the reranker did not answer within 8 s"),
    (lambda *a, **k: httpx.Response(503, request=REQ, text="busy"),
     "the reranker refused the request (HTTP 503: busy), and again when asked once more"),
    (lambda *a, **k: httpx.Response(400, request=REQ, text=""),
     "the reranker refused the request (HTTP 400)"),
    (_raise(httpx.ConnectError("refused", request=REQ)),
     "the reranker could not be reached"),
    (_raise(dispatch.GateTimeout("rerank: not admitted within 8s")),
     "the reranker was busy with other work for the whole 8 s budget"),
    (lambda *a, **k: httpx.Response(200, request=REQ, json={"results": [{"index": 0}]}),
     "the reranker's answer could not be read (KeyError)"),
    (lambda *a, **k: httpx.Response(200, request=REQ, json={"results": [
        {"index": 0, "relevance_score": 1.0}, {"index": 1, "relevance_score": 0.5}]}),
     "the reranker answered for 2 of 3 candidates"),
    (_raise(RuntimeError("boom")), "the reranker failed (RuntimeError)"),
])
def test_each_fallback_says_why(monkeypatch, post, reason):
    monkeypatch.setattr(retrieval.httpc, "post", post)
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 8.0, 50)
    assert (ranked, ok) == (HITS, False), "the fused order stands"
    assert why == reason


def test_a_reranked_list_carries_no_reason(monkeypatch):
    monkeypatch.setattr(retrieval.httpc, "post", lambda *a, **k: httpx.Response(
        200, request=REQ, json={"results": [{"index": i, "relevance_score": float(i)}
                                            for i in range(3)]}))
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 8.0, 50)
    assert ok is True and why is None
    assert [r["book_id"] for r in ranked] == [2, 1, 0]


def test_no_reranker_is_not_a_fallback():
    ranked, ok, why = retrieval._reranked("q", list(HITS), None, 8.0, 50)
    assert (ok, why) == (False, None), "a node that runs no reranker has nothing to explain"


def test_the_reason_never_carries_the_request():
    secret = "Bearer s3cr3t"
    e = httpx.HTTPStatusError(f"401 for {secret}", request=REQ,
                              response=httpx.Response(401, request=REQ))
    assert secret not in retrieval.rerank_failure(e, 8.0)
    assert secret not in retrieval.rerank_failure(RuntimeError(secret), 8.0)


# ---- a server error is asked again once (2026-10-25: 130 of 5,792 night calls answered 500) ----

def _sequence(*answers):
    calls = []

    def post(url, json=None, timeout=None, **_kw):     # noqa: A002
        calls.append(timeout)
        a = answers[min(len(calls), len(answers)) - 1]
        return a() if callable(a) else a
    return post, calls


OK = httpx.Response(200, request=REQ, json={"results": [{"index": i, "relevance_score": float(i)}
                                                        for i in range(3)]})
LOCALAI_500 = httpx.Response(500, request=REQ, json={"error": {
    "code": 500, "message": "rpc error: code = Unknown desc = failed to decode the batch", "type": ""}})


def test_a_transient_500_is_asked_again_and_the_list_is_reranked(monkeypatch):
    post, calls = _sequence(LOCALAI_500, OK)
    monkeypatch.setattr(retrieval.httpc, "post", post)
    monkeypatch.setattr(retrieval, "RERANK_RETRY_PAUSE_S", 0.01)
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 8.0, 50)
    assert ok is True and why is None and [r["book_id"] for r in ranked] == [2, 1, 0]
    assert len(calls) == 2 and calls[1] < calls[0] <= 8.0, "the second try has what is left"


def test_a_500_twice_says_what_the_server_said(monkeypatch):
    post, calls = _sequence(LOCALAI_500)
    monkeypatch.setattr(retrieval.httpc, "post", post)
    monkeypatch.setattr(retrieval, "RERANK_RETRY_PAUSE_S", 0.01)
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 8.0, 50)
    assert (ranked, ok) == (HITS, False) and len(calls) == 2
    assert why == ("the reranker refused the request (HTTP 500: rpc error: code = Unknown desc = "
                   "failed to decode the batch), and again when asked once more")


def test_no_second_try_without_budget_and_none_for_a_client_error(monkeypatch):
    post, calls = _sequence(LOCALAI_500, OK)
    monkeypatch.setattr(retrieval.httpc, "post", post)
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 1.1, 50)
    assert ok is False and len(calls) == 1, "under a second left after the pause: not asked again"
    assert why.startswith("the reranker refused the request (HTTP 500:") and "again" not in why
    post, calls = _sequence(httpx.Response(422, request=REQ, json={"error": {"message": "bad"}}), OK)
    monkeypatch.setattr(retrieval.httpc, "post", post)
    ranked, ok, why = retrieval._reranked("q", list(HITS), RECIPE, 8.0, 50)
    assert ok is False and len(calls) == 1 and why == "the reranker refused the request (HTTP 422: bad)"
