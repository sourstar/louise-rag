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

THE INTERFACE (operator, 2026-11-03: "fix the way any model interfaces
with louise"). Before it, the node's chat benchmark (München, five3: 39
answers of Qwen3-8B with their calls) showed every model sending the
question VERBATIM as one search and never searching again when the work
was not in the results — what was found depended on the question as
typed, never on the model; a 2B model never called the tool at all. So
Louise does what it can do for any model, and asks the model only for what
Louise cannot know:

1. LOUISE SEARCHES FIRST, as the Ask tab does: the question as typed with
   its translations (web._ask_result), before the model's first round, so
   a model that never calls a tool still answers from sources.
2. NO MODEL REWRITE BEFORE IT (tried and walked 2026-11-03, removed the
   same day). Rewrite-Retrieve-Read (Ma et al., EMNLP 2023) asked the model
   for the searches the question implies. Walked on the node with Qwen3-8B
   (München): its titles were wrong for both questions ("Le Collier de
   diamants", "La Tour de Nesle" for Le Collier de la Reine), the wrong
   title then became the answer, from memory, and the four searches side
   by side took 10-23 s each against one another. A rewrite is only as good
   as the model's knowledge; a model that knows the title still searches
   for it with the tool (the system prompt says so), one search at a time.
3. NUMBERED SOURCES, as the Ask tab's answer (retrieval.
   build_answer_messages; the form of the ALCE benchmark, Gao et al.,
   EMNLP 2023): every record shown is [n], the same n whenever it comes
   back; the model cites [n], and the answer carries what each n is.
4. THE MODEL SAYS WHAT ONLY IT CAN KNOW (CLAUDE.md RULE 2): what to search
   for, and in which collections. Translation, depth, spaces, annotations
   are Louise's to set; an argument the model invents is dropped, and named
   in the turn.

The three tools stay offered, so a model that wants another search, a
lookup or a traversal still calls it. MCP's own declarations (05 §5.4) are
unchanged: a client model there sees every parameter.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.hold.codex import CodexError

MAX_ROUNDS = 6
TEXT_CHARS = 600            # of a result's text the model sees
HITS_PER_LIST = 10
SYSTEM = (
    "You are Louise, a research assistant over the collections this peer holds. Louise has already "
    "searched the reader's question: the numbered sources come with the question. Answer from the sources, and cite each claim with its source's number in square "
    "brackets, e.g. [2] or [2][5]; a fact from a reference source is cited by its id, e.g. [hgnc:1101]. "
    "When the sources do not answer, search again with `louise_search` — the work's title and author, "
    "other words, another language — and cite what it finds by its number. `louise_lookup` gives a fact "
    "by its canonical id (<namespace>:<id>); `louise_traverse` follows the declared relations from a "
    "record's uid. Use only what the sources say: never add a name, a date or a number they do not give, "
    "never cite a number you were not shown, and never invent a record. When no source is the work, the "
    "person or the fact asked for, say that the collections searched do not show it and name the "
    "closest sources; never supply it from memory. Say plainly when a collection was withheld and when "
    "the evidence is unverified."
)


class ChatError(CodexError):
    exit_code = 2


#: what the model may say to each tool (the module's point 4); every other
#: parameter of 05 §5.4 is Louise's to set
MODEL_ARGS: dict[str, tuple[str, ...]] = {
    "louise_search": ("query", "collections"),
    "louise_lookup": ("id",),
    "louise_traverse": ("id", "relation", "depth"),
}
QUERY_SAID = ("what to look for, in any language: a work's title and author, a name, key terms, or "
              "the reader's own words")


def tool_specs() -> list[dict[str, Any]]:
    """The three tools of 05 §5.4 as the model's function declarations: the
    MCP declarations, with only the parameters the model can know."""
    from app.ask import mcp
    out = []
    for t in mcp.TOOLS:
        schema = t["inputSchema"]
        keep = MODEL_ARGS.get(t["name"], tuple(schema.get("properties", {})))
        props = {k: dict(v) for k, v in schema.get("properties", {}).items() if k in keep}
        if t["name"] == "louise_search":
            props["query"] = {**props["query"], "description": QUERY_SAID}
        out.append({"type": "function", "function": {
            "name": t["name"], "description": t["description"],
            "parameters": {"type": "object", "properties": props,
                           "required": [r for r in schema.get("required", []) if r in keep]}}})
    return out


