"""The streamed answer's END is said (2026-10-13: "the answer was cut", and
nothing had recorded how it ended). `llm.chat_stream` returns a StreamEnd —
the endpoint's finish_reason, whether its end marker came, an error it
carried — and the answer prompt carries Qwen3's no-think switch, measured on
the node that day (app/retrieval.py THINK_OFF)."""
import json

import httpx

from app import config, llm, retrieval


def _stream_client(monkeypatch, body: bytes, status: int = 200) -> None:
    def handler(request):
        return httpx.Response(status, content=body,
                              headers={"content-type": "text/event-stream"})
    real = httpx.Client
    monkeypatch.setattr(llm.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(llm, "warm", lambda level=None: None)


def _openai(monkeypatch) -> None:
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", "http://chat.test")
    monkeypatch.setattr(config, "CHAT_MODEL", "m")


def _sse(*objs: dict, done: bool = True) -> bytes:
    out = b"".join(f"data: {json.dumps(o)}\n\n".encode() for o in objs)
    return out + (b"data: [DONE]\n\n" if done else b"")


def _run() -> tuple[list[str], llm.StreamEnd]:
    ends: list[llm.StreamEnd] = []

    def gen():
        ends.append((yield from llm.chat_stream([{"role": "user", "content": "q"}])))
    pieces = list(gen())
    return pieces, ends[0]


def test_a_whole_openai_stream_ends_with_stop(monkeypatch):
    _openai(monkeypatch)
    _stream_client(monkeypatch, _sse(
        {"choices": [{"delta": {"content": "Il "}, "finish_reason": None}]},
        {"choices": [{"delta": {"reasoning_content": "hidden"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "diritto"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    pieces, end = _run()
    assert pieces == ["Il ", "diritto"], "reasoning is never shown"
    assert end == llm.StreamEnd(finish_reason="stop", done=True, error=None)
    assert end.whole and end.why() == "whole"


def test_a_capped_stream_says_so(monkeypatch):
    _openai(monkeypatch)
    _stream_client(monkeypatch, _sse(
        {"choices": [{"delta": {"content": "Il diritto"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]}))
    _, end = _run()
    assert end.finish_reason == "length" and end.done and not end.whole
    assert "token limit" in end.why()


def test_a_stream_closed_without_its_marker_is_not_whole(monkeypatch):
    _openai(monkeypatch)
    _stream_client(monkeypatch, _sse(
        {"choices": [{"delta": {"content": "Il diritto all'"}, "finish_reason": None}]},
        done=False))
    pieces, end = _run()
    assert pieces == ["Il diritto all'"]
    assert end == llm.StreamEnd(finish_reason=None, done=False, error=None)
    assert not end.whole and "closed before" in end.why()


def test_an_error_payload_in_the_stream_is_carried(monkeypatch):
    _openai(monkeypatch)
    _stream_client(monkeypatch, _sse(
        {"choices": [{"delta": {"content": "Il "}, "finish_reason": None}]},
        {"error": {"message": "slot unavailable", "code": 503}}, done=False))
    pieces, end = _run()
    assert pieces == ["Il "]
    assert end.error == "slot unavailable" and not end.whole
    assert "slot unavailable" in end.why()


def test_the_ollama_dialect_reads_done_reason(monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "ollama")
    monkeypatch.setattr(config, "CHAT_URL", "http://chat.test")
    body = (json.dumps({"message": {"content": "Il "}, "done": False}) + "\n"
            + json.dumps({"message": {"content": "diritto"}, "done": True,
                          "done_reason": "length"}) + "\n")
    _stream_client(monkeypatch, body.encode())
    pieces, end = _run()
    assert pieces == ["Il ", "diritto"]
    assert end == llm.StreamEnd(finish_reason="length", done=True, error=None)
    assert not end.whole


def test_delta_still_answers_the_old_pair(monkeypatch):
    _openai(monkeypatch)
    assert llm._delta('data: {"choices": [{"delta": {"content": "x"}}]}') == ("x", False)
    assert llm._delta("data: [DONE]") == ("", True)
    assert llm._delta(": keepalive") == ("", False)


def test_the_answer_prompt_carries_the_no_think_switch():
    result = {"hits": [], "facts": [], "catalog": []}
    for kw in ({}, {"answer_lang": "it"}):
        system = retrieval.build_answer_messages("q", result, **kw)[0]["content"]
        assert system.endswith(retrieval.THINK_OFF), system[-60:]
    assert "ISO code 'it'" in retrieval.build_answer_messages("q", result, answer_lang="it")[0]["content"]
