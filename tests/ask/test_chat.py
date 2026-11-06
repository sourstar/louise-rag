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


def test_louise_searches_the_question_first_and_the_answer_cites_by_number(chat_model):
    """The operator, 2026-11-03: "fix the way any model interfaces with
    louise". München's five3: Qwen3-8B sent the question verbatim, once.
    Louise searches it as the Ask tab does before the model answers, and
    asks the model for nothing before that (the rewrite step was walked and
    removed: chat.py's point 2)."""
    model = scripted(("Ahab [1]; see also [9].", []))
    called = []

    def call(name, args):
        called.append((name, args))
        return SEARCH_RESULT
    out = chat.ask("who hunts the whale?", collections=["gutenberg"], call=call, model=model,
                   history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"},
                            {"role": "tool", "content": "never replayed"}])
    assert called == [("louise_search", {"query": "who hunts the whale?", "translate": True,
                                         "collections": ["gutenberg"]})], "as on the Ask tab, once"
    assert len(model.seen) == 1, "no call to the model before the answer"
    assert [t["by"] for t in out["turns"]] == ["louise"]
    assert out["rounds"] == 1 and not out["exhausted"] and out["answer"].startswith("Ahab")
    assert [s["uid"] for s in out["sources"]] == ["gutenberg:2701", "pubmed:1"]
    assert out["cited"] == [{"n": 1, "uid": "gutenberg:2701", "title": "Moby Dick"}]
    assert out["cited_unknown"] == ["[9]"], "a number never shown is said, not resolved"
    first, tools = model.seen[0]
    system = first[0]["content"]
    assert first[0]["role"] == "system" and "[2][5]" in system and "gutenberg" in system
    assert "never supply it from memory" in system, "the walk: a title the sources lack became the answer"
    assert [m["role"] for m in first[1:]] == ["user", "assistant", "user"], "history kept, a tool message not replayed"
    asked = first[-1]["content"]
    assert asked.startswith("Sources:") and asked.endswith("Question: who hunts the whale?")
    assert "Search: «who hunts the whale?» and its translations\n\n[1] Moby Dick (en) · gutenberg:2701" in asked
    assert "· unverified\n(the text is not shown here; the origin: https://pubmed.ncbi.nlm.nih.gov/1/)" in asked, \
        "red: the origin's page, as 05 §5.3 says"
    assert ("x" * chat.TEXT_CHARS + " …") in asked and "x" * (chat.TEXT_CHARS + 1) not in asked, "the text trimmed"
    assert "Withheld here: x (quarantine)" in asked and "sha256:" not in asked, "no ids the model cannot use"
    assert [t["function"]["name"] for t in tools] == ["louise_search", "louise_lookup", "louise_traverse"]


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


def test_a_record_found_again_keeps_its_number(chat_model):
    """A record a later search finds again is its number, not its text again."""
    model = scripted(("", [llm.ToolCall(None, "louise_search", {"query": "more"})]), ("ok [3]", []))
    out = chat.ask("q", call=lambda n, a: _big_search(0), model=model)
    assert [t["by"] for t in out["turns"]] == ["louise", "model"] and len(out["sources"]) == 10
    assert model.seen[1][0][-1]["content"].count("— shown above") == 10
    assert out["cited"] == [{"n": 3, "uid": "gutenberg:02", "title": "Book 2"}]


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
    """Each model round's wall time and each search's, on the
    answer (tools/measure/chat_eval.py reads them; operator, 2026-11-03)."""
    lookup = llm.ToolCall("c1", "louise_lookup", {"id": "hgnc:1101"})
    model = scripted(("", [lookup]), ("", [lookup]), ("13q13.1.", []))
    out = chat.ask("where is BRCA2?", call=lambda n, a: {"rows": []}, model=model, max_rounds=1)
    assert out["exhausted"] and len(out["model_s"]) == 2, "the bound's last round is timed too"
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
        raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(400, text=text))
    with pytest.raises(httpx.HTTPStatusError):
        chat.ask("q", call=lambda n, a: _big_search(0), model=model)


