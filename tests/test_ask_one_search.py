"""One Ask question, one search (München, 2026-10-22, measured on the node:
the page's /ask/query searched, then /v1/chat/completions searched again —
retrieval and rerank twice, ~10.5 s of a 45 s question). The page's result
is kept under a random id and the answer is written from it, so the
answer's [n] are the page's hits by construction."""
import pytest
from fastapi.testclient import TestClient

from app import api, llm, query_service, web


@pytest.fixture
def client(isolated_config, monkeypatch):
    from tests.test_web_pages import _patch_hash
    _patch_hash(monkeypatch)
    calls = []

    def fake_search(collections, query, *a, **kw):
        calls.append(query)
        return {"hits": [], "catalog": [], "timings": {"total": 1.0}}
    monkeypatch.setattr(query_service, "run_search", fake_search)
    monkeypatch.setattr(api, "_run_search", fake_search)

    def words(messages, **kw):
        yield "an answer"

        class End:
            whole = True
        return End()
    monkeypatch.setattr(llm, "chat_stream", words)
    with TestClient(api.app) as c:
        yield c, calls


def _answer(c, q, sid):
    return c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": q}],
                                                "stream": True, "search_id": sid})


def test_the_answer_is_written_from_the_pages_search(client):
    c, calls = client
    r = c.post("/ask/query", json={"query": "windmills"})
    sid = r.headers["x-louise-search"]
    assert r.status_code == 200 and sid and calls == ["windmills"]
    assert "an answer" in _answer(c, "windmills", sid).text
    assert calls == ["windmills"], "one question, one search"


def test_another_question_or_an_unknown_id_searches_as_before(client):
    c, calls = client
    sid = c.post("/ask/query", json={"query": "windmills"}).headers["x-louise-search"]
    _answer(c, "giants", sid)
    _answer(c, "windmills", "nope")
    assert calls == ["windmills", "giants", "windmills"]


def test_a_kept_search_expires(client, monkeypatch):
    c, calls = client
    sid = c.post("/ask/query", json={"query": "windmills"}).headers["x-louise-search"]
    monkeypatch.setattr(web, "SEARCH_TTL_S", -1.0)
    assert web.kept_search(sid, "windmills") is None
