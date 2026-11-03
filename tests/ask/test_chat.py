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


def scripted(*rounds, rewrite=""):
    """A model that answers each round from the script and records what it
    saw; the rewrite call (no tools, its own instruction) answers `rewrite`
    and is recorded apart."""
    seen, rewrites = [], []

    def model(messages, tools):
        if messages[0]["content"].startswith("You write searches"):
            rewrites.append(list(messages))
            return rewrite, [], {"role": "assistant", "content": rewrite}
        seen.append((list(messages), tools))
        content, calls = rounds[min(len(seen), len(rounds)) - 1]
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]
        return content, calls, assistant
    model.seen = seen
    model.rewrites = rewrites
    return model


def test_louise_searches_the_question_and_its_rewrites_first_and_the_answer_cites_by_number(chat_model):
    """The operator, 2026-11-03: "fix the way any model interfaces with
    louise". München's five3: Qwen3-8B sent the question verbatim, once,
    and never searched again. Louise searches it as the Ask tab does, and the
    titles the model names, before the model answers."""
    model = scripted(("Dumas wrote it [1][3]; see also [9].", []),
                     rewrite="Searches:\n1. Le Collier de la Reine Alexandre Dumas\n- \"The Queen's Necklace\"\n"
                             "Le collier de la reine alexandre dumas\nwho wrote the necklace novel?")
    called = []

    def call(name, args):
        called.append((name, args))
        if args["query"] == "The Queen's Necklace":
            return {"lists": [{"hits": [{"uid": "gutenberg:2701", "title": "Moby Dick", "lang": "en",
                                         "text": "again"},
                                        {"uid": "gutenberg:2759", "title": "The Queen's Necklace", "lang": "en",
                                         "text": "Cagliostro"}]}]}
        return SEARCH_RESULT
    out = chat.ask("who wrote the necklace novel?", collections=["gutenberg"], call=call, model=model,
                   history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                            {"role": "tool", "content": "never replayed"}])
    assert out["rewrites"] == ["Le Collier de la Reine Alexandre Dumas", "The Queen's Necklace"], \
        "numbering, quotes, headings, repeats and the question itself taken off"
    assert sorted(called, key=lambda c: c[1]["query"]) == sorted([
        ("louise_search", {"query": "who wrote the necklace novel?", "translate": True, "collections": ["gutenberg"]}),
        ("louise_search", {"query": "Le Collier de la Reine Alexandre Dumas", "translate": False,
                           "collections": ["gutenberg"]}),
        ("louise_search", {"query": "The Queen's Necklace", "translate": False, "collections": ["gutenberg"]})],
        key=lambda c: c[1]["query"]), "the question translated as on the Ask tab; a title searched as written"
    assert [t["by"] for t in out["turns"]] == ["louise", "louise:rewrite", "louise:rewrite"]
    assert out["rounds"] == 1 and not out["exhausted"] and out["answer"].startswith("Dumas wrote it")
    assert [s["uid"] for s in out["sources"]] == ["gutenberg:2701", "pubmed:1", "gutenberg:2759"]
    assert out["cited"] == [{"n": 1, "uid": "gutenberg:2701", "title": "Moby Dick"},
                            {"n": 3, "uid": "gutenberg:2759", "title": "The Queen's Necklace"}]
    assert out["cited_unknown"] == ["[9]"], "a number never shown is said, not resolved"
    first, tools = model.seen[0]
    assert first[0]["role"] == "system" and "[2][5]" in first[0]["content"] and "gutenberg" in first[0]["content"]
    assert [m["role"] for m in first[1:]] == ["user", "assistant", "user"], "history kept, a tool message not replayed"
    asked = first[-1]["content"]
    assert asked.startswith("Sources:") and asked.endswith("Question: who wrote the necklace novel?")
    assert "Search: «who wrote the necklace novel?» and its translations\n\n[1] Moby Dick (en) · gutenberg:2701" in asked
    assert "[1] Moby Dick — shown above" in asked and "[3] The Queen's Necklace (en) · gutenberg:2759\nCagliostro" in asked
    assert "· unverified\n(the text is not shown here; the origin: https://pubmed.ncbi.nlm.nih.gov/1/)" in asked, \
        "red: the origin's page, as 05 §5.3 says"
    assert ("x" * chat.TEXT_CHARS + " …") in asked and "x" * (chat.TEXT_CHARS + 1) not in asked, "the text trimmed"
    assert "Withheld here: x (quarantine)" in asked and "sha256:" not in asked, "no ids the model cannot use"
    assert [t["function"]["name"] for t in tools] == ["louise_search", "louise_lookup", "louise_traverse"]
    assert model.rewrites[0][-1] == {"role": "user", "content": "who wrote the necklace novel?"}


