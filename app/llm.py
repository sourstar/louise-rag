"""Chat-model access via Ollama (Oreste does the talking)."""
import json
import re
import httpx
from . import config

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)

def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()

def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1500,
         num_ctx: int = 16384) -> str:
    with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        r = client.post(f"{config.OLLAMA_URL}/api/chat", json={
            "model": config.CHAT_MODEL, "messages": messages, "stream": False,
            "think": False, "keep_alive": config.LLM_KEEP_ALIVE,
            "options": {"temperature": temperature, "num_predict": max_tokens,
                        "num_ctx": num_ctx},
        })
        r.raise_for_status()
        return strip_think(r.json()["message"]["content"])

def chat_stream(messages: list[dict], temperature: float = 0.3):
    """Yield content deltas."""
    with httpx.Client(timeout=config.LLM_TIMEOUT) as client:
        with client.stream("POST", f"{config.OLLAMA_URL}/api/chat", json={
            "model": config.CHAT_MODEL, "messages": messages, "stream": True,
            "think": False, "keep_alive": config.LLM_KEEP_ALIVE,
            "options": {"temperature": temperature, "num_ctx": 16384},
        }) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                delta = obj.get("message", {}).get("content", "")
                if delta:
                    yield delta
                if obj.get("done"):
                    return

def translate_query(query: str, target_langs: list[str]) -> dict[str, str]:
    """One prompt, N translations, JSON out. Failure degrades to no fan-out."""
    targets = [l for l in target_langs if l][:12]
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
        raw = chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=600)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = raw[raw.find("{"):]
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {k: str(v) for k, v in obj.items() if isinstance(v, str)}
    except Exception:
        return {}


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
                   num_ctx=16384)
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
    echo = {lang.strip().lower(), (LANGUAGE_NAMES.get(lang.lower()) or "").lower()}
    echo.discard("")
    out: list[str | None] = []
    for i, _original in enumerate(items):
        value = obj.get(str(i + 1))
        ok = (isinstance(value, str) and value.strip()
              and value.strip().lower() not in echo)
        out.append(value if ok else None)
    return out
