"""tests/ask/test_chat.py — `ask chat` (05 §5.4): the model with the three
tools and nothing else — a tool it calls is run and its answer handed
back compacted, the final answer travels with every call made, the
rounds are bounded, a tool's refusal is handed back rather than raised,
no chat model refuses; and the chat client's tool protocol in both of
CHAT_URL's dialects."""
import json

import pytest

from app import config, llm
from app.ask import chat
from app.hold.codex import CodexError

LONG = "x" * 2000
SEARCH_RESULT = {"lists": [{"space_id": "sha256:" + "1" * 64, "question": "relevance", "hits": [
    {"uid": "gutenberg:2701", "snapshot_id": "2026-08", "rank": 1, "title": "Moby Dick", "lang": "en",
     "trust_level": "checked", "verified_on_use": "local", "legs": {"text": 1, "vector": 2},
     "collection_id": "sha256:" + "2" * 64, "chunk_id": "gutenberg:2701:3:1", "text": LONG, "certification": None},
    {"uid": "pubmed:1", "snapshot_id": "2026-08", "rank": 2, "trust_level": "unverified", "verified_on_use": "imported",
     "legs": {"text": None, "vector": 1}, "origin_url": "https://pubmed.ncbi.nlm.nih.gov/1/"}]}],
    "facts": [], "unresolved": [], "withheld": [{"collection": "x", "reason": "quarantine"}],
    "legs_withheld": [], "translations": {}, "timings": {"total": 3}}


@pytest.fixture
def chat_model(monkeypatch):
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")


def scripted(*rounds):
    """A model that answers each round from the script and records what it saw."""
    seen = []

    def model(messages, tools):
        seen.append((list(messages), tools))
        content, calls = rounds[min(len(seen), len(rounds)) - 1]
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]
        return content, calls, assistant
    model.seen = seen
    return model


def test_the_model_calls_a_tool_and_answers_from_what_it_returned(chat_model):
    search = llm.ToolCall(None, "louise_search", {"query": "white whale"})
    model = scripted(("", [search]), ("Ahab hunts the whale [gutenberg:2701].", []))
    called = []

    def call(name, args):
        called.append((name, args))
        return SEARCH_RESULT
    out = chat.ask("who hunts the whale?", collections=["gutenberg"], call=call, model=model,
                   history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                            {"role": "tool", "content": "never replayed"}])
    assert out["answer"] == "Ahab hunts the whale [gutenberg:2701]." and out["rounds"] == 2 and not out["exhausted"]
    assert called == [("louise_search", {"query": "white whale", "collections": ["gutenberg"],
                                         "translate": True})], \
        "the collections asked for reach the tool, and translation is on as on the Ask tab"
    (turn,) = out["turns"]
    assert turn["tool"] == "louise_search" and turn["result"] is SEARCH_RESULT and turn["error"] is None
    first, _ = model.seen[0]
    assert first[0]["role"] == "system" and "louise_search" in first[0]["content"] and "gutenberg" in first[0]["content"]
    assert [m["role"] for m in first[1:]] == ["user", "assistant", "user"], "history kept, a tool message not replayed"
    second, tools = model.seen[1]
    assert second[-2]["role"] == "assistant" and second[-1]["role"] == "tool"
    assert [t["function"]["name"] for t in tools] == ["louise_search", "louise_lookup", "louise_traverse"]
    shown = json.loads(second[-1]["content"])
    hit, red = shown["lists"][0]["hits"]
    assert hit["text"].endswith(" …") and len(hit["text"]) <= chat.TEXT_CHARS + 2, "the model sees the text trimmed"
    assert "collection_id" not in hit and "chunk_id" not in hit and hit["legs"] == {"text": 1, "vector": 2}
    assert "text" not in red and red["origin_url"].endswith("/1/"), "red: the origin's page, as 05 §5.3 says"
    assert shown["withheld"] == [{"collection": "x", "reason": "quarantine"}] and "timings" not in shown


