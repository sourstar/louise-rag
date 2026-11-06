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
    """A tokenizer that, as a real one, counts the same content differently
    in another key order (a merge across `{"type"`)."""
    return len(re.findall(r"<\|\w+\|>|\S+", text)) + text.count('{"type"')


def engine(*, adds: str = "", parse: str = "right", raws: list[str] | None = None, chat_says: str | None = None):
    """LocalAI over llama.cpp as the check sees it: the chat route renders
    the template (plus whatever `adds`) and counts the BOS the tokenizer
    adds; the completion route takes the prompt as given and reports usage
    0, as LocalAI's does; /v1/tokenize counts a text with special tokens
    parsed and adds nothing; the model calls the tool when tools are in its
    prompt; `parse` is what the chat route makes of the raw text; `raws`
    the texts successive raw runs give, `chat_says` the chat's own text."""
    runs = iter(raws or [])

    def generate(prompt: str) -> str:
        return CALL if "<|tools|>" in prompt else ANSWER

    def post(url, body):
        if url.endswith("/v1/tokenize"):
            return 200, {"tokens": list(range(tokens(body["content"])))}
        if url.endswith("/v1/completions"):
            raw = next(runs, None) or generate(body["prompt"])
            return 200, {"choices": [{"text": raw}],
                         "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        prompt = wc.render(T, body["messages"], body.get("tools"), body.get("chat_template_kwargs") or {})[0]
        prompt += adds
        raw = generate(prompt)
        msg: dict = {"role": "assistant", "content": ""}
        if chat_says is not None:
            msg["content"] = chat_says
        elif raw == CALL and parse == "right":
            msg["tool_calls"] = [{"type": "function", "function": {
                "name": "louise_search", "arguments": json.dumps({"query": "Dumas collier"})}}]
        elif parse == "markers":
            msg["content"] = raw
        elif raw == ANSWER:
            msg["content"] = "It is The Queen's Necklace [1]."
        return 200, {"choices": [{"message": msg}],
                     "usage": {"prompt_tokens": tokens(prompt) + 1, "completion_tokens": tokens(raw)}}
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


def no_log(url):
    return 404, "no such route"


def run(post, **kw):
    kw.setdefault("get", no_log)
    return wc.check("m", Q, T, collections=None, search_cache=None, post=post, chat_url="http://chat",
                    ask=ask, **kw)


def test_a_wired_engine_passes_on_the_tool_template():
    got = run(engine())
    assert got["passed"], _failed(got)
    assert got["template"]["which"] == "tool_use"
    assert not got["reference"]["head"].startswith("<s>"), "the BOS the tokenizer adds is not text"
    asked = [k for k in got["answers"] if not k.startswith("tokenize")]
    assert asked == ["chat_count", "raw", "chat", "raw_again"], \
        "the count first; then raw, chat, raw from the same cached prompt"


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
    assert not got["passed"] and "chat_count: 500: Unexpected error" in _failed(got)["every request answered"]


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


def test_a_greedy_answer_that_parts_from_itself_is_held_to_the_run_it_agrees_with():
    """2026-11-06, qwen3-8b-q8 on the node: two greedy raw runs parted after
    596 characters, the chat answer equal to the first. Reported, not
    decided on; the chat answer is held to the run it agrees with."""
    other = CALL.replace("Dumas collier", "Dumas collana")
    got = run(engine(raws=[CALL, other]))
    assert got["passed"], _failed(got)
    greedy = next(c for c in got["checks"] if c["name"] == "greedy repeats itself (raw)")
    assert not greedy["ok"] and not greedy["decides"] and "2 different texts in 2 runs" in greedy["detail"]
    lost = run(engine(raws=[other, other.replace("collana", "collar"), other], chat_says="Something else."))
    assert "in no raw run" in _failed(lost)["OUTPUT: the answer is the model's own text"]
    assert "raw_third" in lost["answers"], "a third run asked when neither agrees"


def test_a_tokenizer_that_does_not_read_markers_decides_nothing():
    def post(url, body):
        if url.endswith("/v1/tokenize"):
            return 200, {"tokens": list(range(len(body["content"]) // 2 + 3))}
        return engine()(url, body)
    got = run(post)
    assert "undecidable" in _failed(got)["INPUT: the prompt is the template's"]


def test_a_tokenizer_that_refuses_an_empty_text_still_counts():
    def post(url, body):
        if url.endswith("/v1/tokenize") and not body["content"]:
            return 400, "empty"
        return engine()(url, body)
    got = run(post)
    assert got["passed"], _failed(got)


def test_a_prompt_difference_is_located_by_part():
    """2026-11-06 on the node: every model's prompt differed from its
    template's by a few tokens (R7B -3, Qwen3-8B -2, Qwen3.5-9B +1), the
    same on both questions. Cut-down conversations say which part."""
    def post(url, body):
        if url.endswith("/v1/chat/completions") and body.get("tools"):
            status, got = engine()(url, body)
            got["usage"]["prompt_tokens"] -= 2           # an engine that renders the tools shorter
            return status, got
        return engine()(url, body)
    got = run(post)
    detail = _failed(got)["INPUT: the prompt is the template's"]
    assert "-2 through chat" in detail
    assert ("by part: a user turn +0, + system +0, + tools -2, + tools, keys sorted -1, "
            "Ask's turns, no tools +0") in detail


def test_tools_whose_keys_the_engine_sorts_are_its_template_s_prompt():
    """LocalAI is Go: the request's tools decoded into maps and encoded again
    come out with sorted keys. The same content, so the prompt is the
    template's; the check says so, and the raw runs read that prompt."""
    def post(url, body):
        if url.endswith("/v1/chat/completions") and body.get("tools"):
            body = {**body, "tools": wc.go_order(body["tools"])}
        return engine()(url, body)
    tools_sorted = wc.go_order(TOOLS)
    assert json.dumps(tools_sorted) != json.dumps(TOOLS), "the test's tools change when sorted"
    got = run(post)
    assert got["passed"], _failed(got)
    detail = next(c["detail"] for c in got["checks"] if c["name"] == "INPUT: the prompt is the template's")
    assert "keys sorted, as LocalAI's Go encoding writes them" in detail
    assert '"function": {"description"' in got["reference"]["head"] + got["reference"]["tail"], \
        "the raw runs read the sorted rendering"
    plain = run(engine())
    assert plain["passed"] and '{"type": "function"' in plain["reference"]["head"] + plain["reference"]["tail"]


def test_a_diagnostic_arm_sends_the_same_sampling_on_both_paths_and_gates_nothing(tmp_path):
    """2026-11-06: from the same prompt and cached slot the Qwens' chat answers
    still parted from the raw runs; which setting do the two endpoints apply
    otherwise? An arm sends the same values on both paths."""
    bodies = []

    def post(url, body):
        bodies.append((url, body))
        return engine()(url, body)
    got = run(post, sampling={"top_k": 1, "presence_penalty": 1e-6})
    sent = [b for u, b in bodies if not u.endswith("/v1/tokenize")]
    assert all(b.get("top_k") == 1 and b.get("presence_penalty") == 1e-6 and b.get("temperature") == 0
               for b in sent), "every chat and raw request carries the arm"
    assert got["sampling"] == {"temperature": 0, "top_k": 1, "presence_penalty": 1e-6}
    where = wc.write(tmp_path, [got], "top_k1")
    assert where.name == "m.wire.top_k1.json"
    with pytest.raises(SystemExit, match="no wiring check"):
        wc.require(tmp_path, "m")


def logged(*, parts_at: int | None = None):
    """An engine whose log ring says how many prompt tokens each request
    evaluated: the one-token chat request all of them; a raw run whose
    prompt is the cached one, its last token only, or, when the prompts
    part at token `parts_at`, all from there on."""
    lines: list[dict] = []
    inner = engine()
    total = {}

    def post(url, body):
        if url.endswith("/v1/detokenize"):
            return 200, {"content": f"<{len(body['tokens'])} tokens>"}
        status, got = inner(url, body)
        if url.endswith("/v1/chat/completions") and body.get("max_tokens") == 1:
            total["n"] = got["usage"]["prompt_tokens"]
            n = total["n"]
        elif url.endswith(("/v1/completions", "/v1/chat/completions")):
            n = 1 if parts_at is None else total["n"] - parts_at
        else:
            return status, got
        lines.append({"timestamp": f"t{len(lines)}", "text": f"prompt eval time = 12.5 ms / {n} tokens"})
        return status, got

    def get(url):
        assert url == "http://chat/api/backend-logs/m"
        return 200, list(lines)
    return post, get


def test_the_same_tokens_not_only_as_many():
    """2026-11-06: equal counts, top_k 1 on both paths, and the Qwens' chat
    answers still parted from the raw runs. The engine's log says how many
    prompt tokens a raw run evaluated after the chat request left its
    prompt in the slot: one when the prompts are token for token the same."""
    post, get = logged()
    got = run(post, get=get)
    same = next(c for c in got["checks"] if c["name"] == "INPUT: the same tokens, not only as many")
    assert same["ok"] and "reused the engine's whole prompt" in same["detail"]
    assert got["answers"]["raw"]["prompt_evaluated"] == 1
    post, get = logged(parts_at=7)
    parted = _failed(run(post, get=get))["INPUT: the same tokens, not only as many"]
    assert "the prompts part at token 7" in parted and "<7 tokens>" in parted and "<12 tokens>" in parted
    unread = next(c for c in run(engine())["checks"] if c["name"] == "INPUT: the same tokens, not only as many")
    assert not unread["ok"] and not unread["decides"], "no log: said, and decides nothing"
