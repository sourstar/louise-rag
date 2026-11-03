"""`ask chat` (05 §5.4; 07 §7.3): the chat model with the three tools —
`louise_search`, `louise_lookup`, `louise_traverse` — and nothing else.
"There is no separate classifier to train and keep current; the tool
descriptions are the routing." Migration step 11.

The model is the node's chat model (`CHAT_URL`, 07 §7.8; unset refuses,
exit 2), spoken to through app/llm.py in whichever protocol it has, with
the tools declared as functions. Each round the model either calls tools
— run here through the same dispatcher MCP uses (app/ask/mcp.py), their
answers compacted for a context window and handed back as tool messages
— or answers. The answer cites what the tools returned; every tool call
and its compacted result travel with it, so a reader can see what the
model saw. Rounds are bounded; past the bound the model is asked to
answer with what it has.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from app.hold.codex import CodexError

MAX_ROUNDS = 6
TEXT_CHARS = 600            # of a result's text the model sees
HITS_PER_LIST = 10
SYSTEM = (
    "You are Louise, a research assistant over the collections this peer holds. You have three tools and "
    "choose among them: `louise_search` for a semantic or exact-term question over the records (one fused "
    "list per space; each result names its collection, snapshot, record uid, trust level and whether its "
    "vector was verified here); `louise_lookup` for a fact by canonical id (<namespace>:<id>) from a "
    "reference source — authoritative structured data, never ranked; `louise_traverse` to follow the "
    "declared relations from a record or id. Call tools as the question needs, several rounds if useful, "
    "then answer ONLY from what the tools returned. Cite each claim with the result's uid in square "
    "brackets, e.g. [pubmed:37000021]. Say which snapshot answered when the question is about a date. "
    "Say plainly when the tools returned nothing that answers, when a collection was withheld, and when "
    "the evidence is unverified. Never invent a record."
)


class ChatError(CodexError):
    exit_code = 2


def tool_specs() -> list[dict[str, Any]]:
    """The three tools of 05 §5.4 as the model's function declarations —
    the MCP declarations, one to one."""
    from app.ask import mcp
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t["inputSchema"]}} for t in mcp.TOOLS]


def _trim(text: Any) -> Any:
    if isinstance(text, str) and len(text) > TEXT_CHARS:
        return text[:TEXT_CHARS].rstrip() + " …"
    return text


def compact(name: str, result: Any) -> Any:
    """What the model sees of a tool's answer: the fields it can cite and
    reason from, the text trimmed, the lists cut — never the whole
    object, which is the caller's (05 §5.3 travels in `turns`)."""
    if name == "louise_search" and isinstance(result, dict):
        lists = []
        for lst in result.get("lists", []):
            hits = []
            for h in lst.get("hits", [])[:HITS_PER_LIST]:
                hit = {k: h.get(k) for k in ("uid", "snapshot_id", "rank", "title", "lang", "trust_level",
                                             "verified_on_use", "legs") if k in h}
                if "text" in h:
                    hit["text"] = _trim(h.get("text"))
                if h.get("origin_url"):
                    hit["origin_url"] = h["origin_url"]
                hits.append(hit)
            lists.append({"space_id": lst.get("space_id"), "hits": hits})
        out: dict[str, Any] = {"lists": lists}
        for k in ("facts", "unresolved", "withheld", "legs_withheld", "rerank_withheld",
                  "as_of", "filtered"):
            v = result.get(k)
            if v:
                out[k] = v
        return out
    if name == "louise_traverse" and isinstance(result, dict):
        out = dict(result)
        if isinstance(out.get("nodes"), list):
            out["nodes"] = out["nodes"][:50]
        if isinstance(out.get("edges"), list):
            out["edges"] = out["edges"][:100]
        return out
    return result


#: THE CONVERSATION FITS THE MODEL'S CONTEXT (2026-11-03, Ask's benchmark: 7 of
#: 50 answers failed with a 400 once several searches' results outgrew it).
#: The chat model's context_size (every louise-chat-* model file), the room kept
#: for its answer (llm.chat_tools' max_tokens) and for its template's own tokens.
CONTEXT_TOKENS = 16384
ANSWER_TOKENS = 1500
MARGIN_TOKENS = 512
#: characters per token assumed when sizing a request: low on purpose, so the
#: estimate errs large (European text runs ~4; a refused request is the fault)
CHARS_PER_TOKEN = 3.0
#: what is left of a tool answer that is not a search's, once slimmed
SLIM_CHARS = 1200