def model_args(name: str, args: dict[str, Any], collections: list[str] | None
               ) -> tuple[dict[str, Any], list[str]]:
    """A call as the model made it → as Louise runs it, and the arguments
    dropped. A search is translated as the Ask tab's is (web._ask_result)
    and kept to the reader's collections when the model names none."""
    keep = MODEL_ARGS.get(name)
    if keep is None:
        return args, []
    dropped = sorted(k for k in args if k not in keep)
    args = {k: v for k, v in args.items() if k in keep}
    if name == "louise_search":
        if collections and not args.get("collections"):
            args["collections"] = list(collections)
        args["translate"] = True
    return args, dropped


#: [n], [n, m], [n; m], [n-m]; and a uid or canonical id, [pubmed:37000021]
CITE_N = re.compile(r"\[(\d+(?:\s*(?:[,;]|[-–])\s*\d+)*)\]")
CITE_ID = re.compile(r"\[([A-Za-z][\w.-]*:[^\]\s,;]+)\]")


class Sources:
    """The answer's [n] (the module's point 3): every record a search showed
    the model, numbered in the order first shown, the same n whenever it
    comes back; and the reference facts shown, cited by their ids."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._n: dict[str, int] = {}
        self.ids: set[str] = set()

    def number(self, hit: dict[str, Any]) -> tuple[int, bool]:
        """(n, new) for a hit."""
        key = str(hit.get("uid") or hit.get("chunk_id") or hit.get("title") or "")
        if key in self._n:
            return self._n[key], False
        n = len(self.entries) + 1
        self._n[key] = n
        self.entries.append({k: hit.get(k) for k in ("uid", "title", "authors", "lang", "collection_id",
                                                    "snapshot_id", "chunk_id", "origin_url")}
                            | {"n": n})
        return n, True

    def cited(self, answer: str) -> tuple[list[dict[str, Any]], list[str]]:
        """(what the answer cites, in order — `{n, uid, title}` for a source,
        `{id}` for a fact; the citations that name nothing shown)."""
        got: list[dict[str, Any]] = []
        unknown: list[str] = []
        seen: set[Any] = set()

        def source(n: int) -> None:
            if n in seen:
                return
            seen.add(n)
            if 1 <= n <= len(self.entries):
                e = self.entries[n - 1]
                got.append({"n": n, "uid": e["uid"], "title": e["title"]})
            else:
                unknown.append(f"[{n}]")
        for m in re.finditer(f"{CITE_N.pattern}|{CITE_ID.pattern}", answer or ""):
            if m.group(1):
                for part in re.split(r"\s*[,;]\s*", m.group(1)):
                    bounds = [int(x) for x in re.split(r"\s*[-–]\s*", part)]
                    span = (range(bounds[0], bounds[-1] + 1) if len(bounds) == 2
                            and 0 < bounds[-1] - bounds[0] <= 20 else bounds)
                    for n in span:
                        source(n)
            else:
                cid = m.group(2)
                if cid in self._n:
                    source(self._n[cid])
                elif cid in self.ids:
                    if cid not in seen:
                        seen.add(cid)
                        got.append({"id": cid})
                elif cid not in seen:
                    seen.add(cid)
                    unknown.append(f"[{cid}]")
        return got, unknown


def shown_search(query: str, result: dict[str, Any], sources: Sources, *,
                 translated: bool = True) -> str:
    """A search's answer as the model reads it: each hit as its source [n]
    (a record shown before, by its number alone), the reference facts by
    their ids, and what was withheld."""
    said = f"Search: «{query}»" + (" and its translations" if translated else "")
    blocks = [said]
    for lst in result.get("lists", []):
        for h in lst.get("hits", [])[:HITS_PER_LIST]:
            n, fresh = sources.number(h)
            title = h.get("title") or h.get("uid") or "?"
            if not fresh:
                blocks.append(f"[{n}] {title} — shown above")
                continue
            by = f" — {h['authors']}" if h.get("authors") else ""
            head = f"[{n}] {title}{by} ({h.get('lang') or '?'}) · {h.get('uid')}"
            if h.get("trust_level") == "unverified":
                head += " · unverified"
            editions = [f"{e.get('title')} ({e.get('lang')})" for e in (h.get("editions") or [])[:3]]
            if editions:
                head += " · other editions held: " + "; ".join(editions)
            if "text" in h:
                body = _trim(h.get("text")) or ""
            else:
                body = f"(the text is not shown here; the origin: {h.get('origin_url')})"
            blocks.append(head + "\n" + str(body))
    for f in result.get("facts", []) or []:
        fid = str(f.get("entity_id") or "")
        sources.ids.add(fid)
        fields = "; ".join(f"{k}: {v}" for k, v in list((f.get("fields") or {}).items())[:12])
        blocks.append(f"[{fid}] {f.get('label') or ''} (reference source {f.get('source_id')})\n{fields}")
    if len(blocks) == 1:
        blocks.append("No results.")
    for w in result.get("withheld", []) or []:
        blocks.append(f"Withheld here: {w.get('collection') or w.get('collection_id')} ({w.get('reason')})")
    for w in result.get("legs_withheld", []) or []:
        blocks.append(f"Searched on its text only: {w.get('collection') or w.get('collection_id')} "
                      f"({w.get('reason')})")
    if result.get("unresolved"):
        blocks.append("No reference row for: " + ", ".join(map(str, result["unresolved"])))
    return "\n\n".join(blocks)


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
                hit = {k: h.get(k) for k in ("uid", "snapshot_id", "rank", "title", "authors", "lang",
                                             "trust_level", "verified_on_use", "legs") if k in h}
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
SLIMMED_TEXT = "(slimmed: the texts dropped to fit the context)"


def request_tokens(messages: list[dict[str, Any]], tools: Any) -> int:
    """The request's size, estimated from its characters."""
    chars = len(json.dumps(messages, ensure_ascii=False, default=str))
    chars += len(json.dumps(tools, ensure_ascii=False)) if tools else 0
    return int(chars / CHARS_PER_TOKEN)


