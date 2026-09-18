"""The language half of a search's plan (docs/05 §5.2, *expand*): which
languages the query goes out in, and which the documents must be written in.

The rule (operator, 2026-09-18) holds for every collection a search
touches: a search covers ALL the languages its collections hold, unless
the query itself limits it to documents in some of them.

- **All the languages.** The query is translated once per search into
  every language the text indexes in scope hold, and each language group
  is searched with its own variant. It used to be once PER COLLECTION,
  into the six largest groups, never English: an Italian query ran
  untranslated against gutenberg's English index and all of PubMed, and
  came back with "Libro della caccia" and "Balena's method" (live,
  2026-09-18). "cjk" is one index holding three languages, so it gets
  three variants and a record ranks by its best (§5.2: a variant is a
  query, not a leg). "other" is a catch-all with no one language, so it
  is searched with the query as typed; its forty-odd languages hold
  about 500 of gutenberg's 80,000 books, and translating into them too
  measured 34.3 s a search against 9.2 s.
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

    def searches(self, group: str) -> bool:
        """Whether the text index group `group` may hold a wanted record."""
        return not self.only or any(fts.group_for(c) == group for c in self.only)

    def lang_filter(self, group: str) -> list[str] | None:
        """The languages to keep inside a MIXED group; None where the group
        is one language, and so already the filter."""
        if not self.only or group not in ("cjk", "other"):
            return None
        return [c for c in self.only if fts.group_for(c) == group]

    def variants(self, group: str) -> list[str]:
        """The query texts to run against `group`, one per language."""
        if group == "cjk":
            langs = [c for c in (self.only or sorted(config.CJK_LANGS))
                     if fts.group_for(c) == "cjk"]
        elif group == "other":
            langs = [c for c in self.only if fts.group_for(c) == "other"]
        else:
            langs = [group]
        out: list[str] = []
        for c in langs:
            t = self.translations.get(c)
            if t and t not in out:
                out.append(t)
        return out or [self.core]

    def summary(self) -> dict:
        return {"core": self.core, "only": list(self.only), "status": self.status}


def reset() -> None:
    with _cache_lock:
        _cache.clear()


def held_languages(groups) -> set[str]:
    """The languages a set of text index groups is known to hold."""
    out: set[str] = set()
    for g in groups:
        if g == "cjk":
            out |= config.CJK_LANGS
        elif g != "other":
            out.add(g)
    return out


def explicit(query: str) -> tuple[list[str], str] | None:
    m = _EXPLICIT.search(query)
    if m is None:
        return None
    codes = sorted({c.lower() for c in m.group(1).split(",")})
    return codes, llm.strip_instruction(query, m.start(), m.end())


def plan(query: str, collections, translate: bool) -> LanguagePlan:
    """ONE plan per search, shared by every collection in it. Cached per
    (query, languages held), so the Ask page's second search — the answer's
    — costs no model call and cannot restrict differently from the first."""
    if not translate:
        return LanguagePlan(core=query)
    held = set()
    for c in collections:
        held |= held_languages(fts.existing_groups(c))
    key = (query, tuple(sorted(held)))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    status = "ok"
    found = explicit(query)
    if found is None:
        found = llm.language_restriction(query)
        if found is None:
            status, found = "unavailable", ([], query)
    only, core = found
    targets = sorted(only) if only else sorted(held)
    translations = llm.translate_query(core, targets) if targets else {}
    if targets and not translations:
        status = "unavailable"
    result = LanguagePlan(core=core or query, only=tuple(only),
                          translations=translations, status=status)
    if status == "ok":
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = result
    return result