def test_the_model_reads_who_wrote_each_source(chat_model):
    """Operator, 2026-11-03, A: a source shows its authors (05 §5.3's
    `authors`), so "who wrote it?" is answered from the record, not from
    the model's memory."""
    result = {"lists": [{"hits": [{"uid": "gutenberg:2759", "title": "Le Collier de la Reine",
                                   "authors": "Dumas, Alexandre, 1802-1870", "lang": "fr", "text": "t"},
                                  {"uid": "gutenberg:1", "title": "Anon", "lang": "en", "text": "t"}]}]}
    model = scripted(("Dumas [1].", []))
    out = chat.ask("q", call=lambda n, a: result, model=model)
    asked = model.seen[0][0][-1]["content"]
    assert "[1] Le Collier de la Reine — Dumas, Alexandre, 1802-1870 (fr) · gutenberg:2759" in asked
    assert "[2] Anon (en) · gutenberg:1" in asked, "no authors, no dash"
    assert out["sources"][0]["authors"] == "Dumas, Alexandre, 1802-1870"


def test_a_tool_call_the_engine_cannot_parse_is_answered_without_tools(chat_model):
    """2026-11-03, München: Command R7B on our llama.cpp — LocalAI answered
    500 ("Unexpected error in RPC handling") whenever the model chose a tool,
    the generic chat parser throwing on its output. The round is answered
    without tools, from the sources Louise searched; the answer says so."""
    import httpx

    def failing(status):
        def model(messages, tools):
            if tools:
                raise httpx.HTTPStatusError(str(status), request=httpx.Request("POST", "http://x"),
                                            response=httpx.Response(status, text="Unexpected error in RPC handling"))
            return "From the sources: Moby Dick [1].", [], {"role": "assistant", "content": ""}
        return model
    out = chat.ask("q", call=lambda n, a: SEARCH_RESULT, model=failing(500))
    assert out["answer"] == "From the sources: Moby Dick [1]." and out["exhausted"]
    assert out["tools_failed"].startswith("500: Unexpected error")
    assert out["cited"][0]["uid"] == "gutenberg:2701"
    with pytest.raises(httpx.HTTPStatusError):
        chat.ask("q", call=lambda n, a: SEARCH_RESULT, model=failing(422)), "a client error is a fault to see"

    def down(messages, tools):
        raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"),
                                    response=httpx.Response(500, text="down"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        chat.ask("q", call=lambda n, a: SEARCH_RESULT, model=down), "failing without tools too: raised"
    # 2026-11-06, Command R7B's pilot: both requests refused, and only the
    # second one's error was left; the first one's body goes with it now
    assert raised.value.__notes__ == ["the request with tools failed first: 500: down"]


def test_ask_sends_no_temperature_and_each_model_runs_its_own_file(fake_chat, monkeypatch):
    """2026-11-03: Ask sent 0.2 to every chat model, and Qwen3.6-35B-A3B
    looped at it; its card says 0.7. The model file decides now; Qwen3-8B's
    file carries the 0.2 it was measured at."""
    import json as _json
    from pathlib import Path
    monkeypatch.setattr(config, "CHAT_API", "openai")
    fake_chat["answer"] = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    llm.chat_tools([{"role": "user", "content": "q"}], None)
    assert "temperature" not in fake_chat["body"]
    llm.chat_tools([{"role": "user", "content": "q"}], None, temperature=0.4)
    assert fake_chat["body"]["temperature"] == 0.4, "a caller that wants one still passes it"
    root = Path(__file__).resolve().parents[2] / "deploy" / "vega" / "stage" / "models"
    text = (root / "louise-chat-qwen3-8b.yaml").read_text()
    cfg = _json.loads("\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")))
    assert cfg["parameters"]["temperature"] == 0.2, "the live model runs as it was measured"


