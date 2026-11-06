"""Is a chat model wired as its own template says? (operator, 2026-11-06:
"Are you sure that the model input/output are correctly wired?")

    GGUF_PY=<llama.cpp source>/gguf-py python -m tools.measure.chat_eval wire --out DIR \\
        --model louise-chat-command-r7b-q8 --gguf <the model file's .gguf> \\
        --search-cache FILE --collections gutenberg --keys WQ506865:fr,WQ6950087:fr

WHAT THE SAMPLING PROBE DOES NOT SEE. sampling_probe checks every sampling
value by its effect. It does not check what the model is GIVEN, the prompt
the engine builds from Ask's messages and tools, nor what Ask is GIVEN BACK,
the message parsed out of the model's raw text. Both are the engine's:
llama.cpp renders the template the GGUF carries (`tokenizer.chat_template`,
or its `tool_use` variant when tools are declared) with its own Jinja
(minja), and parses the output with a per-format handler (Command R7B's is
our patch P6, docs/notes/localai-bugs.md).

THE CHECK, by effect, on Ask's real first request for a recorded question:
1. REFERENCE: the GGUF's own template rendered with Python's Jinja as
   Hugging Face transformers renders chat templates (the standard reference:
   transformers/utils/chat_template_utils.py — ImmutableSandboxedEnvironment,
   trim_blocks, lstrip_blocks, loopcontrols, `tojson` without ASCII
   escaping, `raise_exception`, `strftime_now`), with the messages and tools
   Ask sends, `add_generation_prompt`, and the request's
   `chat_template_kwargs`; the BOS text dropped where the tokenizer adds one
   and the EOS where it appends one (llama.cpp's common/chat.cpp does both).
2. CHAT: Ask's request itself (llm._request, Ask's tools), at greedy; a
   one-token one first leaves the engine's prompt in the slot's cache.
3. RAW: a reference sent to /v1/completions (no template: LocalAI hands a
   completion's prompt to the backend as it is), at greedy, twice. The
   reference is the candidate the engine's cache says is its prompt TOKEN
   FOR TOKEN: the tools as Ask sends them, as LocalAI's Go Tool struct
   writes them (field order, "strict": false, parameters' keys sorted), or
   with every key sorted; each with and without the BOS text.
Wired when:
- INPUT: a candidate has the engine's prompt count AND, sent after the
  one-token chat request, evaluates only its last token (llama.cpp's
  print_timing, in the log LocalAI keeps per model): its tokens are the
  engine's. Counts alone proved nothing (2026-11-06: a reference with the
  engine's count parted from its prompt at token 330). With no log line,
  only the count is said, and identity is undecidable;
- OUTPUT: the chat answer's text, reasoning and tool calls are inside a raw
  run's text (a greedy answer can part from itself on a near-tie on this
  engine, so the agreeing run is the measure, and a third run is asked when
  neither agrees); a tool named as a JSON string in that raw text is among
  the parsed calls; no control marker is left in the answer.
Whether greedy repeats itself is reported, not decided on: it is not wiring.
A failed check says what differs and keeps the texts; it never guesses why.
`--append TEXT` adds TEXT to the reference prompt, to test what the engine
adds (llama.cpp's Command R7B handler appends a closed thinking block when
thinking is off).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

Post = Callable[[str, dict[str, Any]], tuple[int, Any]]
Get = Callable[[str], tuple[int, Any]]

#: llama.cpp's print_timing: the prompt tokens a request had to evaluate
#: (the rest came from the slot's cache)
_PROMPT_EVAL = re.compile(r"prompt eval time\s*=\s*[\d.]+ ms /\s*(\d+) tokens")


def http_get(url: str) -> tuple[int, Any]:
    import httpx
    try:
        r = httpx.get(url, timeout=20)
    except httpx.HTTPError as e:
        return 0, str(e)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def _log_lines(get: Get, chat_url: str, model: str) -> list[tuple[str, str]]:
    """The backend's log ring, as LocalAI keeps it per model
    (/api/backend-logs/<model>): (timestamp, text) pairs; empty when none."""
    status, got = get(f"{chat_url}/api/backend-logs/{model}")
    if status != 200 or not isinstance(got, list):
        return []
    return [(str(e.get("timestamp", "")), str(e.get("text", ""))) for e in got if isinstance(e, dict)]

#: what a parsed answer must never carry: a control marker of the template
#: (Cohere's <|START_RESPONSE|>, ChatML's <|im_end|>), a tool-call or thinking
#: tag, a Cohere citation span
MARKERS = re.compile(r"<\|[A-Za-z_]+\|>|</?tool_call>|</?think>|</?co[:>]")


@dataclass
class Templates:
    """What the GGUF carries for a chat: its templates and special tokens."""
    default: str
    tool_use: str | None
    bos: str
    eos: str
    add_bos: bool
    add_eos: bool
    source: str = ""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: False for what is reported but is not wiring (greedy repeating itself)
    decides: bool = True


def read_gguf(path: Path) -> Templates:
    """The chat templates and special tokens of a GGUF, read with llama.cpp's
    own gguf-py (GGUF_PY: the gguf-py directory of the llama.cpp source the
    engine is built from, as deploy/vega/stage/derive_gguf_context.sh), the
    way its gguf_dump.py reads fields: a value is `parts[data[i]]`."""
    where = os.environ.get("GGUF_PY")
    if where and where not in sys.path:
        sys.path.insert(0, where)
    try:
        from gguf import GGUFReader
    except ImportError:
        raise SystemExit("wire: gguf-py not importable; set GGUF_PY to the gguf-py directory of "
                         "the llama.cpp source the engine is built from") from None
    reader = GGUFReader(str(path))

    def field(name: str):
        return reader.get_field(name)

    def text(name: str, index: int = 0) -> str | None:
        f = field(name)
        return None if f is None else bytes(f.parts[f.data[index]]).decode("utf-8")

    def scalar(name: str):
        f = field(name)
        return None if f is None else f.parts[f.data[0]][0].item()

    default = text("tokenizer.chat_template")
    if default is None:
        raise SystemExit(f"wire: {path} carries no tokenizer.chat_template")
    bos_id, eos_id = scalar("tokenizer.ggml.bos_token_id"), scalar("tokenizer.ggml.eos_token_id")
    add_bos, add_eos = scalar("tokenizer.ggml.add_bos_token"), scalar("tokenizer.ggml.add_eos_token")
    return Templates(default=default, tool_use=text("tokenizer.chat_template.tool_use"),
                     bos=text("tokenizer.ggml.tokens", bos_id) if bos_id is not None else "",
                     eos=text("tokenizer.ggml.tokens", eos_id) if eos_id is not None else "",
                     add_bos=bool(add_bos), add_eos=bool(add_eos), source=str(path))


def render(t: Templates, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
           kwargs: dict[str, Any]) -> tuple[str, str]:
    """(the reference prompt, which template rendered it)."""
    from datetime import datetime

    from jinja2.ext import loopcontrols
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    def raise_exception(message: str):
        raise ValueError(f"the template refused: {message}")

    def tojson(x, ensure_ascii=False, indent=None, separators=None, sort_keys=False):
        return json.dumps(x, ensure_ascii=ensure_ascii, indent=indent, separators=separators,
                          sort_keys=sort_keys)
    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True, extensions=[loopcontrols])
    env.filters["tojson"] = tojson
    env.globals["raise_exception"] = raise_exception
    # transformers' strftime_now: the local time, as the template's date line
    env.globals["strftime_now"] = lambda fmt: datetime.now().astimezone().strftime(fmt)
    which = "tool_use" if tools and t.tool_use else "default"
    source = t.tool_use if which == "tool_use" else t.default
    text = env.from_string(source).render(messages=messages, tools=tools or None, bos_token=t.bos,
                                          eos_token=t.eos, add_generation_prompt=True, **kwargs)
    if t.add_bos and t.bos and text.startswith(t.bos):
        text = text[len(t.bos):]
    if t.add_eos and t.eos and text.endswith(t.eos):
        text = text[:-len(t.eos)]
    return text, which


def first_request(question: str, *, collections: list[str] | None, search_cache, ask=None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """The messages and tools of Ask's first model request for `question`,
    as Ask builds them (its search replayed from the cache)."""
    import copy

    from app.ask import chat
    ask = ask or chat.ask
    seen: dict[str, Any] = {}

    def captured(messages, tools):
        seen.update(messages=copy.deepcopy(messages), tools=copy.deepcopy(tools))
        return "(captured)", [], {"role": "assistant", "content": "(captured)"}
    ask(question, collections=collections, call=search_cache, model=captured)
    return seen["messages"], seen["tools"]


def _norm(s: str) -> str:
    return " ".join(s.split())


def _strings(x: Any) -> list[str]:
    """The string values inside a tool call's arguments."""
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for v in x.values() for s in _strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return []


