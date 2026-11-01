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
