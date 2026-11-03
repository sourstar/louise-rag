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

1. REWRITE-RETRIEVE-READ (Ma et al., EMNLP 2023, "Query Rewriting for
   Retrieval-Augmented Large Language Models"; LangChain's
   MultiQueryRetriever is the same step). One short call, no tools: the
   searches the question implies — the title and author of a work it
   describes, a name, key terms. The model's knowledge goes into the
   SEARCH, never into the answer (`unsupported`, chat_eval, checks that).
2. LOUISE SEARCHES FIRST, as the Ask tab does: the question as typed with
   its translations (web._ask_result), while the model writes the
   rewrites; then each rewrite as written (a title is searched in its own
   language), side by side.
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

import concurrent.futures
import contextvars
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
    "searched the reader's question, and the works and names it implies: the numbered sources come with "
    "the question. Answer from the sources, and cite each claim with its source's number in square "
    "brackets, e.g. [2] or [2][5]; a fact from a reference source is cited by its id, e.g. [hgnc:1101]. "
    "When the sources do not answer, search again with `louise_search` — the work's title and author, "
    "other words, another language — and cite what it finds by its number. `louise_lookup` gives a fact "
    "by its canonical id (<namespace>:<id>); `louise_traverse` follows the declared relations from a "
    "record's uid. Use only what the sources say: never add a name, a date or a number they do not give, "
    "never cite a number you were not shown, and never invent a record. Say plainly when nothing "
    "answers, when a collection was withheld, and when the evidence is unverified."
)
#: the rewrite step's own instruction (the module's point 1); `{k}` searches at most
REWRITE_SYSTEM = (
    "You write searches for a library that holds books, articles and records in many languages. Given a "
    "reader's question, write at most {k} short searches that would find what it asks for, one per line "
    "and nothing else: when it describes a work, the work's title and its author's name as published, in "
    "the work's own language, and its English title; otherwise the names and key terms. Write only the "
    "searches: no numbering, no quotes, no answer, no explanation."
)
#: searches the rewrite step may add, and the new sources each may bring
REWRITES = 3
REWRITE_HITS = 5


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
        self.entries.append({k: hit.get(k) for k in ("uid", "title", "lang", "collection_id",
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
                 new_at_most: int | None = None, translated: bool = True) -> str:
    """A search's answer as the model reads it: each hit as its source [n]
    (a record shown before, by its number alone), the reference facts by
    their ids, and what was withheld. `new_at_most` keeps a search from
    adding more than that many new sources."""
    said = f"Search: «{query}»" + (" and its translations" if translated else "")
    blocks = [said]
    new = 0
    for lst in result.get("lists", []):
        for h in lst.get("hits", [])[:HITS_PER_LIST]:
            key = str(h.get("uid") or h.get("chunk_id") or h.get("title") or "")
            if new_at_most is not None and key not in sources._n and new >= new_at_most:
                continue
            n, fresh = sources.number(h)
            title = h.get("title") or h.get("uid") or "?"
            if not fresh:
                blocks.append(f"[{n}] {title} — shown above")
                continue
            new += 1
            head = f"[{n}] {title} ({h.get('lang') or '?'}) · {h.get('uid')}"
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


def rewrites_of(content: str, question: str, k: int = REWRITES) -> list[str]:
    """The searches a rewrite answer names, one per line, at most `k`: a
    list's bullets and numbers, quotes and headings taken off; the question
    itself and repeats left out."""
    out: list[str] = []
    for line in (content or "").splitlines():
        s = re.sub(r"^\s*(?:[-*•]+|\d+[.):])\s*", "", line).strip().strip("\"'“”«»`").strip()
        if not s or s.endswith(":") or len(s) > 160:
            continue
        if s.casefold() == question.strip().casefold() or s.casefold() in {o.casefold() for o in out}:
            continue
        out.append(s)
        if len(out) == k:
            break
    return out


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


def _submit(pool: concurrent.futures.ThreadPoolExecutor, fn, *args) -> concurrent.futures.Future:
    """`fn(*args)` on a pool thread in a copy of THIS thread's context, taken
    here before the hand-over, so the reader's cancel token (app/cancel.py)
    and the rerank's reasons (retrieval.py) go with the search."""
    return pool.submit(contextvars.copy_context().run, fn, *args)


def ask(question: str, *, collections: list[str] | None = None, history: list[dict[str, Any]] | None = None,
        call: Callable[[str, dict[str, Any]], Any] | None = None, model=None,
        max_rounds: int = MAX_ROUNDS, answer_lang: str | None = None) -> dict[str, Any]:
    """One question through the loop: `{answer, sources, cited, cited_unknown,
    rewrites, turns: [{tool, arguments, by, result, error, seconds}], rounds,
    exhausted, model_s, rewrite_s}`. `model(messages, tools) -> (content,
    calls, assistant_message)` is llm.chat_tools unless a test gives another;
    `call(name, args)` is the MCP dispatcher.

    First Louise's searches (the module's points 1-2): the question with its
    translations, beside the rewrite call, then the rewrites. Then the
    model's rounds, the sources with the question, the tools offered.
    `model_s` is each model round's wall time, `rewrite_s` the rewrite's,
    and `seconds` each search's, so where an answer's time went is read off
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

    # Louise's searches: the question as the Ask tab searches it, beside the
    # rewrite call; then the rewrites, side by side
    turns: list[dict[str, Any]] = []
    rewrites: list[str] = []
    rewrite_s: float | None = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=REWRITES + 1,
                                               thread_name_prefix="ask-search") as pool:
        typed = _submit(pool, _searched, call, {"query": question, "translate": True, **scope}, "louise")
        t0 = time.monotonic()
        try:
            content, _, _ = model([{"role": "system", "content": REWRITE_SYSTEM.format(k=REWRITES)},
                                   *past[-4:], {"role": "user", "content": question}], None)
            rewrites = rewrites_of(content, question)
        except (httpx.HTTPError, ValueError):
            rewrites = []     # no rewrite: the question as typed is searched all the same
        rewrite_s = round(time.monotonic() - t0, 3)
        more = [_submit(pool, _searched, call, {"query": r, "translate": False, **scope}, "louise:rewrite")
                for r in rewrites]
        turns = [typed.result()] + [f.result() for f in more]

    sources = Sources()
    blocks = []
    for t in turns:
        q, translated = t["arguments"]["query"], bool(t["arguments"].get("translate"))
        if t["error"]:
            blocks.append(f"Search: «{q}» refused: {t['error']}")
        else:
            blocks.append(shown_search(q, t["result"] or {}, sources,
                                       new_at_most=None if t["by"] == "louise" else REWRITE_HITS,
                                       translated=translated))
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *past,
                                      {"role": "user", "content": "Sources:\n\n" + "\n\n---\n\n".join(blocks)
                                       + f"\n\nQuestion: {question}"}]
    tools = tool_specs()
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

    def done(content: str, rounds: int, exhausted: bool) -> dict[str, Any]:
        cited, unknown = sources.cited(content)
        return {"answer": content, "sources": sources.entries, "cited": cited, "cited_unknown": unknown,
                "rewrites": rewrites, "turns": turns, "rounds": rounds, "exhausted": exhausted,
                "model_s": model_s, "rewrite_s": rewrite_s, "answer_lang": lang, "slimmed": slimmed}

    def answer_with_what_it_has(rounds: int) -> dict[str, Any]:
        """A request the server refused as too large: every tool answer
        slimmed, tools withheld, one more ask; never the whole answer lost
        because the searches returned a lot."""
        nonlocal slimmed
        slimmed += fit(messages, None, everything=True)
        content, _, _ = timed_model(None)
        return done(content, rounds, True)
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
            return done(content, rounds, False)
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
        content, _, _ = timed_model(None)
    except httpx.HTTPStatusError as e:
        if not too_large(e):
            raise
        return answer_with_what_it_has(rounds)
    return done(content, rounds, True)
