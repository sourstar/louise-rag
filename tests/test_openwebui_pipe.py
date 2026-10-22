"""deploy/openwebui/louise_pipe.py — Louise as an Open WebUI pipe (operator,
2026-10-22: "an OpenWebUI equivalent of our ask page … selecting knowledge
bases equivalent to collections and annotations").

The pipe runs inside Open WebUI, so it is tested here against Louise's own
routes through httpx's ASGI transport: the models it lists come from
`/v1/models`, one question is ONE search (`sources: true`), the hits become
Open WebUI sources numbered as the answer's [n], and the person's choices
of collections and annotations reach the search."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app import api, config, users

SPEC = importlib.util.spec_from_file_location(
    "louise_pipe", Path(__file__).resolve().parents[1] / "deploy" / "openwebui" / "louise_pipe.py")
pipe_mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipe_mod)

HITS = [{"chunk_id": "gutenberg:996:3:1", "uid": "gutenberg:996", "book_id": 996, "title": "Don Quixote",
         "authors": "Cervantes", "lang": "en", "source_collection": "gutenberg", "text": "the windmills…",
         "editions": [{"uid": "gutenberg:2000", "title": "Don Quijote", "lang": "es", "held": True}],
         "annotations": [{"type": "translation-of", "state": "current",
                          "record": {"uid": "gutenberg:2000", "title": "Don Quijote", "lang": "es"}}]},
        {"chunk_id": "pubmed:7:0:1", "uid": "pubmed:7", "book_id": None, "title": "On windmills",
         "lang": "en", "source_collection": "pubmed", "text": "abstract", "origin_url": "https://pubmed/7"}]


class Louise(pipe_mod.Pipe):
    """The pipe, its HTTP going to this process's app instead of a network."""
    def _client(self, timeout):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://louise",
                                 timeout=timeout)

    def _sync_client(self):
        from fastapi.testclient import TestClient
        return TestClient(api.app)


@pytest.fixture
def louise(isolated_config, monkeypatch):
    from tests.test_web_pages import _patch_hash
    _patch_hash(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    calls = []

    async def fake_search(request, fn, collections, query, *a, **kw):
        calls.append({"collections": collections, "query": query, **kw})
        return {"hits": [dict(h) for h in HITS], "catalog": [], "timings": {"total": 1.0}}
    monkeypatch.setattr(api.cancel, "while_connected", fake_search)
    monkeypatch.setattr(api.llm, "chat_stream", lambda messages, **kw: _words("Tilting at windmills [1]."))
    p = Louise()
    p.valves = p.Valves(LOUISE_URL="http://louise", PUBLIC_URL="https://louise.example")
    return p, calls


def _words(text):
    for w in text.split(" "):
        yield w + " "

    class End:
        whole = True
    return End()


def run(p, body, uv=None, task=None):
    events = []

    async def emit(e):
        events.append(e)

    async def go():
        out = await p.pipe(body, __user__={"valves": uv or p.UserValves()}, __event_emitter__=emit, __task__=task)
        if isinstance(out, str):
            return out
        return "".join([t async for t in out])
    return asyncio.run(go()), events


def test_the_models_are_louises_collections(louise):
    p, _ = louise
    ids = [m["id"] for m in p.pipes()]
    assert ids[0] == "all" and len(ids) >= 2 and "unreachable" not in ids


def test_one_question_is_one_search_and_its_hits_are_the_sources(louise):
    p, calls = louise
    text, events = run(p, {"model": "louise_pipe.all", "messages": [{"role": "user", "content": "windmills?"}]})
    assert text.split() == "Tilting at windmills [1].".split(), "the answer, streamed through"
    assert len(calls) == 1, "one search: the hits come back with the answer"
    cites = [e["data"] for e in events if e["type"] == "citation"]
    assert [c["source"]["name"] for c in cites] == ["[1] Don Quixote — gutenberg (en)",
                                                     "[2] On windmills — pubmed (en)"]
    assert cites[0]["source"]["url"] == "https://louise.example/read/gutenberg/996?hl=gutenberg:996:3:1"
    assert cites[1]["source"]["url"] == "https://pubmed/7", "no reader page: the origin"
    assert "Also in: Don Quijote (es)" in cites[0]["document"][0]
    assert "translation-of — Don Quijote (es)" in cites[0]["document"][0]


def test_the_persons_choices_reach_the_search(louise):
    p, calls = louise
    uv = p.UserValves(COLLECTIONS="gutenberg, pubmed", ANNOTATIONS="none", ANSWER_IN="IT", RESULTS=5)
    run(p, {"model": "louise_pipe.gutenberg", "messages": [{"role": "user", "content": "q"}]}, uv)
    assert calls[-1]["query"] == "q" and calls[-1]["collections"] == ["gutenberg", "pubmed"]
    assert calls[-1]["annotations"] == [] and calls[-1]["show_in"] == "it"
    body = p.request_body({"model": "louise_pipe.gutenberg"}, p.UserValves())
    assert body["collections"] == ["gutenberg"] and "annotations" not in body, "the model's collection; all annotations"
    assert "collections" not in p.request_body({"model": "louise_pipe.all"}, p.UserValves())


def test_a_refused_key_says_what_to_set(louise):
    p, _ = louise
    users.add("someone")                    # Louise now needs a key, and the pipe has none
    text, events = run(p, {"model": "louise_pipe.all", "messages": [{"role": "user", "content": "q"}]})
    assert "LOUISE_TOKEN" in text and "louise user add openwebui" in text
    _, token = users.add("openwebui")
    p.valves = p.Valves(LOUISE_URL="http://louise", LOUISE_TOKEN=token)
    text, _ = run(p, {"model": "louise_pipe.all", "messages": [{"role": "user", "content": "q"}]})
    assert "windmills" in text


def test_open_webuis_own_tasks_never_search(louise):
    p, calls = louise
    out, _ = run(p, {"model": "louise_pipe.all", "messages": [{"role": "user", "content": "Who tilts?"}]},
                 task="title_generation")
    assert json.loads(out) == {"title": "Who tilts?"} and calls == []


def test_v1_returns_the_sources_only_when_asked(louise):
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    plain = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}], "stream": True})
    assert "louise_sources" not in plain.text, "an OpenAI client gets what it always got"
    asked = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "q"}],
                                                 "stream": True, "sources": True})
    first = json.loads(asked.text.split("\n\n")[0][len("data: "):])
    assert [s["n"] for s in first["louise_sources"]] == [1, 2] and first["choices"][0]["delta"] == {}
