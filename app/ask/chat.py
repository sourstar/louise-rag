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
from typing import Any, Callable

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
        for k in ("facts", "unresolved", "withheld", "legs_withheld", "as_of", "filtered"):
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


def ask(question: str, *, collections: list[str] | None = None, history: list[dict[str, Any]] | None = None,
        call: Callable[[str, dict[str, Any]], Any] | None = None, model=None,
        max_rounds: int = MAX_ROUNDS) -> dict[str, Any]:
    """One question through the loop: `{answer, turns: [{tool, arguments,
    result}], rounds, exhausted}`. `model(messages, tools) -> (content,
    calls, assistant_message)` is llm.chat_tools unless a test gives
    another; `call(name, args)` is the MCP dispatcher."""
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
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in history or []:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})
    tools = tool_specs()
    turns: list[dict[str, Any]] = []
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        content, calls, assistant = model(messages, tools)
        if not calls:
            return {"answer": content, "turns": turns, "rounds": rounds, "exhausted": False}
        messages.append(assistant)
        for c in calls:
            args = c.arguments if isinstance(c.arguments, dict) else {}
            if collections and c.name == "louise_search" and not args.get("collections"):
                args = {**args, "collections": list(collections)}
            try:
                result = call(c.name, args)
                shown = compact(c.name, result)
                error = None
            except CodexError as e:
                result, shown, error = None, {"error": str(e)}, str(e)
            except KeyError:
                result, shown, error = None, {"error": f"no tool named {c.name!r}"}, f"no tool named {c.name!r}"
            turns.append({"tool": c.name, "arguments": args, "result": result, "error": error})
            messages.append(llm.tool_message(c, json.dumps(shown, ensure_ascii=False, default=str)))
    # the bound reached: the model answers with what it has, tools withheld
    content, _, _ = model(messages, None)
    return {"answer": content, "turns": turns, "rounds": rounds, "exhausted": True}
