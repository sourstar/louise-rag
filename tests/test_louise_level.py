"""Every request to a model endpoint carries its level, and the chat client
speaks either protocol (migration-plan step 13e; docs/09-formats.md §9.12,
*the request's level*; docs/notes/rage.md §4, §11).

`Louise-Level` is what lets RAGE hold the card's clock while a person waits
without anything being written anywhere: it sees every model request itself.
`CHAT_API=openai` is what lets the chat model leave Ollama for RAGE — the same
file, behind llama-server — and come back by switching one variable.
"""
import json

import pytest

from app import collections as collections_mod, config, dispatch, embeddings, llm, \
    retrieval, translate
from app.dispatch import Level


@pytest.fixture
def posts(monkeypatch):
    """Every `httpx.post` the clients make, answered by whatever fits."""
    seen = []

    class Answer:
        status_code = 200
        text = ""

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            pass

        def json(self):
            if self.url.endswith("/v1/completions"):        # the translator, 15e-2
                return {"choices": [{"text": '{"translations": ["ciao"]}'}]}
            if "rerank" in self.url:
                return {"results": [{"index": 0, "relevance_score": 0.9}]}
            if self.url.endswith("/v1/embeddings"):
                return {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            return {"embeddings": [[1.0, 0.0]]}

    def post(url, **kwargs):
        seen.append((url, kwargs))
        return Answer(url)

    import httpx

    from app import httpc
    monkeypatch.setattr(httpx, "post", post)
    # the embedding and rerank clients go through the pooled client now, which
    # exists so that one TLS context and one connection serve every request
    # (app/httpc.py); the header they carry is what this file is about
    monkeypatch.setattr(httpc, "post", post)
    return seen


def _level(call):
    return call[1]["headers"][dispatch.LEVEL_HEADER]


def test_an_embedding_carries_the_level_of_whoever_asks(posts, isolated_config):
    recipe = collections_mod.recipe_for(collections_mod.default_collection())
    embeddings._embed_texts(["a question"], recipe=recipe)
    with dispatch.working_as(Level.CORPUS, "gutenberg"):
        embeddings._embed_texts(["a chunk"], recipe=recipe)
    assert [_level(c) for c in posts] == ["query", "corpus"], \
        "the web's unmarked call has a person behind it; the worker's is background"


def test_the_openai_embedding_path_carries_it_too(posts, isolated_config):
    import dataclasses
    recipe = dataclasses.replace(
        collections_mod.recipe_for(collections_mod.default_collection()),
        url="http://nas:30437/upstream/bge-m3", api="openai")
    embeddings._embed_texts(["x"], recipe=recipe)
    assert posts[0][0] == "http://nas:30437/upstream/bge-m3/v1/embeddings"
    assert _level(posts[0]) == "query"


def test_a_rerank_and_a_translation_say_what_they_are(posts, monkeypatch, isolated_config):
    recipe = collections_mod.RerankRecipe(url="http://nas:30437/upstream/bge-reranker/v1/rerank",
                                          api="jina")
    hits, reranked = retrieval._rerank_list("whales", [{"text": "a whale"}], recipe, 5.0)
    assert reranked and _level(posts[-1]) == "rerank"

    monkeypatch.setattr(config, "TRANSLATE_URL", "http://nas:30437/upstream/madlad")
    assert translate.translate([("hello", "it")]) == ["ciao"]
    assert _level(posts[-1]) == "translate"


# ---- the chat client, in both protocols -------------------------------------------

class _FakeStream:
    def __init__(self, lines):
        self.lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        yield from self.lines


@pytest.fixture
def chat_server(monkeypatch, isolated_config):
    seen = {}

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def post(self, url, json=None, headers=None):
            seen.update(url=url, body=json, headers=headers)

            class R:
                def raise_for_status(self_inner):
                    pass

                def json(self_inner):
                    return seen["answer"]
            return R()

        def stream(self, method, url, json=None, headers=None):
            seen.update(url=url, body=json, headers=headers)
            return _FakeStream(seen["lines"])

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")
    return seen


def test_ollama_stays_exactly_what_it_was(chat_server, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "ollama")
    chat_server["answer"] = {"message": {"content": "<think>hm</think>Ahab."}}
    assert llm.chat([{"role": "user", "content": "who?"}], max_tokens=50, num_ctx=4096) == "Ahab."
    assert chat_server["url"] == "http://chat/api/chat"
    body = chat_server["body"]
    assert body["think"] is False and body["options"] == {
        "temperature": 0.3, "num_ctx": 4096, "num_predict": 50}
    assert chat_server["headers"] == {"Louise-Level": "chat"}


def test_openai_is_what_rage_serves(chat_server, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", "http://nas:30437/upstream/chat")
    chat_server["answer"] = {"choices": [{"message": {"content": "Ahab."}}]}
    assert llm.chat([{"role": "user", "content": "who?"}], temperature=0.0,
                    max_tokens=50, level=Level.TRANSLATE) == "Ahab."
    assert chat_server["url"] == "http://nas:30437/upstream/chat/v1/chat/completions"
    body = chat_server["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}, \
        "thinking off through the model's own template"
    assert body["max_tokens"] == 50 and body["temperature"] == 0.0 and body["stream"] is False
    assert "options" not in body and "num_ctx" not in json.dumps(body), \
        "the context size is the server's, fixed at launch"
    assert chat_server["headers"] == {"Louise-Level": "translate"}, \
        "the chat model standing in for the translation model says so"


def test_a_streamed_answer_is_read_in_either_protocol(chat_server, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "ollama")
    chat_server["lines"] = ['{"message": {"content": "Call me "}}', "",
                            '{"message": {"content": "Ishmael."}, "done": true}',
                            '{"message": {"content": "never read"}}']
    assert "".join(llm.chat_stream([{"role": "user", "content": "?"}])) == "Call me Ishmael."
    assert chat_server["headers"] == {"Louise-Level": "chat"}

    monkeypatch.setattr(config, "CHAT_API", "openai")
    chat_server["lines"] = [": keep-alive", 'data: {"choices": [{"delta": {"role": "assistant"}}]}',
                            'data: {"choices": [{"delta": {"content": "Call me "}}]}',
                            "data: not json",
                            'data: {"choices": [{"delta": {"content": "Ishmael."}}]}',
                            "data: [DONE]",
                            'data: {"choices": [{"delta": {"content": "never read"}}]}']
    assert "".join(llm.chat_stream([{"role": "user", "content": "?"}],
                                   max_tokens=None)) == "Call me Ishmael."
    assert chat_server["url"].endswith("/v1/chat/completions")
    assert "max_tokens" not in chat_server["body"] and chat_server["body"]["stream"] is True


def test_the_levels_header_names_are_the_specs():
    assert dispatch.LEVEL_HEADER == "Louise-Level"
    assert [l.name.lower() for l in Level] == ["corpus", "translate", "chat", "rerank", "query"]