def test_rounds_are_bounded_then_the_model_answers_without_tools(chat_model):
    lookup = llm.ToolCall("c1", "louise_lookup", {"id": "hgnc:1101"})
    model = scripted(("", [lookup]), ("", [lookup]), ("BRCA2 is on 13q13.1.", []))
    out = chat.ask("where is BRCA2?", call=lambda n, a: {"rows": []}, model=model, max_rounds=2)
    assert out["exhausted"] and out["rounds"] == 2 and len(out["turns"]) == 2
    assert out["answer"] == "BRCA2 is on 13q13.1."
    assert model.seen[-1][1] is None, "past the bound the tools are withheld"


def test_a_tool_s_refusal_is_handed_back_not_raised(chat_model):
    bad = llm.ToolCall(None, "louise_search", {"query": "q", "as_of": "2026-9"})
    nope = llm.ToolCall(None, "louise_summon", {})
    model = scripted(("", [bad, nope]), ("Nothing answers.", []))

    def call(name, args):
        if name == "louise_summon":
            raise KeyError(name)
        raise CodexError("as_of is a date YYYY-MM-DD")
    out = chat.ask("q", call=call, model=model)
    assert [t["error"] for t in out["turns"]] == ["as_of is a date YYYY-MM-DD", "no tool named 'louise_summon'"]
    second, _ = model.seen[1]
    assert json.loads(second[-2]["content"]) == {"error": "as_of is a date YYYY-MM-DD"}
    assert out["answer"] == "Nothing answers."


def test_no_chat_model_refuses_and_so_does_an_empty_question(monkeypatch):
    monkeypatch.setattr(config, "CHAT_URL", "")
    with pytest.raises(chat.ChatError, match="CHAT_URL") as e:
        chat.ask("q", call=lambda n, a: None, model=lambda m, t: ("", [], {}))
    assert e.value.exit_code == 2
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")
    with pytest.raises(chat.ChatError):
        chat.ask("   ", call=lambda n, a: None, model=lambda m, t: ("", [], {}))


# ---- the client's tool protocol -------------------------------------------------------

class _Client:
    def __init__(self, seen):
        self.seen = seen

    def __call__(self, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post(self, url, json=None, headers=None):
        self.seen.update(url=url, body=json, headers=headers)
        answer = self.seen["answer"]

        class R:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return answer
        return R()


@pytest.fixture
def fake_chat(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm.httpx, "Client", _Client(seen))
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")
    return seen


def test_ollama_tool_calls_round_trip(fake_chat, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "ollama")
    fake_chat["answer"] = {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "louise_lookup", "arguments": {"id": "hgnc:1101"}}}]}}
    tools = chat.tool_specs()
    content, calls, assistant = llm.chat_tools([{"role": "user", "content": "q"}], tools)
    assert fake_chat["url"] == "http://chat/api/chat" and fake_chat["body"]["tools"] == tools
    assert fake_chat["body"]["think"] is False
    assert content == "" and calls == [llm.ToolCall(None, "louise_lookup", {"id": "hgnc:1101"})]
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["function"]["name"] == "louise_lookup"
    assert llm.tool_message(calls[0], "{}") == {"role": "tool", "content": "{}", "tool_name": "louise_lookup"}
    fake_chat["answer"] = {"message": {"role": "assistant", "content": "<think>…</think>Done."}}
    content, calls, _ = llm.chat_tools([{"role": "user", "content": "q"}], None)
    assert (content, calls) == ("Done.", []) and "tools" not in fake_chat["body"]


