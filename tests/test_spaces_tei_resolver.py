"""TEI model-hash resolution: openai-api recipes confirm their space pin
from GET /info (model_id + model_sha = the HF revision actually serving).
Before this, every TEI-served collection (MedCPT, BGE-M3) carried a weak
name-derived hash forever, and boot validation could never distinguish a
repointed container from the real model.
"""
import pytest

from app import collections as collections_mod, spaces


def _recipe(api="openai", url="http://tei:8080"):
    return collections_mod.EmbedRecipe(
        url=url, api=api, model="BAAI/bge-m3", dtype="f16", ctx=2048, dim=1024)


class _FakeClient:
    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    def __call__(self, timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        self._calls.append(("GET", url))
        return self._responses[url]

    def post(self, url, json=None):
        self._calls.append(("POST", url))
        return self._responses[url]


class _Resp:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise RuntimeError(self._error)

    def json(self):
        return self._payload


def test_tei_info_confirms_the_hash(monkeypatch):
    calls = []
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://tei:8080/info": _Resp({"model_id": "BAAI/bge-m3",
                                        "model_sha": "babcf60cae0a"}),
    }, calls))
    h, weak = spaces.resolve_embed_model_hash(_recipe())
    assert weak is False
    assert calls == [("GET", "http://tei:8080/info")]
    # deterministic over what is SERVED
    h2, _ = spaces.resolve_embed_model_hash(_recipe())
    assert h == h2


def test_repointed_container_changes_the_hash(monkeypatch):
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://tei:8080/info": _Resp({"model_id": "BAAI/bge-m3",
                                        "model_sha": "aaaa"}),
    }, []))
    h1, _ = spaces.resolve_embed_model_hash(_recipe())
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://tei:8080/info": _Resp({"model_id": "sentence-transformers/other",
                                        "model_sha": "bbbb"}),
    }, []))
    h2, _ = spaces.resolve_embed_model_hash(_recipe())
    assert h1 != h2


def test_llama_server_404_falls_through_to_weak(monkeypatch):
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://tei:8080/info": _Resp(error="404"),
    }, []))
    h, weak = spaces.resolve_embed_model_hash(_recipe())
    assert weak is True


def test_missing_sha_stays_weak(monkeypatch):
    # TEI serving from a local path reports model_sha: null — unconfirmed
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://tei:8080/info": _Resp({"model_id": "BAAI/bge-m3",
                                        "model_sha": None}),
    }, []))
    h, weak = spaces.resolve_embed_model_hash(_recipe())
    assert weak is True


def test_ollama_branch_untouched(monkeypatch):
    monkeypatch.setattr(spaces.httpx, "Client", _FakeClient({
        "http://ol:1/api/show": _Resp({"digest": "sha256:abc"}),
    }, []))
    h, weak = spaces.resolve_embed_model_hash(_recipe(api="ollama", url="http://ol:1"))
    assert (h, weak) == ("sha256:abc", False)
