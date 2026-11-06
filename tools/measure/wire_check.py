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
2. CHAT: Ask's request itself (llm._request, Ask's tools), at greedy.
3. RAW: the reference sent to /v1/completions (no template: LocalAI hands
   a completion's prompt to the backend as it is), at greedy, twice. The
   reference is the one the chat request's count matched: the tools as Ask
   sends them, or with their keys sorted, as LocalAI's Go encoding writes
   them to the backend.
Wired when:
- INPUT: the chat request's prompt tokens equal the reference counted by
  the model's own tokenizer (LocalAI /v1/tokenize, special tokens parsed;
  what it adds by itself off, the engine's BOS/EOS on). Not the raw
  request's usage: LocalAI's /v1/completions reports 0 on this backend
  (the node, 2026-11-06);
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
          ask=None) -> dict[str, Any]:
    """Every check on one question; a refused request fails the check with
    the server's answer."""
    from app import config, llm
    config.CHAT_MODEL = model
    messages, tools = first_request(question["query"], collections=collections,
                                    search_cache=search_cache, ask=ask)
    url, body = llm._request(messages, stream=False, temperature=None, max_tokens=max_tokens,
                             num_ctx=16384, sampling={"temperature": 0})
    if tools:
        body["tools"] = tools
    kwargs = dict(body.get("chat_template_kwargs") or {})
    reference, which = render(t, messages, tools, kwargs)
    reference += append
    # THE TOOLS AS THE ENGINE GETS THEM (the node, 2026-11-06: every model's
    # prompt differed in the tools part alone, R7B -3, Qwen3-8B -2,
    # Qwen3.5-9B +1). LocalAI is Go: a tool's JSON object decoded into a map
    # and encoded again comes out with its keys sorted (encoding/json writes
    # map keys in sorted order). The same content rendered so is the second
    # reference; which one the engine's count matches is said.
    sorted_ref = (render(t, messages, go_order(tools), kwargs)[0] + append) if tools else None
    checks: list[Check] = []
    got: dict[str, Any] = {}

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
            return len(tokens)
        return None

    if asked("chat", url, body) is None:
        return _result(model, question, which, t, reference, append, got, checks)
    p_chat = (got["chat"]["answer"].get("usage") or {}).get("prompt_tokens")

    # INPUT. LocalAI's /v1/completions reports usage 0 on this backend (the
    # node, 2026-11-06, every model), so the reference is counted by the
    # model's tokenizer: what it adds on its own (an empty text's tokens) is
    # taken off and the BOS/EOS the engine adds put on, and a marker of the
    # template must read as ONE token (special tokens parsed), else the
    # count says nothing
    n_ref, n_empty = count("tokenize_reference", reference), count("tokenize_empty", "")
    empty_refused = n_empty is None and n_ref is not None
    n_empty = 0 if empty_refused else n_empty
    marker = re.search(r"<\|[^|>\s]+\|>|<[a-z_]+>", reference)
    n_marker = count("tokenize_marker", marker.group(0)) if marker else None
    if n_ref is None or n_empty is None:
        checks.append(Check("INPUT: the prompt is the template's", False,
                            "undecidable: /v1/tokenize gave no tokens"))
    elif marker and n_marker is not None and n_marker - n_empty != 1:
        checks.append(Check("INPUT: the prompt is the template's", False,
                            f"undecidable: /v1/tokenize reads {marker.group(0)} as {n_marker - n_empty} tokens"))
    else:
        added = int(t.add_bos) + int(t.add_eos) - n_empty
        expected = n_ref + added
        n_sorted = count("tokenize_reference_keys_sorted", sorted_ref) if sorted_ref is not None else None
        as_sorted = n_sorted is not None and n_sorted + added == p_chat and expected != p_chat
        if as_sorted:
            reference = sorted_ref
        checks.append(Check("INPUT: the prompt is the template's", expected == p_chat or as_sorted,
                            f"{p_chat} prompt tokens through chat; the template's text ({which}"
                            f"{', + appended' if append else ''}) is {expected}"
                            + (f"; with the tools' keys sorted, as LocalAI's Go encoding writes them, "
                               f"{n_sorted + added}" if n_sorted is not None else "")
                            + ("" if expected == p_chat or as_sorted else f", {(p_chat or 0) - expected:+d} through chat")
                            + (" (an empty text refused by /v1/tokenize: nothing assumed added)"
                               if empty_refused else "")))
        if expected != p_chat and not as_sorted:
            # WHERE the prompts part: the same comparison on cut-down
            # conversations, each asked for one token
            parts = localize(count, n_empty, t, messages, tools, kwargs, post, got)
            checks[-1].detail += "; by part: " + ", ".join(
                f"{v['part']} {v['delta']:+d}" if isinstance(v.get("delta"), int) else f"{v['part']} ?"
                for v in parts)
            got["localized"] = parts

    # the raw runs on the reference the engine's count matched, so the two
    # paths read the same prompt
    raw_body = {"model": model, "prompt": reference, "temperature": 0, "max_tokens": max_tokens}
    if asked("raw", f"{chat_url}/v1/completions", raw_body) is None \
            or asked("raw_again", f"{chat_url}/v1/completions", raw_body) is None:
        return _result(model, question, which, t, reference, append, got, checks)
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
            return _result(model, question, which, t, reference, append, got, checks)
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
    return _result(model, question, which, t, reference, append, got, checks)


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
            append: str, got: dict[str, Any], checks: list[Check]) -> dict[str, Any]:
    template = t.tool_use if which == "tool_use" else t.default
    return {"model": model, "key": question["key"], "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "passed": bool(checks) and all(c.ok for c in checks if c.decides),
            "checks": [asdict(c) for c in checks],
            "template": {"which": which, "sha256": hashlib.sha256((template or "").encode()).hexdigest(),
                         "gguf": t.source, "add_bos": t.add_bos, "add_eos": t.add_eos},
            "reference": {"sha256": hashlib.sha256(reference.encode()).hexdigest(), "chars": len(reference),
                          "head": reference[:400], "tail": reference[-800:], "appended": append},
            "answers": got}


def write(out: Path, results: list[dict[str, Any]]) -> Path:
    model = results[0]["model"]
    p = out / "probes" / f"{model}.wire.json"
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
