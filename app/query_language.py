"""The language a query is written in, guessed without a model (05 §5.2,
amended 2026-10-10).

The reading (`llm.language_restriction`) names the query's language, and a
work's one hit is shown in it (05 §5.3). It needs the chat model, which the
node's runner evicts when the card is busy: measured live on 906f06b, with
the reading unavailable the reader saw the work in the query's language for
0.59 of the found queries, against 0.72 before one hit per work and 0.85
with the reading. So when the reading is unavailable, or names no language,
this guess stands in for it — deterministic, no dependency, no network:

  * a script that one held language writes alone answers at once — Greek
    for el, Cyrillic for ru, kana for ja, Hangul for ko, Han for zh, Arabic,
    Hebrew;
  * otherwise each word of the query that is a frequent function word of a
    held language (`app/lang_words/<code>.txt`, small lists written from
    general knowledge) counts for that language, a word shared by n
    languages 1/n to each, and letters only some languages write (ñ, ß, ø,
    ő, ă, ł, ĉ …) add half a point, shared the same way;
  * the best score wins when it is at least `MIN_SCORE` and clear of the
    second by `MARGIN`; else there is no guess (None), and the work is
    shown in English as before.

Measured on the 490 labelled queries of docs/notes/one-language-per-work.md
(tools/measure/one_language_guess.py): see that note's "Fixed (2026-10-10)".
A guess only orders ties and picks the edition shown; it never restricts or
boosts (05 §5.2).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable

WORDS_DIR = Path(__file__).with_name("lang_words")
MIN_SCORE = 1.0
MARGIN = 0.5

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

#: scripts written by one language among those a collection may hold
_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("GREEK", "el"), ("CYRILLIC", "ru"), ("HIRAGANA", "ja"), ("KATAKANA", "ja"),
    ("HANGUL", "ko"), ("CJK", "zh"), ("ARABIC", "ar"), ("HEBREW", "he"),
)

#: letters that only some languages write
_LETTERS: dict[str, tuple[str, ...]] = {
    "ß": ("de",), "ñ": ("es",), "¿": ("es",), "¡": ("es",),
    "ã": ("pt",), "õ": ("pt",), "ç": ("fr", "pt"), "œ": ("fr",),
    "è": ("fr", "it"), "ê": ("fr", "pt"), "ù": ("fr", "it"), "ì": ("it",), "ò": ("it",),
    "ë": ("fr", "nl"), "ï": ("fr", "nl"),
    "å": ("sv", "da", "no"), "æ": ("da", "no"), "ø": ("da", "no"),
    "ä": ("de", "fi", "sv"), "ö": ("de", "fi", "sv", "hu"), "ü": ("de", "hu"),
    "ő": ("hu",), "ű": ("hu",),
    "ă": ("ro",), "ș": ("ro",), "ş": ("ro",), "ț": ("ro",), "ţ": ("ro",),
    "ł": ("pl",), "ą": ("pl",), "ę": ("pl",), "ś": ("pl",), "ź": ("pl",), "ż": ("pl",),
    "ń": ("pl",), "ć": ("pl",),
    "ĉ": ("eo",), "ĝ": ("eo",), "ĥ": ("eo",), "ĵ": ("eo",), "ŝ": ("eo",), "ŭ": ("eo",),
}


@lru_cache(maxsize=1)
def word_lists() -> dict[str, frozenset[str]]:
    """Every shipped list: language code -> its words."""
    out: dict[str, frozenset[str]] = {}
    for f in sorted(WORDS_DIR.glob("*.txt")):
        words = [ln.strip().lower() for ln in f.read_text("utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")]
        out[f.stem] = frozenset(words)
    return out


def _script_language(text: str, held: set[str]) -> str | None:
    counts: dict[str, int] = {}
    kana = False
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script, lang in _SCRIPTS:
            if name.startswith(script):
                counts[lang] = counts.get(lang, 0) + 1
                kana = kana or lang == "ja"
                break
        else:
            counts["latin"] = counts.get("latin", 0) + 1
    if kana:
        counts.pop("zh", None)      # Japanese writes Han beside its kana
    if not counts:
        return None
    lang, n = max(counts.items(), key=lambda kv: kv[1])
    if lang == "latin" or n * 2 < sum(counts.values()):
        return None
    return lang if lang in held else None


def scores(text: str, held: Iterable[str]) -> dict[str, float]:
    """Each held language's evidence in `text` (words and letters)."""
    lists = {c: w for c, w in word_lists().items() if c in set(held)}
    out = {c: 0.0 for c in lists}
    if not lists:
        return out
    low = text.lower()
    for word in _WORD.findall(low):
        has = [c for c, w in lists.items() if word in w]
        for c in has:
            out[c] += 1.0 / len(has)
    for ch, langs in _LETTERS.items():
        n = low.count(ch)
        has = [c for c in langs if c in lists]
        if n and has:
            for c in has:
                out[c] += 0.5 * n / len(has)
    return out


def guess(text: str, held: Iterable[str]) -> str | None:
    """The language `text` is written in, among `held`; None when the text
    does not say clearly enough (a bare title, a name)."""
    held = {c.lower() for c in held}
    if not text or not held:
        return None
    by_script = _script_language(text, held)
    if by_script is not None:
        return by_script
    ranked = sorted(scores(text, held).items(), key=lambda kv: (-kv[1], kv[0]))
    if not ranked or ranked[0][1] < MIN_SCORE:
        return None
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MARGIN:
        return None
    return ranked[0][0]
