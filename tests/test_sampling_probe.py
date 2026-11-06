"""tools/measure/sampling_probe.py: every sampling value checked by its
effect (operator, 2026-11-06: "Be absolutely sure that localai is applying
your settings otherwise the test is void")."""
from __future__ import annotations

import random

import pytest

from app import config
from tools.measure import chat_eval as ev
from tools.measure import sampling_probe as sp


def engine(*, file_presence: float = 0.0, last_n: int = 64, seed_applied: bool = True,
           top_k_applied: bool = True, ctx: int = 16384, thinking: bool = False,
           sure_repeats: bool = False):
    """A llama.cpp behind LocalAI, as the probe sees it: a request's penalty
    of 0 is unset and the file's (or a family's) applies; repeat_last_n 0
    disables the penalties; top_k 1, a tiny top_p or a min_p near 1 leave
    one candidate."""
    rng = random.Random(0)

    def post(url, body):
        prompt = body["messages"][0]["content"]
        assert body["tools"], "the probe declares Ask's tools"
        if prompt.startswith("Louise Louise"):
            return 400, {"error": {"message": f"request (40000 tokens) exceeds the available "
                                              f"context size ({ctx} tokens)"}}
        presence = body.get("presence_penalty") or file_presence
        penalised = last_n > 0 and presence >= 1e-3
        greedy = (body.get("temperature") == 0 or (top_k_applied and body.get("top_k") == 1)
                  or body.get("top_p", 1) <= 1e-3 or body.get("min_p", 0) >= 0.99)
        if prompt == sp.REPEAT:
            text = f"R{presence}" if penalised and not sure_repeats else "R"
        elif greedy:
            text = f"G{presence}" if penalised else "G"
        else:
            text = f"H{body.get('seed') if seed_applied else rng.random()}"
        n = min(body["max_tokens"], 30)
        msg = {"role": "assistant", "content": ("<think>x</think>" if thinking else "") + text}
        return 200, {"choices": [{"message": msg, "finish_reason": "length" if n < 30 else "stop"}],
                     "usage": {"completion_tokens": n}}
    return post


@pytest.fixture(autouse=True)
def localai(monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")


def _failed(result):
    return {c["name"]: c["detail"] for c in result["checks"] if not c["ok"]}


def test_an_engine_that_applies_everything_passes():
    got = sp.probe("m", engine())
    assert got["passed"], _failed(got)
    assert len(got["checks"]) == 11


def test_a_family_default_filling_the_file_s_zero_is_caught_and_named():
    """LocalAI's inference defaults take a penalty of 0 for unset: a file's
    presence 0 on a model named qwen3.5 runs at the family's 1.5."""
    got = sp.probe("louise-chat-qwen3.5-9b-q8", engine(file_presence=1.5))
    assert not got["passed"]
    assert "family default" in _failed(got)["the file's penalties are neutral"]


def test_penalties_disabled_by_a_zero_window_are_caught():
    """No repeat_last_n in the file: LocalAI sends 0, which disables them."""
    got = sp.probe("m", engine(last_n=0))
    assert list(_failed(got)) == ["a presence penalty reaches the engine"]


def test_a_model_too_sure_to_move_on_a_repetition_shows_the_penalty_on_free_text():
    """München's first probes, 2026-11-06: told to write a word 30 times,
    Qwen3-8B was sure enough of every token that presence 2.0 moved none;
    free text, full of near-ties, is where the penalty shows."""
    got = sp.probe("louise-chat-qwen3-8b-q8", engine(sure_repeats=True))
    assert got["passed"], _failed(got)
    detail = next(c["detail"] for c in got["checks"] if c["name"] == "a presence penalty reaches the engine")
    assert detail == "2.0 changed the answer on free text"
    # the negative control: a file with no repeat_last_n (LocalAI sends 0) fails on both
    live = sp.probe("louise-chat-qwen3-8b", engine(sure_repeats=True, last_n=0))
    assert "free text or repetition" in _failed(live)["a presence penalty reaches the engine"]


def test_a_value_that_does_not_reach_the_engine_is_named():
    assert list(_failed(sp.probe("m", engine(seed_applied=False)))) == ["seed reaches the engine"]
    assert list(_failed(sp.probe("m", engine(top_k_applied=False)))) == ["top_k reaches the engine"]
    assert list(_failed(sp.probe("m", engine(ctx=8192)))) == ["the context is 16384 tokens"]
    assert list(_failed(sp.probe("m", engine(thinking=True)))) == ["thinking is off"]


def test_an_engine_that_is_not_deterministic_decides_nothing():
    flaky = iter(range(10**6))
    got = sp.probe("m", lambda url, body: (200, {"choices": [{"message": {"content": str(next(flaky))}}]}))
    assert [c["name"] for c in got["checks"]] == ["greedy repeats itself"] and not got["passed"]


def test_a_refused_request_fails_the_probe_with_its_reason():
    got = sp.probe("m", lambda url, body: (500, "backend down"))
    assert not got["passed"] and "500: backend down" in _failed(got)["every probe request answered"]


def test_a_run_on_a_model_without_a_passing_probe_is_refused(tmp_path):
    q = tmp_path / "q.tsv"
    q.write_text("W1\ten\tDumas novel\n", encoding="utf-8")
    args = ["run", "--out", str(tmp_path), "--name", "m.t01", "--model", "m", "--queries", str(q),
            "--pilot", "3", "--sampling", "temperature=0.1"]
    with pytest.raises(SystemExit, match="no sampling probe"):
        ev.main(args)
    sp.write(tmp_path, sp.probe("m", engine(file_presence=1.5)))
    with pytest.raises(SystemExit, match="family default"):
        ev.main(args)
    installed = tmp_path / "m.yaml"
    installed.write_text("name: m\n", encoding="utf-8")
    where = sp.write(tmp_path, sp.probe("m", engine()), installed, "v9.9")
    sp.require(tmp_path, "m")
    assert '"installed_file"' in where.read_text() and '"v9.9"' in where.read_text()
