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


def _completion(request: dict, timeout: float, headers: dict[str, str] | None = None,
                max_tokens: int | None = None):
    """One request in the batch shape: `request` as the prompt, the answer
    read back from the completion's text. Every request carries a bound:
    the torch backend ignores it, and a llama.cpp runner that was taken for
    the batch shape by mistake then costs seconds, not the 92 s of
    2026-10-28 (the first switch, backed out), when the JSON prompt ran to
    the slot's limit."""
    body = {"prompt": json.dumps(request, ensure_ascii=False),
            "max_tokens": max_tokens if max_tokens is not None else _batch_bound(request)}
    if config.TRANSLATE_MODEL:
        body["model"] = config.TRANSLATE_MODEL
    r = httpx.post(f"{config.TRANSLATE_URL}/v1/completions", json=body,
                   timeout=timeout, headers=headers or {})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["text"])


def _batch_bound(request: dict) -> int:
    """The output bound of a batch-shape request: the torch backend's own
    per-segment bound (min(512, 2 × input + 32)) summed over the items, plus
    the JSON around them — generous for a true batch answer, and a few
    seconds on a llama.cpp runner that should never have been asked."""
    items = request.get("items") if isinstance(request, dict) else None
    if not items:
        return BATCH_MAX_TOKENS
    return min(32768, 256 + sum(2 * estimate_tokens(str(it.get("text", ""))) + 64 for it in items))


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
                        {dispatch.LEVEL_HEADER: "translate"}, max_tokens=16)

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
#: a llama.cpp runner is MADLAD when its vocabulary holds at least this many
#: `<2xx>` target tokens (MADLAD has ~450; a chat model none)
T5_MIN_CODES = 16
#: the bound of a batch-shape request that is not a translation of items
BATCH_MAX_TOKENS = 4096
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
#: an inline citation as the chat model is asked to write it (retrieval.
#: CITE_REMINDER: "[2] or [2][5]") and as the Ask page links it (ask.html
#: linkCitations): digits in square brackets, nothing else. MADLAD does not
#: keep them (measured on the node 2026-10-28, a 3,300-character answer with
#: 36 markers: six dropped into Italian, "[8]" renumbered "[10]" into
#: English), so they never reach the model: `_strip_cites` takes them out of
#: a segment and `_cite` puts them back after it — the standard handling of
#: inline codes in machine translation (XLIFF/Okapi: codes are removed before
#: the engine and restored after; translator-choice.md).
_CITE = re.compile(r"\[\d+\]")
#: a run of citations with the space before it: "castle [1][2]." -> "castle."
_CITE_RUN = re.compile(r"(?:\s*\[\d+\])+")
#: a run written after a sentence's stop cites THAT sentence ("castle. [1] In
#: London"); it is moved inside the stop so the cut keeps it with its sentence
#: (across at most one line break: a run opening a new paragraph is that paragraph's)
_CITE_AFTER_STOP = re.compile(r"([.!?;:。！？；])([^\S\n]*\n?[^\S\n]*)((?:\[\d+\](?:\s*\[\d+\])*))")
_WORD = re.compile(r"\w")
_STOPS = ".!?;:"
_CJK_STOPS = "。！？；"
#: a line that is structure, not a wrapped line — a list item, a heading: the
#: break before it (and after a heading) is kept, and the marker itself stays
#: out of the model's sight (MADLAD drops "- " and makes one sentence of a
#: list: the node, 2026-10-28)
_LEAD = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)]|#{1,6})[ \t]+")

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
    shape for this call only (the call then fails as it always did).

    A runner with no tokenizer route is the python backend: the batch shape.
    One with the route is a llama.cpp runner, and MADLAD is told by its
    VOCABULARY — the `<2xx>` target tokens `_t5_languages` reads off
    /v1/detokenize (kept for `languages()`) — never by how the route
    tokenizes "<2en>": LocalAI's /v1/tokenize splits it into two pieces
    (recorded on the node 2026-10-28: `[805, 38]`), while the completion
    path reads it as one token. The first switch was backed out on that
    difference: the probe said batch, and the warm's JSON prompt ran 92 s."""
    global _shape_cache, _languages
    with _lock:
        cached = _shape_cache
    if cached is not None and time.monotonic() - cached[0] < _SHAPE_TTL_S:
        return cached[1]
    try:
        tokens = _tokenize("<2en>", timeout)
        if tokens is None:
            got, codes = SHAPE_BATCH, None
        else:
            codes = _t5_languages(timeout=max(timeout, 30.0))
            got = SHAPE_T5 if len(codes) >= T5_MIN_CODES else SHAPE_BATCH
    except Exception:   # noqa: BLE001 — unreachable: undetermined, asked again next time
        return SHAPE_BATCH
    with _lock:
        _shape_cache = (time.monotonic(), got)
        if got == SHAPE_T5:
            _languages = (time.monotonic(), codes)   # the scan is the language list too
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


def split_segments(text: str, max_tokens: int, count, close_after=None) -> list[list[str]]:
    """`text` as paragraphs of segments, each at most `max_tokens` by `count`
    where a sentence allows it — deploy/madlad-translate/server.py's rule,
    so the two shapes cut a text the same way. A paragraph's own line wraps
    are layout, not content (a Gutenberg page wraps at 70 columns), so it is
    flattened first; paragraph breaks survive. `close_after(sentence)` true
    ends the segment after that sentence (the t5 shape closes one after a
    cited sentence, so a segment's citations are its last sentence's)."""
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
            if close_after is not None and current and close_after(sentence):
                segs.append(current)
                current = ""
        if current:
            segs.append(current)
        paragraphs.append(segs)
    return paragraphs


