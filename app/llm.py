"""Chat-model access (Oreste does the talking).

TWO PROTOCOLS, ONE CLIENT (`config.CHAT_API`):
- `ollama` — `/api/chat`, newline-delimited JSON, `think`, `keep_alive` and a
  per-request `num_ctx`. What the node has always run.
- `openai` — `/v1/chat/completions`, server-sent events. What llama-server
  serves, and so what RAGE serves (docs/notes/rage.md §4): the same Qwen3
  file Ollama holds, run by the node's model container. Thinking is turned
  off through the model's own chat template (`enable_thinking`, which the
  node's file knows — checked 2026-09-19); the context size is the
  server's, fixed at launch (operator, 2026-09-19), so `num_ctx` is sent to
  Ollama only. The model named is `config.CHAT_MODEL` either way; behind
  `/upstream/<model>` the URL has already chosen it.

Every request carries `Louise-Level` (docs/09-formats.md §9.12).
"""
import json
import re
from dataclasses import dataclass

import httpx
from . import config, dispatch
from .dispatch import Level

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)

def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()


def _request(messages: list[dict], *, stream: bool, temperature: float,
             max_tokens: int | None, num_ctx: int) -> tuple[str, dict]:
    """(url, body) in the protocol CHAT_URL speaks."""
    if config.CHAT_API == "openai":
        body: dict = {"model": config.CHAT_MODEL, "messages": messages, "stream": stream,
                      "temperature": temperature,
                      "chat_template_kwargs": {"enable_thinking": False}}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return f"{config.CHAT_URL}/v1/chat/completions", body
    options: dict = {"temperature": temperature, "num_ctx": num_ctx}
    if max_tokens is not None:
        options["num_predict"] = max_tokens
    return f"{config.CHAT_URL}/api/chat", {
        "model": config.CHAT_MODEL, "messages": messages, "stream": stream,
        "think": False, "keep_alive": config.LLM_KEEP_ALIVE, "options": options}


def _delta(line: str) -> tuple[str, bool]:
    """(the text one streamed line carries, whether the stream is over)."""
    if config.CHAT_API == "openai":
        if not line.startswith("data:"):
            return "", False                    # a comment, an event name, a blank
        data = line[5:].strip()
        if data == "[DONE]":
            return "", True
        try:
            choice = (json.loads(data).get("choices") or [{}])[0]
        except json.JSONDecodeError:
            return "", False
        return (choice.get("delta") or {}).get("content") or "", False
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return "", False
    return obj.get("message", {}).get("content", ""), bool(obj.get("done"))


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1500,
         num_ctx: int = 16384, *, level: Level = Level.CHAT) -> str:
    """`level`: CHAT, or TRANSLATE when the chat model stands in for the
    translation model — the two meet at CHAT_URL's gate, where chat goes
    first (docs/notes/scheduling.md §12)."""
    url, body = _request(messages, stream=False, temperature=temperature,
                         max_tokens=max_tokens, num_ctx=num_ctx)
    with dispatch.call(config.CHAT_URL, level, timeout=config.LLM_TIMEOUT), \
            httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        r = client.post(url, json=body, headers=dispatch.level_headers())
        r.raise_for_status()
        got = r.json()
        if config.CHAT_API == "openai":
            return strip_think(got["choices"][0]["message"]["content"] or "")
        return strip_think(got["message"]["content"])