def check(model: str, question: dict[str, str], t: Templates, *, collections: list[str] | None,
          search_cache, post: Post, chat_url: str, max_tokens: int = 1500, append: str = "",
          ask=None, sampling: dict[str, Any] | None = None, get: Get | None = None) -> dict[str, Any]:
    """Every check on one question; a refused request fails the check with
    the server's answer. `sampling`: values both paths send besides
    temperature 0, to find which setting the two endpoints apply otherwise
    (the node, 2026-11-06: with the same prompt and the same cached slot the
    Qwens' chat answers still parted from the raw runs)."""
    greedy = {"temperature": 0, **(sampling or {})}
    from app import config, llm
    config.CHAT_MODEL = model
    messages, tools = first_request(question["query"], collections=collections,
                                    search_cache=search_cache, ask=ask)
    url, body = llm._request(messages, stream=False, temperature=None, max_tokens=max_tokens,
                             num_ctx=16384, sampling=greedy)
    if tools:
        body["tools"] = tools
    kwargs = dict(body.get("chat_template_kwargs") or {})
    reference, which = render(t, messages, tools, kwargs)
    reference += append
    checks: list[Check] = []
    got: dict[str, Any] = {}
    ids: dict[str, list[int]] = {}
    get = get or http_get

    def asked(label: str, u: str, b: dict[str, Any], *, needed: bool = True) -> Any | None:
        status, answer = post(u, b)
        got[label] = {"status": status, "answer": answer}
        if status != 200:
            if needed:
                checks.append(Check("every request answered", False, f"{label}: {status}: {str(answer)[:300]}"))
            return None
        return answer

    def count(label: str, text: str) -> int | None:
        """Tokens of `text` by the model's own tokenizer (LocalAI /v1/tokenize);
        an empty text may be refused, and then nothing is assumed added."""
        answer = asked(label, f"{chat_url}/v1/tokenize", {"model": model, "content": text}, needed=bool(text))
        tokens = (answer or {}).get("tokens") if isinstance(answer, dict) else None
        if isinstance(tokens, list):
            got[label]["answer"] = {"tokens": len(tokens)}       # the count, not the ids
            ids[label] = tokens
            return len(tokens)
        return None

    def evaluated(label: str, u: str, b: dict[str, Any]) -> Any | None:
        """`asked`, and the prompt tokens the engine evaluated for it, read
        from the lines its log gained meanwhile (llama.cpp's print_timing)."""
        before = set(_log_lines(get, chat_url, model))
        answer = asked(label, u, b)
        new = [text for line in _log_lines(get, chat_url, model) if line not in before
               for text in [line[1]] if _PROMPT_EVAL.search(text)]
        if answer is not None:
            got[label]["prompt_evaluated"] = int(_PROMPT_EVAL.search(new[-1]).group(1)) if new else None
        return answer

    # the engine's prompt count, from a one-token chat request; it also
    # leaves the engine's prompt in the slot's cache
    if evaluated("chat_count", url, {**body, "max_tokens": 1}) is None:
        return _result(model, question, which, t, reference, append, got, checks, greedy)
    p_chat = (got["chat_count"]["answer"].get("usage") or {}).get("prompt_tokens")

    # INPUT, TOKEN FOR TOKEN (the node, 2026-11-06). LocalAI's /v1/completions
    # reports usage 0 on this backend, so the counts come from the model's own
    # tokenizer; and equal counts proved nothing: the Qwen3-8B's "keys sorted"
    # reference had the engine's count and parted from its prompt at token 330,
    # the first key of the first tool. What decides is the cache: after the
    # one-token chat request the slot holds the engine's prompt, and a raw
    # prompt token for token the same evaluates only its last token
    # (llama.cpp's print_timing, in the log LocalAI keeps per model).
    # THE CANDIDATES, each a way the engine may have been given the tools:
    # - as Ask sends them;
    # - as LocalAI's Go Tool struct re-encodes them (the engine's own log,
    #   2026-11-03: {"type":"function","function":{"name":…,"description":…,
    #   "strict":false,"parameters":{…}}}: the struct's field order, an added
    #   "strict": false, the parameters a Go map, so its keys sorted);
    # - every key sorted;
    # each with and without the BOS text (Qwen3.5-9B parted at token 0).
    n_empty = count("tokenize_empty", "")
    empty_refused = n_empty is None
    n_empty = 0 if empty_refused else n_empty
    added = int(t.add_bos) + int(t.add_eos) - n_empty
    forms: list[tuple[str, Any]] = [("as Ask sends them", tools)]
    if tools:
        # 2026-11-06, wire16: Qwen3-8B's engine prompt is Ask's text up to the
        # parameters object, whose keys come sorted (a Go map), no "strict"
        forms += [("as Ask sends them, the parameters' keys sorted", parameters_sorted(tools)),
                  ("as LocalAI's Tool struct writes them", localai_tools(tools)),
                  ("with every key sorted", go_order(tools))]
    candidates: list[tuple[str, str]] = []
    bos_kept: list[bool] = []
    for name, form in forms:
        text = render(t, messages, form, kwargs)[0] + append
        candidates.append((f"tools {name}" if tools else "no tools", text))
        bos_kept.append(False)
        if t.bos and not text.startswith(t.bos):
            # the raw path adding no BOS of its own while the chat path has one
            candidates.append((f"tools {name}, the BOS text kept" if tools else "the BOS text kept", t.bos + text))
            bos_kept.append(True)
    counts = [count(f"tokenize_candidate_{i}", text) for i, (_, text) in enumerate(candidates)]
    tried: list[dict[str, Any]] = [
        {"candidate": name, "count": None if n is None else n + (added - int(t.add_bos) if kept else added)}
        for (name, _), n, kept in zip(candidates, counts, bos_kept, strict=True)]
    matched: tuple[str, str] | None = None
    unlogged = False
    for i, (name, text) in enumerate(candidates):
        if i and evaluated(f"prime_{i}", url, {**body, "max_tokens": 1}) is None:
            break
        if evaluated(f"candidate_{i}", f"{chat_url}/v1/completions",
                     {"model": model, "prompt": text, "max_tokens": 1, **greedy}) is None:
            break
        tried[i]["evaluated"] = got[f"candidate_{i}"].get("prompt_evaluated")
        if tried[i]["evaluated"] is None:
            unlogged = True                         # no log: token identity cannot be told
            break
        if tried[i]["count"] == p_chat and tried[i]["evaluated"] <= 1:
            matched = (name, text)
            break
    got["candidates"] = tried
    same_count = next((c for (c, _), r in zip(candidates, tried, strict=True) if r["count"] == p_chat), None)
    if matched:
        reference = matched[1]
        checks.append(Check("INPUT: the prompt is the template's", True,
                            f"token for token the engine's ({p_chat} tokens): {matched[0]}"))
    elif unlogged or no_reuse(tried):
        # NO PREFIX REUSED (2026-11-06, wire16: Qwen3.5-9B re-evaluated every
        # candidate whole, parting "at token 1"): a hybrid or recurrent model's
        # state cannot be cut back to a prefix, so llama.cpp evaluates each
        # prompt anew and the cache tells nothing. The count chooses, and the
        # OUTPUT checks below decide: each request a fresh prefill, greedy
        # answers from the same tokens are the same
        if same_count:
            reference = dict(candidates)[same_count]
        why = ("no prompt eval line in the backend's log" if unlogged else
               "the engine reuses no cached prefix (every candidate evaluated whole: a hybrid or "
               "recurrent model); the greedy answers below decide")
        checks.append(Check("INPUT: the prompt is the template's", bool(same_count),
                            f"{p_chat} prompt tokens through chat; "
                            + (f"the same count: {same_count}" if same_count else "no candidate has that count")
                            + f"; token identity undecidable: {why}"))
        if not same_count:
            parts = localize(count, n_empty, t, messages, tools, kwargs, post, got)
            checks[-1].detail += "; counts by part: " + ", ".join(
                f"{v['part']} {v['delta']:+d}" if isinstance(v.get("delta"), int) else f"{v['part']} ?"
                for v in parts)
            got["localized"] = parts
    else:
        probed = [r for r in tried if r.get("evaluated") is not None]
        # the tokens a candidate shares with the engine's prompt: its own
        # length less what it evaluated (llama.cpp re-evaluates from the first
        # token that differs)
        def shared(r: dict[str, Any]) -> int:
            return (r.get("count") or 0) - r["evaluated"]
        best = max(probed, key=shared, default=None)
        where = ""
        if best is not None:
            i = [r["candidate"] for r in tried].index(best["candidate"])
            part = shared(best)
            best_ids = ids.get(f"tokenize_candidate_{i}") or []
            if best_ids and 0 <= part < len(best_ids):
                s1, before = post(f"{chat_url}/v1/detokenize",
                                  {"model": model, "tokens": best_ids[max(0, part - 12):part]})
                s2, after = post(f"{chat_url}/v1/detokenize", {"model": model, "tokens": best_ids[part:part + 12]})
                if s1 == 200 and s2 == 200:
                    where = (f"; the closest, {best['candidate']}, parts at token {part}: "
                             f"…{(before or {}).get('content', '')!r} ‖ {(after or {}).get('content', '')!r}…")
            reference = candidates[i][1]
        checks.append(Check("INPUT: the prompt is the template's", False,
                            f"{p_chat} prompt tokens through chat; none token for token: " + "; ".join(
                                f"{r['candidate']}: {r['count']} tokens, {r.get('evaluated')} evaluated"
                                for r in probed) + where))
        parts = localize(count, n_empty, t, messages, tools, kwargs, post, got)
        checks[-1].detail += "; counts by part: " + ", ".join(
            f"{v['part']} {v['delta']:+d}" if isinstance(v.get("delta"), int) else f"{v['part']} ?"
            for v in parts)
        got["localized"] = parts
    if empty_refused:
        checks[-1].detail += " (an empty text refused by /v1/tokenize: nothing assumed added)"

    # raw, chat, raw on that reference, each FROM THE SAME CACHED PROMPT
    # (the node, 2026-11-06: with the chat request first, its prefill reused
    # another question's prefix and the answers parted on near-ties)
    raw_body = {"model": model, "prompt": reference, "max_tokens": max_tokens, **greedy}
    if evaluated("raw", f"{chat_url}/v1/completions", raw_body) is None \
            or evaluated("chat", url, body) is None \
            or evaluated("raw_again", f"{chat_url}/v1/completions", raw_body) is None:
        return _result(model, question, which, t, reference, append, got, checks, greedy)
    msg = got["chat"]["answer"]["choices"][0].get("message") or {}
    content = msg.get("content") or ""
    thought = msg.get("reasoning_content") or msg.get("reasoning") or ""
    calls = [c.get("function") or {} for c in msg.get("tool_calls") or [] if isinstance(c, dict)]

    # OUTPUT. A greedy answer does not always repeat itself on this engine
    # (qwen3-8b-q8, 2026-11-06: two raw runs parted after 596 characters, a
    # near-tie), so the chat answer is held to the raw run it agrees with,
    # and a third run is asked when it agrees with neither
    raws = [got["raw"]["answer"]["choices"][0].get("text") or "",
            got["raw_again"]["answer"]["choices"][0].get("text") or ""]

    def agrees(raw: str) -> bool:
        said = [content, thought] + [s for c in calls for s in [c.get("name") or ""] + _strings(_args(c.get("arguments")))]
        return all(not s or _norm(s) in _norm(raw) or _norm(json.dumps(s, ensure_ascii=False)[1:-1]) in _norm(raw)
                   for s in said)
    if not any(agrees(r) for r in raws) and raws[0] != raws[1]:
        if asked("raw_third", f"{chat_url}/v1/completions", raw_body) is None:
            return _result(model, question, which, t, reference, append, got, checks, greedy)
        raws.append(got["raw_third"]["answer"]["choices"][0].get("text") or "")
    checks.append(Check("greedy repeats itself (raw)", len(set(raws)) == 1,
                        "the same text" if len(set(raws)) == 1 else
                        f"{len(set(raws))} different texts in {len(raws)} runs (" +
                        ", ".join(str(len(r)) for r in raws) + " characters)", decides=False))
    agreeing = [i for i, r in enumerate(raws) if agrees(r)]
    if agreeing:
        checks.append(Check("OUTPUT: the answer is the model's own text", True,
                            f"content, reasoning and calls inside raw run {agreeing[0] + 1}"))
    else:
        common = max(len(os.path.commonprefix([_norm(content or thought), _norm(r)])) for r in raws)
        checks.append(Check("OUTPUT: the answer is the model's own text", False,
                            f"in no raw run: the closest shares its first {common} characters "
                            f"({(content or thought)[:120]!r})"))
    raw = raws[agreeing[0]] if agreeing else raws[0]
    named = sorted({(f.get("function") or {}).get("name") for f in tools or []} - {None})
    called = {c.get("name") for c in calls}
    lost_calls = [n for n in named if f'"{n}"' in raw and n not in called]
    checks.append(Check("OUTPUT: every tool call kept", bool(agreeing) and not lost_calls,
                        f"{len(calls)} call(s) parsed"
                        + (f"; in the raw text, not parsed: {lost_calls}" if lost_calls else "")
                        + ("" if agreeing else "; undecidable: no raw run agrees")))
    left = sorted(set(MARKERS.findall(content)))
    checks.append(Check("OUTPUT: no marker left in the answer", not left,
                        "none" if not left else ", ".join(left)))
    return _result(model, question, which, t, reference, append, got, checks, greedy)


