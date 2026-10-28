"""The node's translation model: TRANSLATE_URL, MADLAD-400 on the GPU, served
by LocalAI (deploy/localai/backends/louise-madlad). HTTP only — the app never loads a model
(invariant 7).

It translates everything the node translates: a search's query into every
language its text indexes hold (app/language_plan.py), and the Ask page's
titles, headings, entries and answer. The chat model did both, one word at
a time — 9.2 s idle and 17–28 s loaded for a query into 19 languages, 58–84 s
for one whole entry (2026-09-18) — and stays the fallback on a node with
no TRANSLATE_URL. What is not translation stays with it: writing the answer,
and reading "only texts in German" out of a question.

Every call is bounded (TIMEOUT_S): a search never waits on this without a
limit, and a failure is an empty answer the caller falls back from.

THE COMPLETION SHAPE (migration step 15e-2). The two requests — translate
`{"items": [...]}`, and the language list `{"languages": true}` — are each, as
JSON, the prompt of one /v1/completions call, and the completion's text is the
backend's JSON answer (deploy/localai/backends/louise-madlad/backend.py). The
server's own POST /translate and GET /languages, and TRANSLATE_API that chose
between the two, were retired at 15g. One prompt
carries the whole batch, because LocalAI calls the model once per prompt and
the model translates a batch in one pass (operator, 2026-10-03).

THE T5 SHAPE (2026-10-27): MADLAD on our llama.cpp backend (localai-bugs.md
P3, P4) takes one raw prompt per segment, "<2xx> segment"; this client cuts,
sends a call's segments at once and puts the pieces back. Which shape the
server speaks is determined from the server (`shape`), never configured.
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config, dispatch, model_load

#: one request's bound. Twelve whole entries in one call measured 33.6 s
#: on the node's GPU (batch 32), a query into 51 languages 1.5 s.
TIMEOUT_S = 120.0
#: how long the server's language list is trusted
_LANGUAGES_TTL_S = 600.0

_lock = threading.Lock()
_languages: tuple[float, frozenset[str]] | None = None


def available() -> bool:
    return bool(config.TRANSLATE_URL)


def health_url() -> str:
    """What the System page pings: a route that answers without loading the
    model."""
    return f"{config.TRANSLATE_URL}/v1/models"


def _completion(request: dict, timeout: float, headers: dict[str, str] | None = None):
    """One request in the completion shape: `request` as the prompt, the
    answer read back from the completion's text."""
    body = {"prompt": json.dumps(request, ensure_ascii=False)}
    if config.TRANSLATE_MODEL:
        body["model"] = config.TRANSLATE_MODEL
    r = httpx.post(f"{config.TRANSLATE_URL}/v1/completions", json=body,
                   timeout=timeout, headers=headers or {})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["text"])


def warm() -> None:
    """Load the translation model before a request when TRANSLATE_URL's
    runner says it is not loaded (app/model_load.py; operator, 2026-10-09:
    "Timeouts shall not include cold start"): one item, bounded by the
    model's load bound, so TIMEOUT_S — and the language list's 10 s, which a
    25 s load used to spend whole — is the request's alone. A load past its
    bound raises `model_load.LoadTimeout`; every caller here turns that into
    its usual empty answer."""
    if not config.TRANSLATE_MODEL:
        return

    def warm_request(bound_s: float) -> None:
        # the shape probe asks the model's tokenizer, which loads the model
        # when cold: bounded like the request that follows it
        if shape(bound_s) == SHAPE_T5:
            _t5_completion("it", "OK", bound_s, {dispatch.LEVEL_HEADER: "translate"}, max_tokens=4)
        else:
            _completion({"items": [{"text": "OK", "to": "it"}]}, bound_s,
                        {dispatch.LEVEL_HEADER: "translate"})

    model_load.ensure_loaded(config.TRANSLATE_URL, config.TRANSLATE_MODEL, warm_request,
                             caller="translation", max_age_s=0.0)


