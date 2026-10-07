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
    assert called == [("louise_search", {"query": "white whale", "collections": ["gutenberg"]})], \
        "the collections asked for reach the tool"
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