def slim(content: str) -> str:
    """A tool answer reduced to what can still be cited: a search's hits keep
    their uid, title, rank and language and lose their text; anything else
    is cut to SLIM_CHARS. The model is told it was slimmed. A search as the
    model reads it (`shown_search`) keeps its line and each source's head."""
    if content.startswith("Search: «"):
        heads = [line for line in content.split("\n") if line.startswith(("Search: «", "["))]
        return "\n".join(heads + [SLIMMED_TEXT])
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("lists"), list):
        lists = [{"space_id": lst.get("space_id"),
                  "hits": [{k: h[k] for k in ("uid", "title", "authors", "rank", "lang") if k in h}
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
        content = str(m.get("content", ""))
        if m.get("role") != "tool" or '"slimmed"' in content or SLIMMED_TEXT in content:
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


#: A TOOL CALL WRITTEN AS TEXT (the blind read of 2026-11-03: one model's
#: answer was a raw `<tool_call>{...}</tool_call>` in 17 of 20 questions —
#: past the round bound the tools are withheld, and a model that still wants
#: one writes it into its answer). Qwen's, Hermes' and Mistral's forms; an
#: unclosed one runs to the end.
_TOOL_TEXT = re.compile(r"<tool_call>.*?(?:</tool_call>|\Z)|<\|tool_call\|>.*?(?:<\|/tool_call\|>|\Z)"
                        r"|\[TOOL_CALLS\].*\Z|<function=.*?(?:</function>|\Z)", re.DOTALL)
#: what the model is told when it answered past the bound with a tool call only
ANSWER_NOW = ("No more searches are possible. Answer the question now, from the sources above only, "
              "citing them by number; if they do not answer it, say so.")


def without_tool_text(content: str) -> str:
    """An answer with any tool call written as text taken out."""
    return _TOOL_TEXT.sub("", content or "").strip()


#: where Ask's first user turn ends its sources (ask(): "Sources: … Question: …")
_QUESTION = "\n\nQuestion: "


def without_tool_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation as a template WITHOUT tools can render it.

    2026-11-06, Command R7B's pilots: 8 answers of 45 failed with LocalAI's
    500 "Jinja Exception: Conversation roles must alternate
    user/assistant/user/assistant/...". Ask withholds the tools by declaring
    none, and with no tools llama.cpp renders a model's DEFAULT template.
    Cohere's default template takes user and assistant turns only, strictly
    alternating, and raises on anything else; Qwen's renders tool turns
    either way, so the Qwens never showed it. The request without tools
    therefore carries no tool turn. Each tool answer, whose content is a
    search block like the first request's sources, joins the sources of the
    user turn that asked; the calls that asked for them are dropped. The
    roles then alternate as every template expects. A copy: the
    conversation with its tool turns stays as it was."""
    out: list[dict[str, Any]] = []
    gathered: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            gathered.append(str(m.get("content") or ""))
        elif role == "assistant" and m.get("tool_calls"):
            continue
        else:
            out.append(dict(m))
    if gathered:
        last = max(i for i, m in enumerate(out) if m.get("role") == "user")
        head, sep, question = str(out[last]["content"]).rpartition(_QUESTION)
        joined = "\n\n---\n\n".join(gathered)
        out[last]["content"] = (f"{head}\n\n---\n\n{joined}{sep}{question}" if sep
                                else f"{out[last]['content']}\n\n---\n\n{joined}")
    return out


def _searched(call: Callable[[str, dict[str, Any]], Any], args: dict[str, Any], by: str
              ) -> dict[str, Any]:
    """One of Louise's own searches, as a turn."""
    t0 = time.monotonic()
    try:
        result, error = call("louise_search", args), None
    except CodexError as e:
        result, error = None, str(e)
    return {"tool": "louise_search", "arguments": args, "by": by, "result": result, "error": error,
            "seconds": round(time.monotonic() - t0, 3)}


def ask(question: str, *, collections: list[str] | None = None, history: list[dict[str, Any]] | None = None,
        call: Callable[[str, dict[str, Any]], Any] | None = None, model=None,
        max_rounds: int = MAX_ROUNDS, answer_lang: str | None = None) -> dict[str, Any]:
    """One question through the loop: `{answer, sources, cited, cited_unknown,
    turns: [{tool, arguments, by, result, error, seconds}], rounds,
    exhausted, model_s}`. `model(messages, tools) -> (content,
    calls, assistant_message)` is llm.chat_tools unless a test gives another;
    `call(name, args)` is the MCP dispatcher.

    First Louise's search (the module's point 1): the question with its
    translations. Then the model's rounds, the sources with the question,
    the tools offered. `model_s` is each model round's wall time and
    `seconds` each search's, so where an answer's time went is read off
    the answer itself (tools/measure/chat_eval.py; operator, 2026-11-03:
    "Use the chat comparison benchmark to work on the optimizations")."""
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
    past = [{"role": m["role"], "content": m["content"]} for m in history or []
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    scope = {"collections": list(collections)} if collections else {}

    # Louise's search: the question as the Ask tab searches it
    turns: list[dict[str, Any]] = [
        _searched(call, {"query": question, "translate": True, **scope}, "louise")]

    sources = Sources()
    blocks = []
    for t in turns:
        q, translated = t["arguments"]["query"], bool(t["arguments"].get("translate"))
        if t["error"]:
            blocks.append(f"Search: «{q}» refused: {t['error']}")
        else:
            blocks.append(shown_search(q, t["result"] or {}, sources, translated=translated))
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *past,
                                      {"role": "user", "content": "Sources:\n\n" + "\n\n---\n\n".join(blocks)
                                       + f"\n\nQuestion: {question}"}]
    tools = tool_specs()
    model_s: list[float] = []
    slimmed = 0
    tools_failed: str | None = None

    def timed_model(tools_offered, asked: list[dict[str, Any]] | None = None):
        nonlocal slimmed
        asked = messages if asked is None else asked
        slimmed += fit(asked, tools_offered)
        t0 = time.monotonic()
        try:
            return model(asked, tools_offered)
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

    def done(content: str, rounds: int, exhausted: bool) -> dict[str, Any]:
        cited, unknown = sources.cited(content)
        out = {"answer": content, "sources": sources.entries, "cited": cited, "cited_unknown": unknown,
               "turns": turns, "rounds": rounds, "exhausted": exhausted,
               "model_s": model_s, "answer_lang": lang, "slimmed": slimmed}
        if tools_failed:
            out["tools_failed"] = tools_failed
        return out

    def without_tools() -> str:
        """The model's answer with the tools withheld, asked on the
        conversation as a template without tools renders it
        (`without_tool_turns`). One that is only a tool call written as
        text is asked once more, told to answer now, after its own turn so
        the roles still alternate; whatever text remains is the answer,
        never the call."""
        asked = without_tool_turns(messages)
        content, _, _ = timed_model(None, asked)
        answer = without_tool_text(content)
        if answer or not content.strip():
            return answer
        asked += [{"role": "assistant", "content": content}, {"role": "user", "content": ANSWER_NOW}]
        content, _, _ = timed_model(None, asked)
        return without_tool_text(content)

    def answer_with_what_it_has(rounds: int) -> dict[str, Any]:
        """A request the server refused as too large: every tool answer
        slimmed, tools withheld, one more ask; never the whole answer lost
        because the searches returned a lot."""
        nonlocal slimmed
        slimmed += fit(messages, None, everything=True)
        return done(without_tools(), rounds, True)
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        try:
            content, calls, assistant = timed_model(tools)
        except httpx.HTTPStatusError as e:
            if too_large(e):
                return answer_with_what_it_has(rounds)
            if e.response.status_code < 500:
                raise
            # A TOOL CALL THE ENGINE CANNOT PARSE (2026-11-03, München: Command
            # R7B on llama.cpp ddf48f8 — no handler for its template, so the
            # generic parser threw on "I will search for …[" and LocalAI
            # answered 500 the moment the model chose a tool; upstream #20708,
            # #20786). The round is answered without tools, from the sources
            # Louise already searched; a server that fails that too is a
            # fault, and its error is raised from there.
            try:
                body = e.response.text
            except Exception:   # noqa: BLE001 — an unreadable body is said as unreadable
                body = "(unreadable)"
            tools_failed = f"{e.response.status_code}: {body[:200]}"
            try:
                return done(without_tools(), rounds, True)
            except httpx.HTTPStatusError as again:
                # both requests refused (2026-11-06, Command R7B's pilot: two
                # 500s, no reason left in any log): the first one's body goes
                # with the raised error, a PEP 678 note, so the row says both
                again.add_note(f"the request with tools failed first: {tools_failed}")
                raise
        if not calls:
            return done(without_tool_text(content), rounds, False)
        messages.append(assistant)
        for c in calls:
            asked = c.arguments if isinstance(c.arguments, dict) else {}
            args, dropped = model_args(c.name, asked, collections)
            t0 = time.monotonic()
            try:
                result = call(c.name, args)
                if c.name == "louise_search":
                    shown: Any = shown_search(str(args.get("query", "")), result or {}, sources)
                else:
                    shown = json.dumps(compact(c.name, result), ensure_ascii=False, default=str)
                error = None
            except CodexError as e:
                result, shown, error = None, json.dumps({"error": str(e)}, ensure_ascii=False), str(e)
            except KeyError:
                error = f"no tool named {c.name!r}"
                result, shown = None, json.dumps({"error": error}, ensure_ascii=False)
            turn = {"tool": c.name, "arguments": args, "by": "model", "result": result, "error": error,
                    "seconds": round(time.monotonic() - t0, 3)}
            if dropped:
                turn["dropped"] = dropped
            turns.append(turn)
            messages.append(llm.tool_message(c, shown))
    # the bound reached: the model answers with what it has, tools withheld
    try:
        content = without_tools()
    except httpx.HTTPStatusError as e:
        if not too_large(e):
            raise
        return answer_with_what_it_has(rounds)
    return done(content, rounds, True)