def languages() -> frozenset[str]:
    """The target codes the server knows; empty when it cannot be asked."""
    global _languages
    if not available():
        return frozenset()
    with _lock:
        cached = _languages
    if cached is not None and time.monotonic() - cached[0] < _LANGUAGES_TTL_S:
        return cached[1]
    try:
        warm()
        if shape() == SHAPE_T5:
            got = _t5_languages()
        else:
            answer = _completion({"languages": True}, 10.0)
            got = frozenset(str(c) for c in answer.get("languages", []))
    except Exception:   # noqa: BLE001 — unreachable is "no languages", not an error
        return frozenset()
    with _lock:
        _languages = (time.monotonic(), got)
    return got


def reset() -> None:
    global _languages, _shape_cache
    with _lock:
        _languages = None
        _shape_cache = None


def translate(items: list[tuple[str, str]]) -> list[str | None]:
    """(text, target code) pairs -> one translation per pair, None where the
    model knows no such target; [] when the call failed as a whole."""
    if not items or not available():
        return []
    request = {"items": [{"text": t, "to": to} for t, to in items]}
    try:
        warm()
        if shape() == SHAPE_T5:
            out = _t5_translate(items)
        else:
            with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE,
                               timeout=TIMEOUT_S):
                answer = _completion(request, TIMEOUT_S, dispatch.level_headers())
            out = answer.get("translations")
    except Exception:   # noqa: BLE001 — the caller falls back and says so
        return []
    if not isinstance(out, list) or len(out) != len(items):
        return []
    return [o if isinstance(o, str) and o.strip() else None for o in out]


# ---- the two shapes of TRANSLATE_URL's server (2026-10-27) ----------------------------
#
# SHAPE_BATCH is the louise-python backend above: one JSON prompt carries the
# whole batch, and it segments, packs and reassembles the texts itself.
# SHAPE_T5 is MADLAD on our llama.cpp backend (docs/notes/localai-bugs.md P3,
# P4): a raw completion per SEGMENT, "<2xx> segment", greedy; the segmenting
# (deploy/madlad-translate/server.py's, ported below), the batching and the
# reassembly are this client's, and a question's languages go out at once so
# the server encodes and decodes them in one wave (P4). The shape is
# DETERMINED from the server, never configured (RULE 2): a runner whose
# tokenizer makes ONE token of "<2en>" is a T5 translator with MADLAD's target
# tokens; the python backend has no tokenizer route.

SHAPE_BATCH = "batch"
SHAPE_T5 = "t5"
#: how long a determined shape is trusted
_SHAPE_TTL_S = 600.0
#: a segment's size in (estimated) tokens: MADLAD saw sentences, its window
#: is 512; 96 is what the torch backend packs to (deploy/madlad-translate)
MAX_SEG_TOKENS = 96
#: concurrent requests of one call: the server's slots (parallel:7, one wave)
T5_CONCURRENCY = 7
#: an ISO code the platform stores -> the token MADLAD was trained with,
#: where the two name the same language differently
ALIASES = {"tl": "fil", "gla": "gd", "oji": "oj", "iw": "he", "in": "id", "ji": "yi"}
#: transformers' clean_up_tokenization, which MADLAD's config switches on
#: (clean_up_tokenization_spaces: true): the torch backend's output had it
_CLEAN = ((" .", "."), (" ?", "?"), (" !", "!"), (" ,", ","), (" ' ", "'"), (" n't", "n't"),
          (" 'm", "'m"), (" 's", "'s"), (" 've", "'ve"), (" 're", "'re"))
_LANG_TOKEN = re.compile(r"<2([A-Za-z0-9_\-]+)>")
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?;:。！？；])\s+")
_SPACE = re.compile(r"\s+")

_shape_cache: tuple[float, str] | None = None


def _tokenize(text: str, timeout: float) -> list[int] | None:
    """The runner's tokens for `text` (LocalAI /v1/tokenize, served by the
    model's own tokenizer); None when it has no such route or fails."""
    body = {"content": text}
    if config.TRANSLATE_MODEL:
        body["model"] = config.TRANSLATE_MODEL
    r = httpx.post(f"{config.TRANSLATE_URL}/v1/tokenize", json=body, timeout=timeout)
    if r.status_code != 200:
        return None
    tokens = r.json().get("tokens")
    return tokens if isinstance(tokens, list) else None