def request_tokens(messages: list[dict[str, Any]], tools: Any) -> int:
    """The request's size, estimated from its characters."""
    chars = len(json.dumps(messages, ensure_ascii=False, default=str))
    chars += len(json.dumps(tools, ensure_ascii=False)) if tools else 0
    return int(chars / CHARS_PER_TOKEN)


def slim(content: str) -> str:
    """A tool answer reduced to what can still be cited: a search's hits keep
    their uid, title, rank and language and lose their text; anything else
    is cut to SLIM_CHARS. The model is told it was slimmed."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("lists"), list):
        lists = [{"space_id": lst.get("space_id"),
                  "hits": [{k: h[k] for k in ("uid", "title", "rank", "lang") if k in h}
                           for h in lst.get("hits", [])]} for lst in data["lists"]]
        return json.dumps({"lists": lists, "slimmed": "texts dropped to fit the context"},
                          ensure_ascii=False)
    if len(content) <= SLIM_CHARS:
        return content
    return json.dumps({"slimmed": "cut to fit the context", "start": content[:SLIM_CHARS]},
                      ensure_ascii=False)


def fit(messages: list[dict[str, Any]], tools: Any, *, everything: bool = False) -> int:
    """Slim the tool answers, oldest first and the latest last, until the
    request fits CONTEXT_TOKENS with room for the answer; `everything` slims
    them all. Returns how many were slimmed now."""
    budget = CONTEXT_TOKENS - ANSWER_TOKENS - MARGIN_TOKENS
    done = 0
    for i, m in enumerate(messages):
        if not everything and request_tokens(messages, tools) <= budget:
            break
        if m.get("role") != "tool" or '"slimmed"' in str(m.get("content", "")):
            continue
        new = slim(str(m.get("content", "")))
        if new != m.get("content"):
            messages[i] = {**m, "content": new}
            done += 1
    return done


def reader_language(question: str, answer_lang: str | None = None) -> str | None:
    """The language the answer is written in: the one the reader asked for
    (the page's "show results in"), else the one the question is written in
    by the node's own guess (05 §5.6's reader's language), else None — the
    question does not say clearly enough, and the prompt asks for its
    language without naming one."""
    if answer_lang and answer_lang.strip():
        return answer_lang.strip().lower()
    from app import query_language
    return query_language.guess(question, query_language.guessable())


def language_rule(lang: str | None) -> str:
    """The system prompt's last sentence. NAMED, never a bare code (llm.py
    LANGUAGE_NAMES: a code alone is echoed back). Measured 2026-11-03 on
    the chat benchmark before it: after English tool results Qwen3-8B
    answered in the question's language 66 % of the time and Bolt-E4B 19 %
    (German 0 of 10, French 0 of 10)."""
    from app import llm
    if lang is None:
        return (" Write your whole answer in the language the question is written in, whatever "
                "language the tool results are in.")
    return (f" Write your whole answer in {llm.language_phrase(lang)}, whatever language the "
            f"question or the tool results are in.")


def ask(question: str, *, collections: list[str] | None = None, history: list[dict[str, Any]] | None = None,
        call: Callable[[str, dict[str, Any]], Any] | None = None, model=None,
        max_rounds: int = MAX_ROUNDS, answer_lang: str | None = None) -> dict[str, Any]:
    """One question through the loop: `{answer, turns: [{tool, arguments,
    result, error, seconds}], rounds, exhausted, model_s}`. `model(messages,
    tools) -> (content, calls, assistant_message)` is llm.chat_tools unless a
    test gives another; `call(name, args)` is the MCP dispatcher.
    `model_s` is each model round's wall time and `seconds` each tool call's,
    so where an answer's time went is read off the answer itself
    (tools/measure/chat_eval.py; operator, 2026-11-03: "Use the chat
    comparison benchmark to work on the optimizations")."""
    from app import config, llm
    from app.ask import mcp
    if not isinstance(question, str) or not question.strip():
        raise ChatError("a question is a non-empty string")
    if not getattr(config, "CHAT_URL", None):
        raise ChatError("CHAT_URL unset: this node has no chat model, and `ask chat` needs one (07 §7.8)")
    model = model or llm.chat_tools
    call = call or mcp._call_tool
    system = SYSTEM
    if collections:
        system += (f" Search only these collections unless the question says otherwise: {', '.join(collections)}; "
                   f"pass them as `collections`.")
    lang = reader_language(question, answer_lang)
    system += language_rule(lang)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in history or []:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})
    tools = tool_specs()
    turns: list[dict[str, Any]] = []
    model_s: list[float] = []

    slimmed = 0

    def timed_model(tools_offered):
        nonlocal slimmed
        slimmed += fit(messages, tools_offered)
        t0 = time.monotonic()
        try:
            return model(messages, tools_offered)
        finally:
            model_s.append(round(time.monotonic() - t0, 3))

    def too_large(e: httpx.HTTPStatusError) -> bool:
        """The server's refusal of a request over its context — LocalAI's
        400 "request (17013 tokens) exceeds the available context size
        (16384 tokens)", measured on the node 2026-11-03 — and nothing else:
        any other 400 is a fault to see, not to answer around."""
        try:
            body = e.response.text
        except Exception:   # noqa: BLE001 — an unreadable body is not this refusal
            return False
        return e.response.status_code == 400 and bool(turns) and "context size" in body

    def answer_with_what_it_has(rounds: int) -> dict[str, Any]:
        """A request the server refused as too large: every tool answer
        slimmed, tools withheld, one more ask; never the whole answer lost
        because the searches returned a lot."""
        nonlocal slimmed
        slimmed += fit(messages, None, everything=True)
        content, _, _ = timed_model(None)
        return {"answer": content, "turns": turns, "rounds": rounds, "exhausted": True,
                "model_s": model_s, "answer_lang": lang, "slimmed": slimmed}
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        try:
            content, calls, assistant = timed_model(tools)
        except httpx.HTTPStatusError as e:
            if not too_large(e):
                raise
            return answer_with_what_it_has(rounds)
        if not calls:
            return {"answer": content, "turns": turns, "rounds": rounds, "exhausted": False,
                    "model_s": model_s, "answer_lang": lang, "slimmed": slimmed}
        messages.append(assistant)
        for c in calls:
            args = c.arguments if isinstance(c.arguments, dict) else {}
            if collections and c.name == "louise_search" and not args.get("collections"):
                args = {**args, "collections": list(collections)}
            if c.name == "louise_search" and "translate" not in args:
                # THE SEARCH THE ASK TAB RUNS (2026-11-03, the operator: "Compare louise web
                # gui ask tab with what the model is sending to louise"). The page searches
                # with translation on (web._ask_result: run_search(..., translate=True));
                # the tool's default is off (05 §5.2: on per request), and a model is not
                # told to ask for it, so an Italian question about a book held in French
                # was searched in Italian only. The loop asks for it on the model's behalf,
                # per request as 05 §5.2 says; a model that sets it keeps its choice.
                args = {**args, "translate": True}
            t0 = time.monotonic()
            try:
                result = call(c.name, args)
                shown = compact(c.name, result)
                error = None
            except CodexError as e:
                result, shown, error = None, {"error": str(e)}, str(e)
            except KeyError:
                result, shown, error = None, {"error": f"no tool named {c.name!r}"}, f"no tool named {c.name!r}"
            turns.append({"tool": c.name, "arguments": args, "result": result, "error": error,
                          "seconds": round(time.monotonic() - t0, 3)})
            messages.append(llm.tool_message(c, json.dumps(shown, ensure_ascii=False, default=str)))
    # the bound reached: the model answers with what it has, tools withheld
    try:
        content, _, _ = timed_model(None)
    except httpx.HTTPStatusError as e:
        if not too_large(e):
            raise
        return answer_with_what_it_has(rounds)
    return {"answer": content, "turns": turns, "rounds": rounds, "exhausted": True,
            "model_s": model_s, "answer_lang": lang, "slimmed": slimmed}