def test_openai_tool_calls_round_trip(fake_chat, monkeypatch):
    monkeypatch.setattr(config, "CHAT_API", "openai")
    fake_chat["answer"] = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "louise_search", "arguments": "{\"query\": \"whale\", \"limit\": 3}"}}]}}]}
    content, calls, assistant = llm.chat_tools([{"role": "user", "content": "q"}], chat.tool_specs())
    assert fake_chat["url"] == "http://chat/v1/chat/completions" and "tools" in fake_chat["body"]
    assert content == "" and calls == [llm.ToolCall("call_1", "louise_search", {"query": "whale", "limit": 3})], \
        "arguments arrive as a JSON string in this dialect"
    assert assistant["tool_calls"][0]["id"] == "call_1" and assistant["content"] == ""
    assert llm.tool_message(calls[0], "[]") == {"role": "tool", "tool_call_id": "call_1", "content": "[]"}
    fake_chat["answer"] = {"choices": [{"message": {"role": "assistant", "content": "x", "tool_calls": [
        {"id": "c2", "function": {"name": "louise_lookup", "arguments": "not json"}}]}}]}
    _, calls, _ = llm.chat_tools([{"role": "user", "content": "q"}], chat.tool_specs())
    assert calls[0].arguments == {}, "unreadable arguments are an empty call, not a crash"


def test_an_answer_says_where_its_time_went(chat_model, monkeypatch):
    """Each model round's wall time and each tool call's, on the answer
    (tools/measure/chat_eval.py reads them; operator, 2026-11-03)."""
    clock = iter([0.0, 1.5, 1.5, 2.25, 2.25, 2.75])
    monkeypatch.setattr(chat.time, "monotonic", lambda: next(clock))
    lookup = llm.ToolCall("c1", "louise_lookup", {"id": "hgnc:1101"})
    model = scripted(("", [lookup]), ("", [lookup]), ("13q13.1.", []))
    out = chat.ask("where is BRCA2?", call=lambda n, a: {"rows": []}, model=model, max_rounds=1)
    assert out["exhausted"] and out["model_s"] == [1.5, 0.5], "the bound's last round is timed too"
    assert [t["seconds"] for t in out["turns"]] == [0.75]


def test_the_answer_is_asked_in_the_readers_language_named(chat_model):
    """2026-11-03, the chat benchmark: after English tool results the model
    answered in English (Bolt-E4B 14 of 73 in the question's language,
    Qwen3-8B 50 of 73). The prompt names the reader's language: the one asked
    for, else the question's by the node's own guess, else the question's."""
    def system_of(question, **kw):
        model = scripted(("ok", []))
        out = chat.ask(question, call=lambda n, a: {}, model=model, **kw)
        return model.seen[0][0][0]["content"], out["answer_lang"]
    text, lang = system_of("Il romanzo storico sullo scandalo della collana della regina, chi l'ha scritto?")
    assert lang == "it" and text.endswith("Write your whole answer in Italian (ISO code 'it'), whatever "
                                          "language the question or the tool results are in.")
    text, lang = system_of("Il romanzo storico sullo scandalo della collana", answer_lang="DE")
    assert lang == "de" and "in German (ISO code 'de')" in text
    text, lang = system_of("Ahab")
    assert lang is None and text.endswith("in the language the question is written in, whatever "
                                          "language the tool results are in.")


def test_every_language_the_node_can_guess_has_a_name_for_the_prompt():
    """A bare code is echoed back (llm.py LANGUAGE_NAMES); every language the
    guess can return is named."""
    from app import query_language
    assert not query_language.guessable() - set(llm.LANGUAGE_NAMES)


def test_a_question_in_a_script_without_a_word_list_is_still_named(chat_model):
    """Greek, Russian, Chinese have no word list; their script names them
    (Ask's benchmark, 2026-11-03: "no guess" for el, ru, zh)."""
    for question, lang in (("Ποιος έγραψε την Οδύσσεια;", "el"),
                           ("Кто написал роман Война и мир?", "ru"), ("谁写了红楼梦？", "zh")):
        out = chat.ask(question, call=lambda n, a: {}, model=scripted(("ok", [])))
        assert out["answer_lang"] == lang


def _big_search(n: int) -> dict:
    return {"lists": [{"space_id": "s", "hits": [
        {"uid": f"gutenberg:{n}{i}", "rank": i + 1, "title": f"Book {i}", "lang": "en", "text": "word " * 400}
        for i in range(10)]}], "facts": [], "timings": {}}


