"""The node's translation model: TRANSLATE_URL, a MADLAD-400 server on the
GPU (deploy/madlad-translate). HTTP only — the app never loads a model
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

TWO SHAPES, by TRANSLATE_API (app/config.py). "translate" is the server's own:
POST /translate {"items": [...]} and GET /languages. "completion" is the same
two requests under LocalAI (migration step 15e-2): each one, as JSON, is the
prompt of one /v1/completions call, and the completion's text is the server's
JSON answer (deploy/localai/backends/louise-madlad/backend.py). One prompt
carries the whole batch, because LocalAI calls the model once per prompt and
the model translates a batch in one pass (operator, 2026-10-03).
"""
from __future__ import annotations

import json
import threading
import time

import httpx

from . import config, dispatch

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
    model in either shape."""
    if config.TRANSLATE_API == "completion":
        return f"{config.TRANSLATE_URL}/v1/models"
    return f"{config.TRANSLATE_URL}/health"


def _completion(request: dict, timeout: float, headers: dict[str, str] | None = None):
    """One request in the completion shape: `request` as the prompt, the
    answer read back from the completion's text."""
    r = httpx.post(f"{config.TRANSLATE_URL}/v1/completions",
                   json={"prompt": json.dumps(request, ensure_ascii=False)},
                   timeout=timeout, headers=headers or {})
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["text"])


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
        if config.TRANSLATE_API == "completion":
            answer = _completion({"languages": True}, 10.0)
        else:
            r = httpx.get(f"{config.TRANSLATE_URL}/languages", timeout=10.0)
            r.raise_for_status()
            answer = r.json()
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
        with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE,
                           timeout=TIMEOUT_S):
            if config.TRANSLATE_API == "completion":
                answer = _completion(request, TIMEOUT_S, dispatch.level_headers())
            else:
                r = httpx.post(f"{config.TRANSLATE_URL}/translate", json=request,
                               timeout=TIMEOUT_S, headers=dispatch.level_headers())
                r.raise_for_status()
                answer = r.json()
        out = answer.get("translations")
    except Exception:   # noqa: BLE001 — the caller falls back and says so
        return []
    if not isinstance(out, list) or len(out) != len(items):
        return []
    return [o if isinstance(o, str) and o.strip() else None for o in out]