def test_the_model_says_only_what_it_can_know(chat_model):
    """CLAUDE.md RULE 2, for a model: what to search for and where. München's
    five3: `"space": "gutenberg"` and `"limit": 10` beside the query."""
    params = {t["function"]["name"]: t["function"]["parameters"] for t in chat.tool_specs()}
    assert set(params["louise_search"]["properties"]) == {"query", "collections"}
    assert params["louise_search"]["required"] == ["query"]
    assert set(params["louise_traverse"]["properties"]) == {"id", "relation", "depth"}
    called = []
    model = scripted(("", [llm.ToolCall(None, "louise_search", {"query": "a", "space": "gutenberg", "limit": 3,
                                                               "translate": False})]), ("ok", []))
    out = chat.ask("q", collections=["gutenberg"], call=lambda n, a: called.append(a) or {"lists": []},
                   model=model)
    assert called[-1] == {"query": "a", "collections": ["gutenberg"], "translate": True}
    assert out["turns"][-1]["by"] == "model" and out["turns"][-1]["dropped"] == ["limit", "space", "translate"]


def test_a_search_adds_at_most_its_share_and_a_record_keeps_its_number(chat_model):
    """Each rewrite brings at most REWRITE_HITS new sources; a record found
    again is its number, not its text again."""
    model = scripted(("", [llm.ToolCall(None, "louise_search", {"query": "more"})]), ("ok [12]", []),
                     rewrite="a\nb")
    out = chat.ask("q", call=lambda n, a: _big_search(0 if a["query"] in ("q", "more") else ord(a["query"])),
                   model=model)
    by = [t["by"] for t in out["turns"]]
    assert by == ["louise", "louise:rewrite", "louise:rewrite", "model"]
    assert len(out["sources"]) == 10 + 2 * chat.REWRITE_HITS
    shown = model.seen[1][0][-1]["content"]
    assert shown.count("— shown above") == 10, "the model's search found the question's ten again"
    assert out["cited"] == [{"n": 12, "uid": "gutenberg:971", "title": "Book 1"}]


def test_a_rewrite_that_fails_leaves_the_question_searched(chat_model):
    import httpx

    def model(messages, tools):
        if messages[0]["content"].startswith("You write searches"):
            raise httpx.ConnectError("down")
        return "From [1].", [], {"role": "assistant", "content": "From [1]."}
    out = chat.ask("q", call=lambda n, a: SEARCH_RESULT, model=model)
    assert out["rewrites"] == [] and [t["by"] for t in out["turns"]] == ["louise"]
    assert out["cited"][0]["uid"] == "gutenberg:2701"


def test_citations_read_every_form_the_models_write():
    src = chat.Sources()
    for i in range(1, 5):
        src.number({"uid": f"gutenberg:{i}", "title": f"B{i}"})
    src.ids.add("hgnc:1101")
    cited, unknown = src.cited("a [2, 3] b [1-3] c [4;2] d [gutenberg:1] e [hgnc:1101] f [7] g [x:y] [2]")
    assert [c.get("n", c.get("id")) for c in cited] == [2, 3, 1, 4, "hgnc:1101"]
    assert unknown == ["[7]", "[x:y]"]


def test_rounds_are_bounded_then_the_model_answers_without_tools(chat_model):
    lookup = llm.ToolCall("c1", "louise_lookup", {"id": "hgnc:1101"})
    model = scripted(("", [lookup]), ("", [lookup]), ("BRCA2 is on 13q13.1.", []))
    out = chat.ask("where is BRCA2?", call=lambda n, a: {"rows": []}, model=model, max_rounds=2)
    assert out["exhausted"] and out["rounds"] == 2 and [t["by"] for t in out["turns"]] == ["louise", "model", "model"]
    assert out["answer"] == "BRCA2 is on 13q13.1."
    assert model.seen[-1][1] is None, "past the bound the tools are withheld"