def shape(timeout: float = 10.0) -> str:
    """Which shape TRANSLATE_URL's server speaks, determined from it and
    trusted `_SHAPE_TTL_S`; a server that cannot be asked is the batch
    shape for this call only (the call then fails as it always did)."""
    global _shape_cache
    with _lock:
        cached = _shape_cache
    if cached is not None and time.monotonic() - cached[0] < _SHAPE_TTL_S:
        return cached[1]
    try:
        tokens = _tokenize("<2en>", timeout)
    except Exception:   # noqa: BLE001 — unreachable: undetermined, asked again next time
        return SHAPE_BATCH
    got = SHAPE_T5 if tokens is not None and len(tokens) == 1 else SHAPE_BATCH
    with _lock:
        _shape_cache = (time.monotonic(), got)
    return got


def target_code(code: str) -> str:
    code = code.strip().lower()
    return ALIASES.get(code, code)


def estimate_tokens(text: str) -> int:
    """About how many tokens MADLAD's tokenizer makes of `text`: one per CJK,
    kana, Hangul or full-width character, one per 3.5 others. An estimate on
    the short side: segments come out a little longer than the torch
    backend's exact 96, well inside the model's 512 and the wave's 1,024."""
    n = 0.0
    for ch in text:
        o = ord(ch)
        if (0x2E80 <= o <= 0x9FFF) or (0xAC00 <= o <= 0xD7AF) or (0xF900 <= o <= 0xFAFF) \
                or (0xFF00 <= o <= 0xFFEF):
            n += 1.0
        elif not ch.isspace():
            n += 1 / 3.5
    return int(n) + 1


def split_segments(text: str, max_tokens: int, count) -> list[list[str]]:
    """`text` as paragraphs of segments, each at most `max_tokens` by `count`
    where a sentence allows it — deploy/madlad-translate/server.py's rule,
    so the two shapes cut a text the same way. A paragraph's own line wraps
    are layout, not content (a Gutenberg page wraps at 70 columns), so it is
    flattened first; paragraph breaks survive."""
    paragraphs = []
    for para in _PARAGRAPH.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        para = _SPACE.sub(" ", para).strip()
        if not para:
            continue
        segs: list[str] = []
        current = ""
        for sentence in _SENTENCE.split(para):
            pieces = [sentence]
            if count(sentence) > max_tokens:
                # a "sentence" longer than the window: cut on words
                words, pieces, piece = sentence.split(" "), [], ""
                for w in words:
                    trial = f"{piece} {w}".strip()
                    if piece and count(trial) > max_tokens:
                        pieces.append(piece)
                        piece = w
                    else:
                        piece = trial
                if piece:
                    pieces.append(piece)
            for p in pieces:
                trial = f"{current} {p}".strip()
                if current and count(trial) > max_tokens:
                    segs.append(current)
                    current = p
                else:
                    current = trial
        if current:
            segs.append(current)
        paragraphs.append(segs)
    return paragraphs


def clean_up(text: str) -> str:
    for a, b in _CLEAN:
        text = text.replace(a, b)
    return text.strip()


def _t5_completion(code: str, segment: str, timeout: float, headers: dict[str, str] | None = None,
                   max_tokens: int | None = None) -> str:
    """One segment through the model: the raw prompt MADLAD was trained with,
    greedy, the output bounded as the torch backend bounds a batch
    (min(512, 2 × input + margin)); the completion's text, cleaned up."""
    prompt = f"<2{code}> {segment}"
    body = {"prompt": prompt, "temperature": 0, "top_k": 1, "seed": 0,
            "max_tokens": max_tokens if max_tokens is not None else min(512, 2 * estimate_tokens(prompt) + 32)}
    if config.TRANSLATE_MODEL:
        body["model"] = config.TRANSLATE_MODEL
    r = httpx.post(f"{config.TRANSLATE_URL}/v1/completions", json=body, timeout=timeout,
                   headers=headers or {})
    r.raise_for_status()
    return clean_up(r.json()["choices"][0]["text"])


