"""The language half of a search's plan (docs/05 §5.2, *expand*): which
languages the query goes out in, and which the documents must be written in.

The rule (operator, 2026-09-18) holds for every collection a search
touches: a search covers the main languages its collections hold — every
language on request — unless the query itself limits it to documents in
some of them.

- **Translated once per search.** The query is translated once into the
  languages it goes out in, and each language group is searched with its
  own variant. It used to be once PER COLLECTION,
  into the six largest groups, never English: an Italian query ran
  untranslated against gutenberg's English index and all of PubMed, and
  came back with "Libro della caccia" and "Balena's method" (live,
  2026-09-18). "cjk" and "other" are MIXED indexes, so they get one
  variant per language they hold (`fts.group_languages`) and a record
  ranks by its best (§5.2: a variant is a query, not a leg).
- **The main languages by default, all of them on request** (operator,
  2026-09-18). A search goes out in the MAIN languages — those with an
  index of their own, and CJK's three — and "other" is searched as typed.
  Every language held, "other"'s forty-odd too, only when asked: the Ask
  page's "all languages", `all_languages` on the API, `lang:all` in the
  question.
- **By the faster model.** The translation model (TRANSLATE_URL,
  app/translate.py: MADLAD-400 on the GPU) whenever the node has one; the
  chat model otherwise, or when it fails. Measured on the node for the 19
  main languages: 0.76–1.75 s against 56–192 s with the GPU busy, and the
  chat model made the white whale a white shark.
- **Unless the query says otherwise.** `lang:de` (or `lang:de,fr`) in the
  query restricts it with no model call. Otherwise
  `llm.language_restriction` looks for an instruction such as "solo testi
  in tedesco". A restriction is a FILTER (§5.2: a filter restricts,
  nothing boosts): both legs and the catalog see only records in those
  languages, and the page names it.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

from . import config, fts, llm
from . import translate as mt

#: `lang:de` / `lang:de,fr` — the form that cannot be misread
_EXPLICIT = re.compile(r"(?<!\S)lang:([a-z]{2,3}(?:,[a-z]{2,3})*)(?!\S)", re.I)

_cache: dict[tuple, "LanguagePlan"] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 256


@dataclass
class LanguagePlan:
    #: the query without its language instruction — what every leg ranks by
    core: str
    #: ISO codes the documents must be written in; () = every language
    only: tuple[str, ...] = ()
    #: code -> `core` in that language
    translations: dict[str, str] = field(default_factory=dict)
    #: off (not asked for) · ok · unavailable (the model could not be asked:
    #: every language searched with the query as typed)
    status: str = "off"
    #: who translated: "translation model" · "chat model" · "" (nobody)
    by: str = ""
    #: went out in every language held, not only the main ones (on request)
    every: bool = False
    #: the language the query is written in, as the chat model read it with
    #: the restriction (`llm.Reading.lang`); None when nobody said — one hit
    #: per work shows a work in it (app/ask/works.py)
    query_lang: str | None = None

    def searches(self, group: str) -> bool:
        """Whether the text index group `group` may hold a wanted record."""
        return not self.only or any(fts.group_for(c) == group for c in self.only)

    def lang_filter(self, group: str) -> list[str] | None:
        """The languages to keep inside a MIXED group; None where the group
        is one language, and so already the filter."""
        if not self.only or group not in ("cjk", "other"):
            return None
        return [c for c in self.only if fts.group_for(c) == group]

    def variants(self, group: str, held: list[str] | None = None) -> list[str]:
        """The query texts to run against `group`, one per language —
        `held` being the languages that group's index holds. A mixed group
        with a language no translation reached is also searched as typed."""
        if group in ("cjk", "other"):
            pool = self.only or held or (sorted(config.CJK_LANGS) if group == "cjk" else [])
            langs = [c for c in pool if fts.group_for(c) == group]
        else:
            langs = [group]
        out: list[str] = []
        for c in langs:
            t = self.translations.get(c)
            if t and t not in out:
                out.append(t)
        if group in ("cjk", "other") and any(c not in self.translations for c in langs):
            if self.core not in out:
                out.append(self.core)
        return out or [self.core]

    def summary(self) -> dict:
        return {"core": self.core, "only": list(self.only), "status": self.status,
                "by": self.by, "every": self.every}


def reset() -> None:
    with _cache_lock:
        _cache.clear()
    llm.reset_readings()


def held_languages(groups) -> set[str]:
    """The languages a set of text index groups is known to hold by their
    names alone: every one-language group, and CJK's three."""
    out: set[str] = set()
    for g in groups:
        if g == "cjk":
            out |= config.CJK_LANGS
        elif g != "other":
            out.add(g)
    return out


def held_everywhere(collection) -> set[str]:
    """Every language the collection's text indexes hold, the mixed groups'
    included — what a search asked for "all languages" goes out in."""
    out: set[str] = set()
    for g in fts.existing_groups(collection):
        out |= set(fts.group_languages(g, collection))
    return out


def _translations(core: str, targets: list[str]) -> tuple[dict[str, str], str]:
    """`core` in each target language: the translation model, every one it
    knows in one batch; the chat model if that fails or is not there."""
    if mt.available():
        known = mt.languages()
        ask = [t for t in targets if t in known]
        got = mt.translate([(core, t) for t in ask]) if ask else []
        if got:
            return {t: v for t, v in zip(ask, got) if v}, "translation model"
    got = llm.translate_query(core, targets)
    return got, ("chat model" if got else "")


def explicit(query: str) -> tuple[list[str], str] | None:
    m = _EXPLICIT.search(query)
    if m is None:
        return None
    codes = sorted({c.lower() for c in m.group(1).split(",")})
    return codes, llm.strip_instruction(query, m.start(), m.end())


def plan(query: str, collections, translate: bool, every: bool = False) -> LanguagePlan:
    """ONE plan per search, shared by every collection in it. Cached per
    (query, languages), so the Ask page's second search — the answer's —
    costs no model call and cannot restrict differently from the first.
    `every` (or `lang:all` in the query) goes out in every language held,
    not only the main ones."""
    if not translate:
        return LanguagePlan(core=query)
    found = explicit(query)
    if found is not None and found[0] == ["all"]:
        every, query_core, found = True, found[1], None
    else:
        query_core = query
    held: set[str] = set()
    for c in collections:
        held |= held_languages(fts.existing_groups(c))
        if every:
            held |= held_everywhere(c)
    key = (query, every, tuple(sorted(held)))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    status = "ok"
    if found is None:
        found = llm.language_restriction(query_core)
        if found is None:
            status, found = "unavailable", ([], query_core)
    query_lang = getattr(found, "lang", None)
    only, core = found
    targets = sorted(only) if only else sorted(held)
    translations, by = _translations(core, targets) if targets else ({}, "")
    if targets and not translations:
        status = "unavailable"
    result = LanguagePlan(core=core or query_core, only=tuple(only),
                          translations=translations, status=status, by=by, every=every,
                          query_lang=query_lang)
    if status == "ok":
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = result
    return result