def test_a_tool_s_refusal_is_handed_back_not_raised(chat_model):
    bad = llm.ToolCall(None, "louise_lookup", {"id": "nocolon"})
    nope = llm.ToolCall(None, "louise_summon", {})
    model = scripted(("", [bad, nope]), ("Nothing answers.", []))

    def call(name, args):
        if name == "louise_summon":
            raise KeyError(name)
        if name == "louise_lookup":
            raise CodexError("a canonical id is <namespace>:<id>")
        return {"lists": []}
    out = chat.ask("q", call=call, model=model)
    assert [t["error"] for t in out["turns"]] == [None, "a canonical id is <namespace>:<id>",
                                                 "no tool named 'louise_summon'"]
    second, _ = model.seen[1]
    assert json.loads(second[-2]["content"]) == {"error": "a canonical id is <namespace>:<id>"}
    assert out["answer"] == "Nothing answers."


def test_a_search_louise_cannot_run_is_said_to_the_model(chat_model):
    def call(name, args):
        raise CodexError("no collection named nowhere")
    model = scripted(("Nothing.", []))
    out = chat.ask("q", collections=["nowhere"], call=call, model=model)
    assert out["turns"][0]["error"] == "no collection named nowhere"
    assert "Search: «q» refused: no collection named nowhere" in model.seen[0][0][-1]["content"]


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


def test_an_answer_says_where_its_time_went(chat_model):
    """Each model round's wall time, the rewrite's and each search's, on the
    answer (tools/measure/chat_eval.py reads them; operator, 2026-11-03)."""
    lookup = llm.ToolCall("c1", "louise_lookup", {"id": "hgnc:1101"})
    model = scripted(("", [lookup]), ("", [lookup]), ("13q13.1.", []))
    out = chat.ask("where is BRCA2?", call=lambda n, a: {"rows": []}, model=model, max_rounds=1)
    assert out["exhausted"] and len(out["model_s"]) == 2, "the bound's last round is timed too"
    assert isinstance(out["rewrite_s"], float)
    assert all(isinstance(t["seconds"], float) for t in out["turns"]) and len(out["turns"]) == 2


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
    model = scripted(*([("", [search])] * 9), ("done [61].", []))
    n = iter(range(100))
    out = chat.ask("q", call=lambda name, a: _big_search(next(n)), model=model, max_rounds=10)
    budget = chat.CONTEXT_TOKENS - chat.ANSWER_TOKENS - chat.MARGIN_TOKENS
    assert all(chat.request_tokens(msgs, tools) <= budget for msgs, tools in model.seen)
    assert out["slimmed"] > 0 and out["answer"] == "done [61]." and out["cited"][0]["n"] == 61
    last_msgs, _ = model.seen[-1]
    tool_msgs = [m["content"] for m in last_msgs if m["role"] == "tool"]
    first, latest = tool_msgs[0], tool_msgs[-1]
    assert first.endswith(chat.SLIMMED_TEXT) and "word" not in first
    assert first.split("\n")[:2] == ["Search: «q» and its translations", "[11] Book 0 (en) · gutenberg:10"], \
        "citable, without text"
    assert chat.SLIMMED_TEXT not in latest and "word word" in latest


def test_a_request_the_server_refuses_as_too_large_is_answered_with_what_it_has(chat_model):
    """A 400 after the tools have run: every result slimmed, tools withheld,
    one more ask. The answer is never lost because the searches returned a lot."""
    import httpx
    search = llm.ToolCall(None, "louise_search", {"query": "q"})
    calls = []

    def model(messages, tools):
        if messages[0]["content"].startswith("You write searches"):
            return "", [], {}
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


@pytest.mark.parametrize("text", [OVER, '{"error": "bad template"}'])
def test_any_other_refusal_is_raised_not_answered_around(chat_model, text):
    """Only the context refusal is answered around, once: a 400 for another
    reason, or one the slimmed request still gets, is a fault to see."""
    import httpx

    def model(messages, tools):
        if messages[0]["content"].startswith("You write searches"):
            return "", [], {}
        raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(400, text=text))
    with pytest.raises(httpx.HTTPStatusError):
        chat.ask("q", call=lambda n, a: _big_search(0), model=model)


def test_louise_s_searches_run_in_the_asking_thread_s_context(chat_model):
    """The searches run on pool threads; the reader's cancel token
    (app/cancel.py) and every other context variable go with them."""
    import contextvars
    var = contextvars.ContextVar("test_var", default=None)
    var.set("the reader's")
    seen = []
    model = scripted(("ok", []), rewrite="a title")
    chat.ask("q", call=lambda n, a: seen.append(var.get()) or {"lists": []}, model=model)
    assert seen == ["the reader's", "the reader's"]