def parameters_sorted(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools as Ask sends them, but each function's `parameters` with
    every key sorted: a struct whose own fields keep their order and whose
    parameters are a Go map (Qwen3-8B's engine prompt, wire16, 2026-11-06)."""
    out = []
    for tool in tools:
        f = tool.get("function")
        if isinstance(f, dict) and "parameters" in f:
            tool = {**tool, "function": {**f, "parameters": go_order(f["parameters"])}}
        out.append(tool)
    return out


def no_reuse(tried: list[dict[str, Any]]) -> bool:
    """Every candidate probed evaluated its whole prompt: the engine reused
    no cached prefix, even of the template's first tokens."""
    probed = [r for r in tried if isinstance(r.get("evaluated"), int) and isinstance(r.get("count"), int)]
    return bool(probed) and all(r["evaluated"] >= r["count"] - 1 for r in probed)


def localai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tools as LocalAI's Go structs encode them for the backend (its
    pkg/functions Tool and Function; the engine's own log, 2026-11-03):
    type, then function with name, description, strict (false unless set)
    and parameters, in that order; parameters a Go map, so with every key
    sorted."""
    out = []
    for tool in tools:
        f = tool.get("function") or {}
        out.append({"type": tool.get("type", "function"),
                    "function": {"name": f.get("name", ""), "description": f.get("description", ""),
                                 "strict": bool(f.get("strict", False)),
                                 "parameters": go_order(f.get("parameters"))}})
    return out


def go_order(x: Any) -> Any:
    """`x` with every object's keys in sorted order: how Go's encoding/json
    writes a map (LocalAI decodes a request's tools into maps and encodes them
    again for the backend)."""
    return json.loads(json.dumps(x, sort_keys=True, ensure_ascii=False))


#: a turn the model has no reason to answer at length: the cut-down conversations' user turn
HELLO = "Hello."


def localize(count: Callable[[str, str], int | None], n_empty: int, t: Templates,
             messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, kwargs: dict[str, Any],
             post: Post, got: dict[str, Any]) -> list[dict[str, Any]]:
    """The prompt difference, chat minus template, on cut-down conversations:
    a user turn alone (the generation prompt: a thinking block, a closing
    marker), with Ask's system turn (the system's handling), with Ask's
    tools (their rendering), and Ask's own turns without tools (their
    content). The part whose difference is not 0 is where the engine renders
    otherwise; each request is asked for one token."""
    from app import llm
    hello = [{"role": "user", "content": HELLO}]
    variants = [("a user turn", hello, None, None),
                ("+ system", [messages[0], *hello], None, None),
                ("+ tools", hello, tools, tools),
                ("+ tools, keys sorted", hello, tools, go_order(tools) if tools else None),
                ("Ask's turns, no tools", messages, None, None)]
    out: list[dict[str, Any]] = []
    for i, (part, msgs, sent, rendered) in enumerate(variants):
        if part.startswith("+ tools") and not sent:
            continue
        url, body = llm._request(msgs, stream=False, temperature=None, max_tokens=1, num_ctx=16384,
                                 sampling={"temperature": 0})
        if sent:
            body["tools"] = sent
        status, answer = post(url, body)
        row: dict[str, Any] = {"part": part}
        try:
            reference, which = render(t, msgs, rendered, kwargs)
        except Exception as e:   # noqa: BLE001 — a template that refuses the shape is said, not raised
            row["error"] = f"the template refused: {e}"[:200]
            out.append(row)
            continue
        n = count(f"tokenize_part_{i}", reference)
        p = (answer.get("usage") or {}).get("prompt_tokens") if status == 200 and isinstance(answer, dict) else None
        row.update(template=which, chat=p, tail=reference[-160:],
                   delta=(p - (n - n_empty + int(t.add_bos) + int(t.add_eos))) if p is not None and n is not None
                   else None)
        if status != 200:
            row["error"] = f"{status}: {str(answer)[:200]}"
        out.append(row)
    return out


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def _result(model: str, question: dict[str, str], which: str, t: Templates, reference: str,
            append: str, got: dict[str, Any], checks: list[Check],
            sampling: dict[str, Any] | None = None) -> dict[str, Any]:
    template = t.tool_use if which == "tool_use" else t.default
    return {"model": model, "key": question["key"], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "sampling": sampling,
            "passed": bool(checks) and all(c.ok for c in checks if c.decides),
            "checks": [asdict(c) for c in checks],
            "template": {"which": which, "sha256": hashlib.sha256((template or "").encode()).hexdigest(),
                         "gguf": t.source, "add_bos": t.add_bos, "add_eos": t.add_eos},
            "reference": {"sha256": hashlib.sha256(reference.encode()).hexdigest(), "chars": len(reference),
                          "head": reference[:400], "tail": reference[-800:], "appended": append},
            "answers": got}


def write(out: Path, results: list[dict[str, Any]], arm: str = "") -> Path:
    """probes/<model>.wire.json, the run gate's; a diagnostic arm (extra
    sampling) is written beside it as <model>.wire.<arm>.json and gates
    nothing."""
    model = results[0]["model"]
    p = out / "probes" / (f"{model}.wire.{arm}.json" if arm else f"{model}.wire.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"model": model, "passed": all(r["passed"] for r in results),
                             "questions": results}, indent=1, ensure_ascii=False), encoding="utf-8")
    return p


def table(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines += [(f"{r['model']} on {r['key']}: {'WIRED' if r['passed'] else 'NOT WIRED'} "
                   f"(template {r['template']['which']})"), "", "| check | ok | what was seen |", "|---|---|---|"]
        lines += [f"| {c['name']} | {'yes' if c['ok'] else ('**NO**' if c.get('decides', True) else 'no (reported)')} "
                  f"| {c['detail']} |" for c in r["checks"]]
        lines.append("")
    return "\n".join(lines)


def require(out: Path, model: str) -> None:
    """A run, or a pilot, on a model whose wiring is unchecked or wrong is
    void, as one whose sampling is (operator, 2026-11-06): refused, naming
    the failed checks."""
    p = out / "probes" / f"{model}.wire.json"
    if not p.exists():
        raise SystemExit(f"{model}: no wiring check (`chat_eval wire --model {model}`): whether the "
                         f"model is given its template's prompt, and Ask its output, is unknown, and "
                         f"the test would be void")
    got = json.loads(p.read_text(encoding="utf-8"))
    if not got.get("passed"):
        failed = "; ".join(f"{q['key']}: {c['name']} ({c['detail']})" for q in got.get("questions", [])
                           for c in q.get("checks", []) if not c["ok"] and c.get("decides", True))
        raise SystemExit(f"{model}: NOT WIRED — {failed}. The test would be void")
