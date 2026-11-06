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
2. RAW: that text sent to /v1/completions — no template, LocalAI hands a
   completion's prompt to the backend as it is — at greedy, twice (before
   and after the chat request: greedy must repeat itself).
3. CHAT: Ask's request itself (llm._request, Ask's tools), at greedy.
Wired when:
- INPUT: both requests count the same prompt tokens (`usage.prompt_tokens`);
- OUTPUT: both generate the same number of tokens; the chat answer's text is
  inside the raw text; every tool call the chat answer carries is in the raw
  text, name and argument values; a tool named as a JSON string in the raw
  text is not missing from the chat answer's calls; no control marker is
  left in the answer.
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
    reference, which = render(t, messages, tools, dict(body.get("chat_template_kwargs") or {}))
    reference += append
    raw_body = {"model": model, "prompt": reference, "temperature": 0, "max_tokens": max_tokens}
    checks: list[Check] = []
    got: dict[str, Any] = {}
    for label, u, b in (("raw", f"{chat_url}/v1/completions", raw_body), ("chat", url, body),
                        ("raw_again", f"{chat_url}/v1/completions", raw_body)):
        status, answer = post(u, b)
        got[label] = {"status": status, "answer": answer}
        if status != 200:
            checks.append(Check("every request answered", False, f"{label}: {status}: {str(answer)[:300]}"))
            return _result(model, question, which, t, reference, append, got, checks)
    raw = got["raw"]["answer"]["choices"][0].get("text") or ""
    again = got["raw_again"]["answer"]["choices"][0].get("text") or ""
    msg = got["chat"]["answer"]["choices"][0].get("message") or {}
    use_raw = got["raw"]["answer"].get("usage") or {}
    use_chat = got["chat"]["answer"].get("usage") or {}
    content = msg.get("content") or ""
    calls = [c.get("function") or {} for c in msg.get("tool_calls") or [] if isinstance(c, dict)]

    checks.append(Check("greedy repeats itself (raw, twice)", raw == again,
                        "the same text" if raw == again else f"{len(raw)} then {len(again)} characters"))
    p_raw, p_chat = use_raw.get("prompt_tokens"), use_chat.get("prompt_tokens")
    checks.append(Check("INPUT: the prompt is the template's", p_raw is not None and p_raw == p_chat,
                        f"{p_chat} prompt tokens through chat, {p_raw} through the template "
                        f"({which}{', + appended' if append else ''})"))
    c_raw, c_chat = use_raw.get("completion_tokens"), use_chat.get("completion_tokens")
    checks.append(Check("OUTPUT: the same tokens generated", c_raw is not None and c_raw == c_chat,
                        f"{c_chat} through chat, {c_raw} raw"))
    inside = _norm(content) in _norm(raw)
    checks.append(Check("OUTPUT: the answer's text is the model's", inside,
                        "inside the raw text" if inside else f"not in the raw text: {content[:160]!r}"))
    lost_args = [f"{c.get('name')}: {s[:60]!r}" for c in calls
                 for s in ([c.get("name") or ""] + _strings(_args(c.get("arguments"))))
                 if s and s not in raw and json.dumps(s, ensure_ascii=False)[1:-1] not in raw]
    named = sorted({(f.get("function") or {}).get("name") for f in tools or []} - {None})
    in_raw = [n for n in named if f'"{n}"' in raw]
    called = {c.get("name") for c in calls}
    lost_calls = [n for n in in_raw if n not in called]
    checks.append(Check("OUTPUT: every tool call kept", not lost_args and not lost_calls,
                        f"{len(calls)} call(s) parsed" + (f"; not in the raw text: {lost_args}" if lost_args else "")
                        + (f"; in the raw text, not parsed: {lost_calls}" if lost_calls else "")))
    left = sorted(set(MARKERS.findall(content)))
    checks.append(Check("OUTPUT: no marker left in the answer", not left,
                        "none" if not left else ", ".join(left)))
    return _result(model, question, which, t, reference, append, got, checks)


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
            "passed": bool(checks) and all(c.ok for c in checks),
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
        lines += [f"| {c['name']} | {'yes' if c['ok'] else '**NO**'} | {c['detail']} |" for c in r["checks"]]
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
                           for c in q.get("checks", []) if not c["ok"])
        raise SystemExit(f"{model}: NOT WIRED — {failed}. The test would be void")