def test_the_conversation_is_kept_within_the_context(chat_model):
    """2026-11-03, Ask's benchmark: 7 of 50 answers failed with a 400 once
    several searches' results outgrew the 16k context. The oldest results
    are slimmed to their citable skeleton first; the latest stays whole."""
    search = llm.ToolCall(None, "louise_search", {"query": "q"})
    model = scripted(*([("", [search])] * 7), ("done [gutenberg:61].", []))
    n = iter(range(100))
    out = chat.ask("q", call=lambda name, a: _big_search(next(n)), model=model, max_rounds=8)
    budget = chat.CONTEXT_TOKENS - chat.ANSWER_TOKENS - chat.MARGIN_TOKENS
    assert all(chat.request_tokens(msgs, tools) <= budget for msgs, tools in model.seen)
    assert out["slimmed"] > 0 and out["answer"] == "done [gutenberg:61]."
    last_msgs, _ = model.seen[-1]
    tool_msgs = [m for m in last_msgs if m["role"] == "tool"]
    first, latest = json.loads(tool_msgs[0]["content"]), json.loads(tool_msgs[-1]["content"])
    assert first["slimmed"] and first["lists"][0]["hits"][0] == {
        "uid": "gutenberg:00", "title": "Book 0", "rank": 1, "lang": "en"}, "citable, without text"
    assert "slimmed" not in latest and "text" in latest["lists"][0]["hits"][0]


def test_a_request_the_server_refuses_as_too_large_is_answered_with_what_it_has(chat_model):
    """A 400 after the tools have run: every result slimmed, tools withheld,
    one more ask. The answer is never lost because the searches returned a lot."""
    import httpx
    search = llm.ToolCall(None, "louise_search", {"query": "q"})
    calls = []

    def model(messages, tools):
        calls.append(tools)
        if len(calls) == 2:
            raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(400, text=OVER))
        if len(calls) == 1:
            return "", [search], {"role": "assistant", "content": ""}
        return "From what I found [gutenberg:00].", [], {"role": "assistant", "content": ""}
    out = chat.ask("q", call=lambda n, a: _big_search(0), model=model)
    assert out["answer"].startswith("From what I found") and out["exhausted"] and out["slimmed"] == 1
    assert calls[-1] is None, "tools withheld on the last ask"


#: LocalAI's answer, measured on the node 2026-11-03 (Ask's benchmark, qwen3.5-9b-q4)
OVER = ('{"error":{"code":400,"message":"code=400, message=rpc error: code = Internal desc = '
        'request (17013 tokens) exceeds the available context size (16384 tokens), try increasing it"}}')


@pytest.mark.parametrize("before_any_tool,text", [(True, OVER), (False, '{"error": "bad template"}')])
def test_any_other_refusal_is_raised_not_answered_around(chat_model, before_any_tool, text):
    """Only the context refusal after tools ran is answered around; a 400
    before any tool, or for another reason, is a fault to see."""
    import httpx
    search = llm.ToolCall(None, "louise_search", {"query": "q"})
    calls = []

    def model(messages, tools):
        calls.append(1)
        if before_any_tool or len(calls) == 2:
            raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(400, text=text))
        return "", [search], {"role": "assistant", "content": ""}
    with pytest.raises(httpx.HTTPStatusError):
        chat.ask("q", call=lambda n, a: _big_search(0), model=model)


def test_the_models_searches_translate_as_the_ask_tab_does_unless_it_says_otherwise(chat_model):
    """2026-11-03: the Ask tab searches with translation on; the tool's
    default is off and no model was told to ask for it."""
    called = []
    model = scripted(("", [llm.ToolCall(None, "louise_search", {"query": "a"}),
                           llm.ToolCall(None, "louise_search", {"query": "b", "translate": False})]),
                     ("ok", []))
    chat.ask("q", call=lambda n, a: called.append(a) or {"lists": []}, model=model)
    assert [a.get("translate") for a in called] == [True, False]