@dataclass(frozen=True)
class ToolCall:
    """One call the model asked for: `id` in the OpenAI dialect, None in
    Ollama's; `arguments` parsed to a dict (an unreadable one is `{}`)."""
    id: str | None
    name: str
    arguments: dict


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            got = json.loads(raw)
            return got if isinstance(got, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def chat_tools(messages: list[dict], tools: list[dict] | None, temperature: float = 0.2,
               max_tokens: int = 1500, num_ctx: int = 16384, *,
               level: Level = Level.CHAT) -> tuple[str, list[ToolCall], dict]:
    """One round with function tools declared (`ask chat`, 05 §5.4):
    `(content, calls, assistant_message)` — the assistant message as the
    protocol wants it appended before the tool messages. `tools` None
    declares none, and the model must answer."""
    url, body = _request(messages, stream=False, temperature=temperature,
                         max_tokens=max_tokens, num_ctx=num_ctx)
    if tools:
        body["tools"] = tools
    with dispatch.call(config.CHAT_URL, level, timeout=config.LLM_TIMEOUT), \
            httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        r = client.post(url, json=body, headers=dispatch.level_headers())
        r.raise_for_status()
        got = r.json()
    msg = got["choices"][0]["message"] if config.CHAT_API == "openai" else got["message"]
    raw_calls = msg.get("tool_calls") or []
    calls = [ToolCall(tc.get("id"), tc["function"]["name"], _arguments(tc["function"].get("arguments")))
             for tc in raw_calls if isinstance(tc, dict) and isinstance(tc.get("function"), dict)]
    content = strip_think(msg.get("content") or "")
    assistant: dict = {"role": "assistant", "content": msg.get("content") or ""}
    if raw_calls:
        assistant["tool_calls"] = raw_calls
    return content, calls, assistant


def tool_message(call: ToolCall, content: str) -> dict:
    """A tool's answer, as the protocol wants it back."""
    if config.CHAT_API == "openai":
        return {"role": "tool", "tool_call_id": call.id or call.name, "content": content}
    return {"role": "tool", "content": content, "tool_name": call.name}


def chat_stream(messages: list[dict], temperature: float = 0.3,
                max_tokens: int | None = None, num_ctx: int = 16384, *,
                level: Level = Level.CHAT):
    """Yield content deltas. The claim and the gate are held for the whole
    stream: the model is working until the last token."""
    url, body = _request(messages, stream=True, temperature=temperature,
                         max_tokens=max_tokens, num_ctx=num_ctx)
    with dispatch.call(config.CHAT_URL, level, timeout=config.LLM_TIMEOUT), \
            httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        # the header is read HERE, in the step that makes the request: a
        # generator's later steps run in another copy of the context
        with client.stream("POST", url, json=body,
                           headers=dispatch.level_headers()) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                delta, over = _delta(line)
                if delta:
                    yield delta
                if over:
                    return

def translate_query(query: str, target_langs: list[str]) -> dict[str, str]:
    """One prompt, N translations, JSON out. Failure degrades to no fan-out.

    NO CAP ON N. A search goes out in every language its text indexes hold
    (docs/05 §5.2; operator, 2026-09-18), and this used to keep the first
    twelve codes it was handed, silently. The budget grows with the list
    instead: 19 languages measured 300 tokens, 59 measured 1,020."""
    targets = [l for l in target_langs if l]
    if not targets:
        return {}
    prompt = (
        "Translate the following search query into each of these ISO language "
        f"codes: {', '.join(targets)}.\n"
        f"Query: {query}\n"
        'Reply with ONLY a JSON object mapping language code to translation, '
        'e.g. {"fr": "...", "de": "..."}.'
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0,
                   max_tokens=max(600, 40 * len(targets)), level=Level.TRANSLATE)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {k: str(v) for k, v in obj.items() if isinstance(v, str)}
    except Exception:
        return {}


# A query may limit its results to documents WRITTEN IN some languages
# ("… solo testi in tedesco"); every other query searches all of them
# (operator, 2026-09-18). The model names the instruction VERBATIM and the
# code checks it, because qwen3:8b asked only for codes (measured on the
# node that day) restricted "Russian novels about war" to Russian, "lingua
# latina grammatica" to Latin, a plain Italian query to Italian, and
# rarely removed the instruction from the query. Returning the phrase made
# those misses — "Geschichte des Walfangs" still came back as the phrase
# for German — and a phrase that is the whole query, or is not in it, is
# refused: the failure is then a search over every language, which is the
# default, never a silent narrowing.
_RESTRICT_SYSTEM = (
    "You find, in a search query, an explicit instruction limiting the results to "
    "documents WRITTEN IN certain languages, such as 'only texts in German' or 'solo "
    "fonti in francese'. A language, nation or people that is the TOPIC of the search "
    "is not such an instruction ('Russian novels', 'German history', 'Latin grammar' "
    "are topics), and neither is the language the query itself is written in, nor the "
    "language of the answer. "
    'Reply with ONLY JSON: {"phrase": "<the instruction copied verbatim from the query, '
    'or empty>", "only": [<ISO 639-1 codes it names>]}.')
_RESTRICT_SHOTS = (
    ("German history of whaling", {"phrase": "", "only": []}),
    ("whaling history, only sources in German",
     {"phrase": "only sources in German", "only": ["de"]}),
    ("romanzi russi sulla guerra", {"phrase": "", "only": []}),
    ("poesia dell'Ottocento, solo testi in francese o spagnolo",
     {"phrase": "solo testi in francese o spagnolo", "only": ["fr", "es"]}),
    ("explain the causes of scurvy, answer in Italian", {"phrase": "", "only": []}),
)
_LANG_CODE = re.compile(r"[a-z]{2,3}")
#: what is left around a removed instruction: "whales, only in German" -> "whales"
_JOIN_EDGES = " \t\n,;:.-–—/|，、。；："


def strip_instruction(query: str, start: int, end: int) -> str:
    """`query` without `query[start:end]`, and the separators it leaves."""
    return re.sub(r"\s+", " ", query[:start] + " " + query[end:]).strip(_JOIN_EDGES)


def language_restriction(query: str) -> tuple[list[str], str] | None:
    """(codes, the query without the instruction) when `query` limits its
    results to documents written in those languages; ([], query) when it
    does not; None when the model could not be asked or answered nothing
    parseable — the caller searches every language and says so."""
    messages = [{"role": "system", "content": _RESTRICT_SYSTEM}]
    for q, a in _RESTRICT_SHOTS:
        messages += [{"role": "user", "content": q},
                     {"role": "assistant", "content": json.dumps(a, ensure_ascii=False)}]
    messages.append({"role": "user", "content": query})
    try:
        raw = chat(messages, temperature=0.0, max_tokens=120, num_ctx=4096)
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        if not isinstance(obj, dict):
            return None
    except Exception:
        return None
    phrase = obj.get("phrase") if isinstance(obj.get("phrase"), str) else ""
    only = obj.get("only") if isinstance(obj.get("only"), list) else []
    codes = sorted({c.strip().lower() for c in only if isinstance(c, str)
                    and _LANG_CODE.fullmatch(c.strip().lower())})
    phrase = phrase.strip()
    found = re.search(re.escape(phrase), query, re.I) if phrase else None
    if not codes or found is None:
        return [], query
    core = strip_instruction(query, found.start(), found.end())
    if not core:
        # the whole query called an instruction: the topic, misread
        return [], query
    return codes, core


# NAME THE LANGUAGE, NEVER JUST THE CODE. Asked to translate "into the
# language with ISO code 'it'", qwen3:8b answers {"1": "it", "2": "it", …}
# — it reads the code as the thing to emit. That parses as valid JSON with
# one non-empty string per passage, so the Ask page painted the literal
# text "it" over every snippet: the failure that passes every check and is
# wrong. With the name ("Italian (ISO code 'it')") the same 11 passages
# come back translated. Measured on the node, 2026-09-12: code → 5.0 s,
# 147 bytes, 11 × "it"; name → 56.1 s, 4569 bytes, 11 real translations.
# A code with no entry here degrades to the bare code, so
# tests/test_result_languages.py pins this table against the picker's.
LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "it": "Italian",
    "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
    "ru": "Russian", "ja": "Japanese", "zh": "Chinese", "tr": "Turkish",
    "fa": "Persian",
}