def _t5_languages(timeout: float = 30.0) -> frozenset[str]:
    """The target codes the MODEL knows, read off its own vocabulary: every
    `<2xx>` token, found by detokenizing the token ids in chunks (LocalAI
    /v1/detokenize renders special tokens as their pieces); a chunk past the
    vocabulary's end fails and is halved, down to one id. About 70 requests
    once per TTL, a few seconds. The platform's own codes too, where
    ALIASES maps them to a known one."""
    codes: set[str] = set()
    start, step = 0, 4096
    while step >= 1 and start < 1_000_000:
        body: dict = {"tokens": list(range(start, start + step))}
        if config.TRANSLATE_MODEL:
            body["model"] = config.TRANSLATE_MODEL
        r = httpx.post(f"{config.TRANSLATE_URL}/v1/detokenize", json=body, timeout=timeout)
        if r.status_code != 200:
            step //= 2                       # past the end: narrow the chunk
            continue
        codes |= set(_LANG_TOKEN.findall(r.json().get("content") or ""))
        start += step
    return frozenset(codes | {a for a, c in ALIASES.items() if c in codes})


def _t5_translate(items: list[tuple[str, str]]) -> list[str | None]:
    """The t5 shape of `translate`: each item cut into segments as the torch
    backend cuts them, every segment one request, the requests of one call
    sent at once (T5_CONCURRENCY) so the server takes them in one wave; the
    pieces put back with the text's paragraph breaks. An unknown target is
    None; an empty or runaway output (a llama.cpp translator can run on:
    translator-choice.md) makes its item None; any failed request fails the
    call, as one failed batch did."""
    known = languages()
    out: list[str | None] = [None] * len(items)
    jobs: list[tuple[int, int, int, str, str]] = []       # (item, paragraph, segment, code, text)
    shapes: dict[int, list[int]] = {}
    for i, (text, to) in enumerate(items):
        code = target_code(to)
        if code not in known:
            continue
        paragraphs = split_segments(text, MAX_SEG_TOKENS, estimate_tokens)
        shapes[i] = [len(p) for p in paragraphs]
        for pi, segs in enumerate(paragraphs):
            for si, seg in enumerate(segs):
                jobs.append((i, pi, si, code, seg))
    if not jobs:
        return out
    with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE, timeout=TIMEOUT_S):
        headers = dispatch.level_headers()      # read here: a pool thread carries no level
        with ThreadPoolExecutor(max_workers=min(T5_CONCURRENCY, len(jobs))) as pool:
            got = list(pool.map(lambda j: _t5_completion(j[3], j[4], TIMEOUT_S, headers), jobs))
    done: dict[tuple[int, int, int], str] = {}
    failed: set[int] = set()
    for (i, pi, si, _code, seg), text in zip(jobs, got):
        if not text or len(text) > 4 * len(seg) + 64:
            failed.add(i)
        done[(i, pi, si)] = text
    for i, counts in shapes.items():
        if i in failed:
            continue
        out[i] = "\n\n".join(" ".join(done[(i, pi, si)] for si in range(n))
                             for pi, n in enumerate(counts))
    return out


# ---- reading translation by source language (operator, 2026-10-12) -------------------
#
# `[translate] reading` in platform.toml maps a SOURCE language to a model for
# what is READ — the Ask page's results and its reader — where TRANSLATE_MODEL
# is weak: Index-Translate-9B for Latin and Ancient Greek (chrF 51.6 / 51.9
# against MADLAD's 34.4 / 47.3; docs/notes/translator-choice.md). Searches
# never come here. One item per call: these are chat models, not MADLAD's
# batched backend. A failure is None and the caller hands the item to
# TRANSLATE_MODEL, so a reading model only ever improves a translation.

#: one reading call's bound: a whole passage measured 31 s median on the node
READING_TIMEOUT_S = 240.0
#: Index-Translate's card asks in Chinese and names the target in Chinese
_ZH = {"en": "英语", "fr": "法语", "fi": "芬兰语", "de": "德语", "zh": "中文", "ja": "日语",
       "ko": "韩语", "it": "意大利语", "es": "西班牙语", "pt": "葡萄牙语", "nl": "荷兰语",
       "sv": "瑞典语", "no": "挪威语", "da": "丹麦语", "pl": "波兰语", "ru": "俄语",
       "uk": "乌克兰语", "cs": "捷克语", "hu": "匈牙利语", "ro": "罗马尼亚语",
       "el": "希腊语", "tr": "土耳其语", "ar": "阿拉伯语", "he": "希伯来语", "hi": "印地语",
       "la": "拉丁语", "grc": "古希腊语", "ca": "加泰罗尼亚语", "eo": "世界语"}
