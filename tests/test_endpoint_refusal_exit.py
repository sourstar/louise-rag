"""A model platform's refusal is a refusal, a busy endpoint a deferral —
for the worker and for a command (07 §7.7). München, 2026-11-02: pubmed's
CPU reference hit VeGa's "no run can be stated" (an unmeasured OpenVINO
tree), the client read it as busy and the command exited 1, so
deploy/supervise-pass.sh retried it every minute."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import collections as collections_mod, embeddings, louise

RECIPE = collections_mod.EmbedRecipe(url="http://localai:8080", api="vega", model="m",
                                     dtype="f16", ctx=512)


def _answer(status: int, text: str):
    return SimpleNamespace(status_code=status, text=text)


def test_vegas_refusal_is_recognised_in_the_runners_error():
    body = ('{"error":{"code":500,"message":"failed to load model with internal loader: '
            'could not load model: rpc error: code = Unknown desc = VeGa: no run can be stated: '
            'the backend tree has no deterministic mode (CLAUDE.md invariant 12)"}}')
    e = embeddings._endpoint_error(RECIPE, _answer(500, body))
    assert isinstance(e, embeddings.EndpointRunRefused)
    assert isinstance(e, embeddings.EndpointBusy), "for the worker, still a deferral"
    assert "no deterministic mode" in str(e) and e.status == 500


def test_a_cooldown_or_any_other_5xx_stays_busy():
    e = embeddings._endpoint_error(RECIPE, _answer(503, "load is in cooldown after a recent failure"))
    assert type(e) is embeddings.EndpointBusy


@pytest.mark.parametrize("raised, code", [
    (embeddings.EndpointRunRefused("VeGa: no run can be stated: x", 500), 2),
    (embeddings.EndpointMisconfigured("embed server HTTP 404", 404), 2),
    (embeddings.EndpointBusy("embed server HTTP 503: load is in cooldown", 503), 3),
    (embeddings.EndpointTimedOut("read timeout"), 3),
])
def test_a_command_exits_refused_or_deferred_never_as_a_crash(monkeypatch, capsys, tmp_path, raised, code):
    def boom(a):
        raise raised
    monkeypatch.setattr(louise, "_run_releasing", boom)
    assert louise.main(["hold", "rage-settings", "show", "x", "--root", str(tmp_path)]) == code
    err = capsys.readouterr().err
    assert ("refused" if code == 2 else "deferred") in err


# ---------------------------------------------------------------- the warm

import threading  # noqa: E402

from app import model_load  # noqa: E402

REFUSAL_500 = ('{"error":{"code":500,"message":"failed to load model with internal loader: could not '
               'load model (no success): VeGa: no run can be stated: louise-python-20261020c task '
               '\'medcpt-article\' has no deterministic mode (CLAUDE.md invariant 12; OpenVINO offers '
               'none) and is not measured bit-exact on this tree","type":""}}')
COOLDOWN_503 = ('{"error":{"code":503,"message":"model \\"m\\" load is in cooldown after a recent '
                'failure; retry after 10s","type":""}}')


@pytest.fixture
def cold(monkeypatch):
    model_load.reset()
    monkeypatch.setattr(model_load, "_fetch_system", lambda server: frozenset())
    monkeypatch.setattr(model_load, "load_timeout_s", lambda model: 2.0)
    monkeypatch.setattr(model_load, "publish", lambda c: None)
    yield
    model_load.reset()


def test_a_refused_load_on_the_warm_is_a_refusal_never_noted_loaded(cold):
    """München, 2026-11-02 (reproduced): the load that fails answers 500 with
    VeGa's reason; every request after it a 503 cooldown with none. The warm
    is the request that made the load, so the refusal is raised there."""
    with pytest.raises(model_load.LoadRefused, match="no deterministic mode"):
        model_load.ensure_loaded("http://localai:8080", "m", lambda b: _answer(500, REFUSAL_500),
                                 caller="corpus embedding")
    assert "m" not in (model_load.loaded_models("localai:8080", 60) or ())


def test_a_waiter_on_a_refused_load_gets_the_refusal(cold):
    started, go = threading.Event(), threading.Event()

    def warm(bound):
        started.set()
        go.wait(2)
        return _answer(500, REFUSAL_500)
    errors = []
    t = threading.Thread(target=lambda: errors.append(pytest.raises(
        model_load.LoadRefused, model_load.ensure_loaded, "http://localai:8080", "m", warm, caller="a")))
    t.start()
    started.wait(2)
    w = threading.Thread(target=lambda: errors.append(pytest.raises(
        model_load.LoadRefused, model_load.ensure_loaded, "http://localai:8080", "m",
        lambda b: None, caller="b")))
    w.start()
    go.set()
    t.join(5)
    w.join(5)
    assert len(errors) == 2


def test_a_load_that_fails_otherwise_is_not_noted_loaded_and_the_request_goes(cold):
    """A cooldown or any other failed load: the real request goes and meets
    its own fate — a busy answer, a deferral."""
    assert model_load.ensure_loaded("http://localai:8080", "m", lambda b: _answer(503, COOLDOWN_503),
                                    caller="corpus embedding") is not None
    assert "m" not in (model_load.loaded_models("localai:8080", 60) or ())


def test_a_refused_load_ends_a_command_as_refused(monkeypatch, capsys, tmp_path):
    def boom(a):
        raise model_load.refused_load("m", _answer(500, REFUSAL_500))
    monkeypatch.setattr(louise, "_run_releasing", boom)
    assert louise.main(["hold", "rage-settings", "show", "x", "--root", str(tmp_path)]) == 2
    assert "refused" in capsys.readouterr().err