def test_a_caller_s_sampling_reaches_the_request_in_both_dialects(fake_chat, monkeypatch):
    """The chat benchmark's arms (2026-11-06): values sent over the model
    file for this request only; a name the client does not know is refused,
    never sent to be ignored."""
    arm = {"temperature": 0.1, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
    monkeypatch.setattr(config, "CHAT_API", "openai")
    fake_chat["answer"] = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
    llm.chat_tools([{"role": "user", "content": "q"}], None, sampling=arm)
    assert {k: fake_chat["body"][k] for k in arm} == arm
    llm.chat_tools([{"role": "user", "content": "q"}], None, temperature=0.4, sampling={"temperature": 0.1})
    assert fake_chat["body"]["temperature"] == 0.1, "the arm's value wins"
    monkeypatch.setattr(config, "CHAT_API", "ollama")
    fake_chat["answer"] = {"message": {"role": "assistant", "content": "ok"}}
    llm.chat_tools([{"role": "user", "content": "q"}], None, sampling=arm)
    assert {k: fake_chat["body"]["options"][k] for k in arm} == arm
    with pytest.raises(ValueError, match="mirostat"):
        llm.chat_tools([{"role": "user", "content": "q"}], None, sampling={"mirostat": 2})


def cohere_default_template(messages):
    """Cohere's default template (Command R7B's GGUF, line 174): after the
    system turn, user and assistant strictly alternate, user first, or it
    raises — LocalAI's 500, measured on the node 2026-11-06."""
    import httpx
    turns = [m for m in messages if m["role"] != "system"]
    for i, m in enumerate(turns):
        if (m["role"] == "user") != (i % 2 == 0) or m["role"] not in ("user", "assistant"):
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(
                500, text="Jinja Exception: Conversation roles must alternate user/assistant/user/assistant/..."))


def test_the_answer_without_tools_is_asked_on_turns_a_template_without_tools_renders(chat_model):
    """2026-11-06, Command R7B's pilots: 8 answers of 45 failed. Past the
    bound, or after a tool round the engine refused, Ask asked again with
    no tools on the conversation with its tool turns; with no tools the
    engine renders the DEFAULT template, and Cohere's raises on a tool turn.
    The tool answers now join the sources; the roles alternate."""
    search = llm.ToolCall("c1", "louise_search", {"query": "Collier de la reine"})
    seen = []

    def model(messages, tools):
        seen.append((messages, tools))
        if tools:
            return "", [search], {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "louise_search", "arguments": {"query": "Collier de la reine"}}}]}
        cohere_default_template(messages)
        return "Le Collier de la reine [1].", [], {"role": "assistant", "content": "Le Collier de la reine [1]."}
    out = chat.ask("Dumas, le collier?", call=lambda n, a: SEARCH_RESULT, model=model, max_rounds=2)
    assert out["answer"] == "Le Collier de la reine [1]." and out["exhausted"]
    last, tools = seen[-1]
    assert tools is None and [m["role"] for m in last] == ["system", "user"]
    assert last[1]["content"].count("Search: «") == 3, "Louise's search and the model's two, as sources"
    assert last[1]["content"].endswith("Question: Dumas, le collier?")
    assert [m["role"] for m in seen[-2][0]][-2:] == ["assistant", "tool"], "the loop's own turns are kept"


def test_an_answer_now_follows_the_model_s_own_turn(chat_model):
    """A model that answers with a tool call written as text is told to
    answer now AFTER its own turn: two user turns in a row are refused by
    Cohere's template too."""
    import httpx
    seen = []

    def model(messages, tools):
        seen.append(messages)
        if tools:
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(500, text="Unexpected error in RPC handling"))
        cohere_default_template(messages)
        if len(seen) == 2:
            return '<tool_call>{"name": "louise_search"}</tool_call>', [], {}
        return "From the sources: Moby Dick [1].", [], {}
    out = chat.ask("q", call=lambda n, a: SEARCH_RESULT, model=model)
    assert out["answer"] == "From the sources: Moby Dick [1]." and out["tools_failed"].startswith("500")
    assert [m["role"] for m in seen[-1]] == ["system", "user", "assistant", "user"]
    assert seen[-1][-1]["content"] == chat.ANSWER_NOW


def test_without_tool_turns_leaves_the_conversation_as_it_was():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Sources:\n\nA\n\nQuestion: q"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "B"}]
    flat = chat.without_tool_turns(msgs)
    assert flat == [{"role": "system", "content": "S"},
                    {"role": "user", "content": "Sources:\n\nA\n\n---\n\nB\n\nQuestion: q"}]
    assert len(msgs) == 4 and msgs[1]["content"] == "Sources:\n\nA\n\nQuestion: q"