_EN = {"en": "English", "fr": "French", "fi": "Finnish", "de": "German", "zh": "Chinese",
       "ja": "Japanese", "ko": "Korean", "it": "Italian", "es": "Spanish", "pt": "Portuguese",
       "nl": "Dutch", "sv": "Swedish", "no": "Norwegian", "da": "Danish", "pl": "Polish",
       "ru": "Russian", "uk": "Ukrainian", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian",
       "el": "Greek", "tr": "Turkish", "ar": "Arabic", "he": "Hebrew", "hi": "Hindi",
       "la": "Latin", "grc": "Ancient Greek", "ca": "Catalan", "eo": "Esperanto"}


def reading_model(src: str) -> str | None:
    """The model `[translate] reading` names for text in `src`, or None."""
    if not available():
        return None
    from . import platform_config
    try:
        return platform_config.load().translate.reading.get((src or "").lower())
    except Exception:   # noqa: BLE001 — an unreadable file: TRANSLATE_MODEL, as before
        return None


def _reading_request(model: str, text: str, src: str, to: str) -> tuple[str, dict] | None:
    """(path, body) for one reading call, or None when this model cannot be
    asked for `to`. Greedy, as measured."""
    greedy = {"temperature": 0, "top_k": 1, "top_p": 1.0, "seed": 0,
              "max_tokens": min(8192, len(text) + 256)}
    if model.startswith("index-translate"):
        if to not in _ZH:
            return None
        # LocalAI drops chat_template_kwargs, so the model would think aloud:
        # its own template with enable_thinking=false, rendered here — an
        # empty think block after the turn (measured 2026-10-10)
        prompt = (f"<|im_start|>user\n请将以下文本翻译为{_ZH[to]}，直接输出翻译结果，"
                  f"不要进行任何解释。\n\n{text}<|im_end|>\n"
                  "<|im_start|>assistant\n<think>\n\n</think>\n\n")
        return "/v1/completions", {"model": model, "prompt": prompt, "stop": ["<|im_end|>"], **greedy}
    if to not in _EN:
        return None
    ask = (f"Translate the following {_EN.get(src, src)} text into {_EN[to]}. "
           f"Output only the translation.\n\n{text}")
    return "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": ask}], **greedy}


def translate_reading(model: str, text: str, src: str, to: str) -> str | None:
    """One text through a reading model; None when it cannot or did not
    answer — the caller then asks TRANSLATE_MODEL."""
    req = _reading_request(model, text, src, to)
    if req is None or not available():
        return None
    path, body = req

    def call(timeout: float) -> dict:
        r = httpx.post(f"{config.TRANSLATE_URL}{path}", json=body, timeout=timeout,
                       headers=dispatch.level_headers())
        r.raise_for_status()
        return r.json()
    try:
        model_load.ensure_loaded(
            config.TRANSLATE_URL, model,
            lambda bound_s: _reading_warm(model, bound_s), caller="translation (reading)", max_age_s=0.0)
        with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE, timeout=READING_TIMEOUT_S):
            choice = call(READING_TIMEOUT_S)["choices"][0]
    except Exception:   # noqa: BLE001 — the caller falls back to TRANSLATE_MODEL
        return None
    out = choice.get("text") if path == "/v1/completions" else (choice.get("message") or {}).get("content")
    out = (out or "").replace("<think>", "").replace("</think>", "").strip()
    return out or None


def _reading_warm(model: str, bound_s: float) -> None:
    path, body = _reading_request(model, "OK", "en", "it") or ("/v1/completions", {"model": model, "prompt": "OK"})
    r = httpx.post(f"{config.TRANSLATE_URL}{path}", json={**body, "max_tokens": 4},
                   timeout=bound_s, headers={dispatch.LEVEL_HEADER: "translate"})
    r.raise_for_status()
