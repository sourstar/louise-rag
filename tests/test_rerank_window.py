"""The reranker's window (05 §5.6, amended 2026-10-21): every candidate is cut
to the model's `window` of its own tokens before it is sent, so the model's
context is the window, never the longest chunk a corpus holds."""
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, retrieval


def _recipe(window=2048, api="jina"):
    return collections_mod.RerankRecipe(url="http://localai:1/v1/rerank", api=api,
                                        model="louise-rerank-bge", window=window)


@pytest.fixture
def tokenizer(monkeypatch):
    """LocalAI's /v1/tokenize as a stand-in: one token per word."""
    calls: list = []

    def post(url, json=None, timeout=None, **kw):
        calls.append((url, json))
        tokens = list(range(len(json["content"].split())))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"tokens": tokens})
    monkeypatch.setattr(retrieval.httpc, "post", post)
    return calls


def test_a_short_candidate_goes_whole_and_uncounted(tokenizer):
    text = "Moby Dick\n\nCall me Ishmael."
    assert retrieval.fit_to_window([text], _recipe(window=64)) == [text]
    assert tokenizer == [], "within window * 2 bytes: no call"


def test_a_long_candidate_is_cut_to_the_window_by_the_models_own_count(tokenizer):
    text = " ".join(f"w{i}" for i in range(500))
    (cut,) = retrieval.fit_to_window([text], _recipe(window=100))
    assert len(cut.split()) <= 100 and text.startswith(cut)
    assert tokenizer[0][0] == "http://localai:1/v1/tokenize"
    assert tokenizer[0][1]["model"] == "louise-rerank-bge"
    assert retrieval.fit_to_window([text], _recipe(window=100)) == [cut], "the same text, the same cut"


def test_without_a_count_the_byte_cut_stands_in(monkeypatch):
    def down(*a, **kw):
        raise OSError("unreachable")
    monkeypatch.setattr(retrieval.httpc, "post", down)
    text = "é" * 400                                   # 800 bytes
    (cut,) = retrieval.fit_to_window([text], _recipe(window=100))
    assert len(cut.encode("utf-8")) <= 100 * retrieval.WINDOW_BYTES_PER_TOKEN
    assert text.startswith(cut)


@pytest.mark.parametrize("recipe", [_recipe(window=0), _recipe(window=10, api="tei")])
def test_no_window_or_a_tei_endpoint_cuts_nothing(tokenizer, recipe):
    text = "word " * 1000
    assert retrieval.fit_to_window([text], recipe) == [text] and tokenizer == []


def test_the_window_is_read_from_the_file_with_a_default():
    base = {"url": "http://localai:1/v1/rerank", "api": "jina", "rerank_model": "m"}
    assert collections_mod.parse_reranker(base, where="f").window == 2048
    assert collections_mod.parse_reranker(base | {"window": 512}, where="f").window == 512
    assert collections_mod.parse_reranker(base | {"window": 0}, where="f").window == 0
    for bad in (-1, "512", True):
        with pytest.raises(ValueError, match="window"):
            collections_mod.parse_reranker(base | {"window": bad}, where="f")


def test_the_reranker_is_warmed_before_its_tokenizer_cuts(monkeypatch):
    """The cut asks the reranker's own tokenizer: cold, that request WAS the
    load — its 10 s timeout ran out, the text was cut by bytes, the next
    text started a second load, and no cold start was logged (München,
    2026-10-27 15:42). Warm first; a load past its bound keeps the fused
    order before anything is tokenized."""
    order: list[str] = []
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda r: order.append("warm"))
    monkeypatch.setattr(retrieval, "fit_to_window",
                        lambda texts, r: order.append("cut") or texts)
    hits = [{"uid": "a", "text": "x"}, {"uid": "b", "text": "y"}]
    try:
        retrieval._rerank_list("q", hits, _recipe(), 5.0)
    except Exception:   # noqa: BLE001 — only the order before the call matters here
        pass
    assert order[:2] == ["warm", "cut"]

    order.clear()
    from app import model_load

    def cold(r):
        raise model_load.LoadTimeout("model loading took longer than 60 s", model="m", bound_s=60)
    monkeypatch.setattr(retrieval, "_warm_reranker", cold)
    got, reranked = retrieval._rerank_list("q", hits, _recipe(), 5.0)
    assert order == [] and reranked is False, "nothing tokenized for a model still loading"
