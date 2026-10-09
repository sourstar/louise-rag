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
"""
from __future__ import annotations

import json
import threading
import time

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
    model_load.ensure_loaded(
        config.TRANSLATE_URL, config.TRANSLATE_MODEL,
        lambda bound_s: _completion({"items": [{"text": "OK", "to": "it"}]}, bound_s,
                                    {dispatch.LEVEL_HEADER: "translate"}),
        caller="translation")


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
        answer = _completion({"languages": True}, 10.0)
        got = frozenset(str(c) for c in answer.get("languages", []))
    except Exception:   # noqa: BLE001 — unreachable is "no languages", not an error
        return frozenset()
    with _lock:
        _languages = (time.monotonic(), got)
    return got


def reset() -> None:
    global _languages
    with _lock:
        _languages = None


def translate(items: list[tuple[str, str]]) -> list[str | None]:
    """(text, target code) pairs -> one translation per pair, None where the
    model knows no such target; [] when the call failed as a whole."""
    if not items or not available():
        return []
    request = {"items": [{"text": t, "to": to} for t, to in items]}
    try:
        warm()
        with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE,
                           timeout=TIMEOUT_S):
            answer = _completion(request, TIMEOUT_S, dispatch.level_headers())
        out = answer.get("translations")
    except Exception:   # noqa: BLE001 — the caller falls back and says so
        return []
    if not isinstance(out, list) or len(out) != len(items):
        return []
    return [o if isinstance(o, str) and o.strip() else None for o in out]