def language_phrase(lang: str) -> str:
    """How a prompt should name `lang` — the name plus the code, or the
    bare code when we have no name for it."""
    name = LANGUAGE_NAMES.get(lang.lower())
    return f"{name} (ISO code {lang!r})" if name else f"the language with ISO code {lang!r}"


_WS_RUN = re.compile(r"[ \t ]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def flatten_whitespace(text: str) -> str:
    """Collapse the ORIGIN'S PAGE LAYOUT before handing a passage to the
    model. Runs of spaces become one, CRLF becomes LF, three or more
    newlines become two.

    NECESSARY, not tidying. A Gutenberg snippet carries the book's
    typesetting — centred title pages, indented verse, CRLF — and on that
    input qwen3:8b degenerates: it starts reproducing the whitespace and
    emits `\\n` until it stops, returning 92 bytes of truncated JSON and
    losing the whole batch. Measured on the node, 2026-09-12, the same 11
    passages with the language NAMED either way:

        raw      1.6 s     92 B   0/11 translated
        cleaned  56.5 s  4569 B  11/11

    An earlier A/B appeared to clear cleaning of any role; it compared
    raw-with-code against cleaned-with-name and never isolated
    raw-with-name, which is the combination that fails.

    Only what is SENT is flattened. The snippet on the page and the
    original in the provenance drawer are untouched, and a translation is
    still compared against the original the caller sent.
    """
    return _BLANK_RUN.sub("\n\n", _WS_RUN.sub(
        " ", text.replace("\r\n", "\n").replace("\r", "\n"))).strip()


def _parse_numbered(raw: str) -> dict[str, str]:
    """The model's numbered JSON, salvaging a truncated tail.

    `json.loads` on the whole object is the fast path. When it raises —
    the model ran out of tokens mid-value, or degenerated — every
    COMPLETE `"n": "…"` pair is still recoverable, and losing only the
    tail beats losing the batch: a single bad passage used to take all
    eleven with it (`failed=True`, nothing translated, observed live).
    """
    body = raw[raw.find("{"): raw.rfind("}") + 1]
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return {str(k): v for k, v in obj.items() if isinstance(v, str)}
    except Exception:
        pass
    out: dict[str, str] = {}
    for m in re.finditer(r'"(\d+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw):
        try:
            out[m.group(1)] = json.loads('"' + m.group(2) + '"')
        except Exception:
            continue
    return out


def translate_texts(texts: list[str], lang: str) -> list[str | None]:
    """Translate several passages into ISO language `lang` in ONE prompt
    (STATUS 5j: the Ask page's "show results in"). Returns one entry per
    input — the translation, or **None** for a passage the model dropped —
    or [] when the whole call failed. Numbered JSON in and out keeps
    passages from bleeding into each other.

    NONE, NOT THE ORIGINAL, for a drop. This used to return the original
    text, which is indistinguishable from a translation that happens to
    match — so `/ask/translate` cached it as the translation for
    `(chunk_id, lang)` and that passage could never be translated again
    for the life of the process. Re-selecting the language could not
    repair it, which is precisely what the operator asked the switch to
    be able to do.

    THE FENCE IS `<<n>>`, not `[n]`. The batch used to be numbered `[1]`,
    `[2]` … and the Ask page's snippets already begin with their own rank
    marker `[1] `, so the model was handed `[1] [1] the lighthouse…` and
    had to guess which bracket was the protocol. A passage lost that way
    came back as a silent drop, which the caching above then made
    permanent. The marker has also left the snippet (see
    `_ask_results.html`), but the fence stays distinct so the two can
    never collide again — a citation `[3]` inside a passage is content and
    must survive verbatim.
    """
    items = [flatten_whitespace(t) for t in texts]
    if not items or not lang:
        return []
    numbered = "\n\n".join(f"<<{i + 1}>> {t}" for i, t in enumerate(items))
    prompt = (
        f"Translate each numbered passage below into {language_phrase(lang)}. "
        f"Each passage starts with a fence like <<2>> which is NOT part "
        f"of the text. Keep numbers, names, citations like [3] and formatting as "
        f"they are; do not summarise, do not add commentary.\n\n{numbered}\n\n"
        'Reply with ONLY a JSON object mapping each fence number to its '
        'translation, e.g. {"1": "...", "2": "..."}.'
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0,
                   # from the PRE-CLEAN lengths on purpose. The cap has to
                   # cover the OUTPUT, whose length tracks what the passage
                   # says and not how the origin laid it out, and 3279 —
                   # this formula over the uncleaned batch — is the value
                   # the 11/11 run was measured at. Deriving it from the
                   # flattened text would quietly tighten a budget nobody
                   # had re-measured.
                   max_tokens=max(600, 2 * sum(len(t) for t in texts) // 3 + 200),
                   num_ctx=16384, level=Level.TRANSLATE)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        obj = _parse_numbered(raw)
    except Exception:
        return []
    # NOTHING recovered is a whole-batch failure, not N misses: the page
    # says "translation into it unavailable — showing the originals",
    # which is the true account. A PARTIAL batch keeps the distinction —
    # those are misses, and the page names how many.
    if not obj:
        return []
    # An answer that is only the language's code or name is the echo
    # described above, not a translation. Belt and braces behind
    # `language_phrase`: it must surface as a MISS, never as content.
    out: list[str | None] = []
    for i, _original in enumerate(items):
        value = obj.get(str(i + 1))
        ok = (isinstance(value, str) and value.strip()
              and not _is_echo(value, lang))
        out.append(value if ok else None)
    return out


def _is_echo(value: str, lang: str) -> bool:
    echo = {lang.strip().lower(), (LANGUAGE_NAMES.get(lang.lower()) or "").lower()}
    echo.discard("")
    return value.strip().lower() in echo


def is_translation(value: str, original: str, lang: str) -> bool:
    """Whether a model's output for `original` may be shown and cached as
    its translation: not empty, not the language's name or code, and not
    the input handed back (compared as it was sent — flattened)."""
    return (bool(value.strip()) and not _is_echo(value, lang)
            and flatten_whitespace(value) != flatten_whitespace(original))


def translate_stream(text: str, lang: str):
    """ONE passage into ISO language `lang`, yielded as the model writes it.

    The Ask page translates its entries IN FULL (operator, 2026-09-18),
    and a whole chunk measured 58.6 s on the node (3,819 characters, 1,266
    tokens at 22 tok/s) — so a page of them is minutes, and the only way
    the switch can act "immediately" is to show the words as they come:
    the first arrived after 1.96 s. Plain text, not numbered JSON: one
    passage has no neighbours to keep apart, and JSON cannot be painted
    until it closes. The budget is the input's length in tokens, which no
    measured language pair has approached (1,266 tokens for 3,819 chars).
    """
    prompt = (
        f"Translate the passage below into {language_phrase(lang)}. Keep numbers, "
        f"names, citations like [3] and paragraph breaks as they are; translate all "
        f"of it, do not summarise, do not add commentary. Reply with ONLY the "
        f"translation.\n\n{flatten_whitespace(text)}")
    yield from chat_stream([{"role": "user", "content": prompt}], temperature=0.0,
                           max_tokens=max(600, len(text) + 200), level=Level.TRANSLATE)
