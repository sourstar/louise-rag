"""tools/measure/wire_check.py: is a chat model given its template's prompt,
and Ask its raw output? (operator, 2026-11-06: "Are you sure that the model
input/output are correctly wired?")"""
from __future__ import annotations

import json
import re

import pytest

from app import config
from tools.measure import wire_check as wc

TEMPLATE = ("{{ bos_token }}{% for m in messages %}<|{{ m.role }}|>{{ m.content }}\n{% endfor %}"
            "{% if add_generation_prompt %}<|assistant|>{% endif %}")
TOOL_TEMPLATE = ("{{ bos_token }}{% for m in messages %}<|{{ m.role }}|>{{ m.content }}\n{% endfor %}"
                 "{% if tools %}<|tools|>{{ tools | tojson }}\n{% endif %}"
                 "{% if add_generation_prompt %}<|assistant|>{% endif %}")
TOOLS = [{"type": "function", "function": {"name": "louise_search", "description": "search",
                                           "parameters": {"type": "object"}}}]
T = wc.Templates(default=TEMPLATE, tool_use=TOOL_TEMPLATE, bos="<s>", eos="</s>", add_bos=True, add_eos=False)
CALL = '<|act|>{"name": "louise_search", "arguments": {"query": "Dumas collier"}}<|end|>'
ANSWER = "<|resp|>It is The Queen's Necklace [1].<|end|>"


def tokens(text: str) -> int:
    return len(re.findall(r"<\|\w+\|>|\S+", text))


def engine(*, adds: str = "", parse: str = "right"):
    """LocalAI over llama.cpp as the check sees it: the chat route renders
    the template (plus whatever `adds`), the completion route takes the
    prompt as given; the model calls the tool when tools are in its prompt;
    `parse` is what the chat route makes of the raw text."""
    def generate(prompt: str) -> str:
        return CALL if "<|tools|>" in prompt else ANSWER

    def post(url, body):
        if url.endswith("/v1/completions"):
            raw = generate(body["prompt"])
            return 200, {"choices": [{"text": raw}],
                         "usage": {"prompt_tokens": tokens(body["prompt"]), "completion_tokens": tokens(raw)}}
        prompt = wc.render(T, body["messages"], body.get("tools"), body.get("chat_template_kwargs") or {})[0]
        prompt += adds
        raw = generate(prompt)
        msg: dict = {"role": "assistant", "content": ""}
        if raw == CALL and parse == "right":
            msg["tool_calls"] = [{"type": "function", "function": {
                "name": "louise_search", "arguments": json.dumps({"query": "Dumas collier"})}}]
        elif parse == "markers":
            msg["content"] = raw
        elif raw == ANSWER:
            msg["content"] = "It is The Queen's Necklace [1]."
        return 200, {"choices": [{"message": msg}],
                     "usage": {"prompt_tokens": tokens(prompt), "completion_tokens": tokens(raw)}}
    return post


def ask(question, collections=None, call=None, model=None):
    """Ask's first request: its system prompt, the sources and the question, its tools."""
    model([{"role": "system", "content": "Answer from the sources."},
           {"role": "user", "content": f"Sources:\n\n[1] La collana\n\nQuestion: {question}"}], TOOLS)
    return {}


@pytest.fixture(autouse=True)
def localai(monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")


Q = {"key": "W1:it", "lang": "it", "query": "romanzo di Dumas sulla collana"}


def _failed(r):
    return {c["name"]: c["detail"] for c in r["checks"] if not c["ok"]}


def run(post, **kw):
    return wc.check("m", Q, T, collections=None, search_cache=None, post=post, chat_url="http://chat",
                    ask=ask, **kw)


def test_a_wired_engine_passes_on_the_tool_template():
    got = run(engine())
    assert got["passed"], _failed(got)
    assert got["template"]["which"] == "tool_use"
    assert not got["reference"]["head"].startswith("<s>"), "the BOS the tokenizer adds is not text"


def test_an_engine_that_adds_to_the_prompt_is_caught_and_the_addition_can_be_tested():
    """llama.cpp's Command R7B handler appends a closed thinking block when
    thinking is off: the counts differ, and --append names what was added."""
    adds = "<|START_THINKING|><|END_THINKING|>"
    got = run(engine(adds=adds))
    assert "INPUT: the prompt is the template's" in _failed(got)
    assert run(engine(adds=adds), append=adds)["passed"]


def test_a_tool_call_the_parser_loses_is_caught():
    got = run(engine(parse="lost"))
    assert "in the raw text, not parsed: ['louise_search']" in _failed(got)["OUTPUT: every tool call kept"]


def test_markers_left_in_the_answer_are_caught():
    got = run(engine(parse="markers"))
    assert "<|act|>" in _failed(got)["OUTPUT: no marker left in the answer"]


def test_a_refused_request_fails_with_the_server_s_answer():
    got = run(lambda url, body: (500, "Unexpected error in RPC handling"))
    assert not got["passed"] and "raw: 500: Unexpected error" in _failed(got)["every request answered"]


def test_the_templates_are_read_from_the_gguf_as_llama_cpp_carries_them(tmp_path):
    gguf = pytest.importorskip("gguf")
    path = tmp_path / "m.gguf"
    w = gguf.GGUFWriter(str(path), "llama")
    w.add_chat_template([{"name": "default", "template": TEMPLATE}, {"name": "tool_use", "template": TOOL_TEMPLATE}])
    w.add_token_list(["<unk>", "<s>", "</s>", "a"])
    w.add_bos_token_id(1)
    w.add_eos_token_id(2)
    w.add_add_bos_token(True)
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()
    got = wc.read_gguf(path)
    assert (got.default, got.tool_use, got.bos, got.eos, got.add_bos, got.add_eos) == \
        (TEMPLATE, TOOL_TEMPLATE, "<s>", "</s>", True, False)


def test_the_result_is_written_and_tabled(tmp_path):
    results = [run(engine())]
    where = wc.write(tmp_path, results)
    assert json.loads(where.read_text())["passed"]
    assert "WIRED" in wc.table(results)


def test_a_run_on_a_model_not_shown_wired_is_refused(tmp_path):
    from tests.test_sampling_probe import engine as sampling_engine
    from tools.measure import chat_eval as ev
    from tools.measure import sampling_probe as sp
    q = tmp_path / "q.tsv"
    q.write_text("W1\ten\tDumas novel\n", encoding="utf-8")
    sp.write(tmp_path, sp.probe("m", sampling_engine()))
    args = ["run", "--out", str(tmp_path), "--name", "m.t07", "--model", "m", "--queries", str(q), "--pilot", "3"]
    with pytest.raises(SystemExit, match="no wiring check"):
        ev.main(args)
    wc.write(tmp_path, [run(engine(parse="lost"))])
    with pytest.raises(SystemExit, match="NOT WIRED — W1:it: OUTPUT: every tool call kept"):
        ev.main(args)
    wc.write(tmp_path, [run(engine())])
    wc.require(tmp_path, "m")
