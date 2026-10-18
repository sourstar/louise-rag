"""A backend-gate verdict holds only for the endpoint it was reached on
(2026-10-09: gutenberg's [encoder] moved from RAGE to LocalAI and RAGE's pass
covered LocalAI's vectors for up to PASS_TTL_S)."""
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, config, db
from app.verify import backend_gate


@pytest.fixture
def gate(monkeypatch):
    backend_gate.reset_cache()
    calls, logged = [], []
    recipe = {"r": SimpleNamespace(url="http://models:30437/upstream/q", api="openai",
                                   model="q", query_model="q")}
    monkeypatch.setattr(config, "BACKEND_GATE", "on")
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c: recipe["r"])
    monkeypatch.setattr(backend_gate, "check",
                        lambda c, fn=None: calls.append(recipe["r"].url) or {"recorded": False})
    monkeypatch.setattr(db, "log_event", lambda con, lvl, kind, msg: logged.append(msg))
    monkeypatch.setattr(db, "set_control", lambda *a, **k: None)
    yield SimpleNamespace(calls=calls, logged=logged, recipe=recipe,
                          coll=SimpleNamespace(name="gutenberg", codex=None))
    backend_gate.reset_cache()


def test_a_pass_is_cached_for_the_same_endpoint(gate):
    assert backend_gate.embed_allowed(None, gate.coll)
    assert backend_gate.embed_allowed(None, gate.coll)
    assert gate.calls == ["http://models:30437/upstream/q"]


def test_a_new_endpoint_is_gated_before_the_cached_pass_is_used(gate):
    assert backend_gate.embed_allowed(None, gate.coll)
    gate.recipe["r"] = SimpleNamespace(url="http://localai:30286", api="vega",
                                       model="vega-q", query_model="vega-q")
    assert backend_gate.embed_allowed(None, gate.coll)
    assert gate.calls == ["http://models:30437/upstream/q", "http://localai:30286"]
    assert len(gate.logged) == 2          # the re-gate on the new endpoint is logged


def test_a_new_endpoint_that_fails_pauses_at_once(gate, monkeypatch):
    assert backend_gate.embed_allowed(None, gate.coll)
    gate.recipe["r"] = SimpleNamespace(url="http://elsewhere", api="vega",
                                       model="x", query_model="x")

    def refuse(c, fn=None):
        raise backend_gate.BackendGateError("cosine 0.2 below the baseline")
    monkeypatch.setattr(backend_gate, "check", refuse)
    assert not backend_gate.embed_allowed(None, gate.coll)