def _blocks(text: str) -> list[tuple[str, str, str]]:
    """`text` as (separator before, lead, body) blocks for the t5 cut: a blank
    line separates paragraphs ("\n\n"); a line break before a list item or a
    heading, or after a heading, is structure ("\n") and the item's or
    heading's marker is the block's `lead`, kept out of the model's sight;
    every other line break is a wrapped line, flattened by `split_segments`."""
    out: list[tuple[str, str, str]] = []
    sep = ""
    for para in _PARAGRAPH.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [ln for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        block: list[str] = []
        for ln in lines:
            if block and (_LEAD.match(ln) or _LEAD.match(block[0])):
                out.append((sep, *_lead(" ".join(block))))
                sep, block = "\n", []
            block.append(ln.strip())
        out.append((sep, *_lead(" ".join(block))))
        sep = "\n\n"
    return out


def _lead(block: str) -> tuple[str, str]:
    m = _LEAD.match(block)
    return (m.group(0).strip() + " ", block[m.end():]) if m else ("", block)


def _strip_cites(segment: str) -> tuple[str, str]:
    """A segment without its citations, and the citations as one run to put
    back after it: ("castle [1]. In London [2][3].") -> ("castle. In London.",
    "[1] [2][3]")."""
    runs = [r.strip() for r in _CITE_RUN.findall(segment)]
    return _SPACE.sub(" ", _CITE_RUN.sub("", segment)).strip(), " ".join(runs)


def _cite(text: str, cites: str) -> str:
    """`cites` back on the translated segment, inside its final stop as the
    chat model writes them ("castello [1]."), after it otherwise."""
    if not cites:
        return text
    if not text:
        return cites
    if text[-1] in _STOPS:
        return f"{text[:-1].rstrip()} {cites}{text[-1]}"
    if text[-1] in _CJK_STOPS:
        return f"{text[:-1]}{cites}{text[-1]}"
    return f"{text} {cites}"


def _bare_count(sentence: str) -> int:
    return estimate_tokens(_CITE_RUN.sub("", sentence))


def _cited(sentence: str) -> bool:
    return _CITE.search(sentence) is not None


def clean_up(text: str) -> str:
    for a, b in _CLEAN:
        text = text.replace(a, b)
    return _unglue(text.strip())


def _unglue(text: str) -> str:
    """The space back between two sentences the model wrote as one word —
    "letterarie.Le prime spade" (the node, 2026-10-28: now and then, in a
    segment of two sentences, the output drops the space the input had). A
    stop between two lowercase letters and an uppercase letter is such a
    join; an abbreviation ("Ph.D.", "U.S.A.", "e.g.") or a number is not."""
    out: list[str] = []
    for i, ch in enumerate(text):
        out.append(ch)
        if ch in _STOPS[:3] and i >= 2 and i + 1 < len(text) and _lower(text[i - 2]) \
                and _lower(text[i - 1]) and text[i + 1].isalpha() and text[i + 1].isupper():
            out.append(" ")
    return "".join(out)


def _lower(ch: str) -> bool:
    return ch.isalpha() and ch.islower()


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
    pieces put back with the text's paragraph breaks and list lines
    (`_blocks`). The `[n]` citations and the list markers never reach the
    model (`_strip_cites`, `_lead`): a segment ends with the sentence that
    carries a citation, its citations go back after it (`_cite`), and a run
    written after a stop stays with the sentence it follows. An unknown
    target is None; an empty or runaway output (a llama.cpp translator can
    run on: translator-choice.md) makes its item None; any failed request
    fails the call, as one failed batch did."""
    known = languages()
    out: list[str | None] = [None] * len(items)
    jobs: list[tuple[int, int, int, str, str]] = []       # (item, block, segment, code, text)
    shapes: dict[int, list[tuple[str, str, int]]] = {}    # item -> (separator, lead, segments) per block
    done: dict[tuple[int, int, int], str] = {}
    cites: dict[tuple[int, int, int], str] = {}
    for i, (text, to) in enumerate(items):
        code = target_code(to)
        if code not in known:
            continue
        shapes[i] = []
        for bi, (sep, lead, body) in enumerate(_blocks(_CITE_AFTER_STOP.sub(r" \3\1\2", text))):
            segs = [seg for para in split_segments(body, MAX_SEG_TOKENS, _bare_count, close_after=_cited)
                    for seg in para]
            shapes[i].append((sep, lead, len(segs)))
            for si, seg in enumerate(segs):
                bare, cites[(i, bi, si)] = _strip_cites(seg)
                if _WORD.search(bare):
                    jobs.append((i, bi, si, code, bare))
                else:
                    done[(i, bi, si)] = bare            # only citations, a rule ("---"): nothing to translate
    got: list[str] = []
    if jobs:
        with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE, timeout=TIMEOUT_S):
            headers = dispatch.level_headers()      # read here: a pool thread carries no level
            with ThreadPoolExecutor(max_workers=min(T5_CONCURRENCY, len(jobs))) as pool:
                got = list(pool.map(lambda j: _t5_completion(j[3], j[4], TIMEOUT_S, headers), jobs))
    failed: set[int] = set()
    for (i, bi, si, _code, seg), text in zip(jobs, got):
        if not text or len(text) > 4 * len(seg) + 64:
            failed.add(i)
        done[(i, bi, si)] = text
    for i, blocks in shapes.items():
        if i in failed:
            continue
        out[i] = "".join(sep + lead + " ".join(_cite(done[(i, bi, si)], cites[(i, bi, si)]) for si in range(n))
                         for bi, (sep, lead, n) in enumerate(blocks))
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
