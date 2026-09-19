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
"""
from __future__ import annotations

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
        r = httpx.get(f"{config.TRANSLATE_URL}/languages", timeout=10.0)
        r.raise_for_status()
        got = frozenset(str(c) for c in r.json().get("languages", []))
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
    try:
        with dispatch.call(config.TRANSLATE_URL, dispatch.Level.TRANSLATE,
                           timeout=TIMEOUT_S):
            r = httpx.post(f"{config.TRANSLATE_URL}/translate",
                           json={"items": [{"text": t, "to": to} for t, to in items]},
                           timeout=TIMEOUT_S, headers=dispatch.level_headers())
        r.raise_for_status()
        out = r.json().get("translations")
    except Exception:   # noqa: BLE001 — the caller falls back and says so
        return []
    if not isinstance(out, list) or len(out) != len(items):
        return []
    return [o if isinstance(o, str) and o.strip() else None for o in out]
