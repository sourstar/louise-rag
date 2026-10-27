"""The question path's models, warmed in the background at the web's start
and when a model server comes back (app/warmup.py; operator, 2026-10-27:
option A, after a first question that waited ~65 s for cold loads)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import collections as collections_mod, config, llm, model_load, retrieval, translate, warmup


@pytest.fixture
def path(monkeypatch):
    monkeypatch.setattr(config, "TRANSLATE_URL", "http://ai:8080")
    monkeypatch.setattr(config, "TRANSLATE_MODEL", "louise-translate-madlad")
    monkeypatch.setattr(config, "CHAT_URL", "http://ai:8080/v1")
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_MODEL", "louise-chat-qwen3-8b")
    bge = collections_mod.RerankRecipe(url="http://ai:8080/v1/rerank", api="jina", model="louise-rerank-bge")
    medcpt = collections_mod.RerankRecipe(url="http://ai:8080/v1/rerank", api="jina", model="louise-rerank-medcpt")
    held = [SimpleNamespace(name="g", reranker=bge), SimpleNamespace(name="w", reranker=bge),
            SimpleNamespace(name="p", reranker=medcpt), SimpleNamespace(name="n", reranker=None)]
    monkeypatch.setattr(collections_mod, "load_all", lambda *a, **k: held)
    monkeypatch.setattr(config, "RERANK_URL", "")
    done: list[str] = []
    monkeypatch.setattr(translate, "warm", lambda: done.append("translate"))
    monkeypatch.setattr(llm, "warm", lambda: done.append("chat"))
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda r: done.append(r.model))
    return done


def test_the_translator_each_distinct_reranker_and_the_chat_model_in_order(path):
    warmup.warm_all("test")
    assert path == ["translate", "louise-rerank-bge", "louise-rerank-medcpt", "chat"]


def test_a_failed_warm_is_said_and_the_next_one_still_goes(path, monkeypatch, capsys):
    def boom():
        raise model_load.LoadTimeout("model loading took longer than 60 s", model="m", bound_s=60)
    monkeypatch.setattr(translate, "warm", boom)
    warmup.warm_all("test")
    assert path == ["louise-rerank-bge", "louise-rerank-medcpt", "chat"]
    assert "not warmed (test): LoadTimeout" in capsys.readouterr().out


def test_only_the_server_that_came_back_is_warmed(path, monkeypatch):
    monkeypatch.setattr(config, "CHAT_URL", "http://other:9000/v1")
    warmup.warm_all("back", only={"http://other:9000"})
    assert path == ["chat"]


def test_switched_off_in_tests_and_started_once(monkeypatch):
    assert warmup.start() is False, "LOUISE_WARMUP=0 (tests/conftest.py)"
    monkeypatch.setenv("LOUISE_WARMUP", "1")
    monkeypatch.setattr(warmup, "_started", True)
    assert warmup.start() is False, "once per process"


def test_a_server_that_comes_back_is_seen_once(path, monkeypatch):
    answers = iter([frozenset({"x"}), None, frozenset(), frozenset({"y"})])
    monkeypatch.setattr(model_load, "loaded_models", lambda s, age=None: next(answers))
    up: dict[str, bool] = {}
    assert warmup._back(up) == set(), "up at the first look: the start's warm covers it"
    assert warmup._back(up) == set(), "gone"
    assert warmup._back(up) == {"http://ai:8080"}, "back, nothing loaded: a restart"
    assert warmup._back(up) == set(), "still up: not again"
