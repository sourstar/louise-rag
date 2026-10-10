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
  2026-09-18; amended 2026-10-10). A search goes out in each collection's
  MAIN languages — its one-language index groups, largest first, until
  they hold `[ask] translate_share` of its documents (`main_of`), plus
  CJK's three — and "other" is searched as typed. The other one-language
  groups are not searched by the text leg at all, unless the query is
  written in their language (as typed, below): the vector leg covers them.
  Measured offline (docs/notes/one-language-per-work.md, "Do we need query
  translation?"): translating into gutenberg's en, fr, fi, de alone beat
  all nineteen — this rule, re-scored: descriptive MRR 0.231 → 0.245,
  R@50 0.605 → 0.630 (+74/−33), titles R@10 0.769 → 0.821, R@50 0.897 →
  0.949 — and an index searched as typed in
  place of a translation always lost. Every language held, "other"'s
  forty-odd too, only when asked: the Ask page's "all languages",
  `all_languages` on the API, `lang:all` in the question. A restriction
  (`lang:hu`) translates into what it names, main or not.
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
import time
from dataclasses import dataclass, field

from . import config, fts, llm, query_language
from . import translate as mt

#: `lang:de` / `lang:de,fr` — the form that cannot be misread
_EXPLICIT = re.compile(r"(?<!\S)lang:([a-z]{2,3}(?:,[a-z]{2,3})*)(?!\S)", re.I)

_cache: dict[tuple, "LanguagePlan"] = {}
_cache_lock = threading.Lock()
_CACHE_MAX = 256

#: the mixed text index groups: many languages each, never a main language
MIXED = ("cjk", "other")
#: (fts_dir, groups, share) -> (computed at, meta mtimes, main languages)
_main_cache: dict[tuple, tuple[float, tuple, tuple[str, ...]]] = {}
#: a GROWING index (the worker committing) is recounted at most this often;
#: an index that has not changed is never recounted
_MAIN_TTL_S = 300.0


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
    #: who named `query_lang`: "reading" (the chat model) · "guess" (no model,
    #: app/query_language.py, when the reading is unavailable or names none)
    #: · "" (nobody)
    query_lang_by: str = ""
    #: the main languages translated into (05 §5.2, amended 2026-10-10),
    #: largest first, the union over the search's collections; () when the
    #: search is not narrowed to them (translation off, a restriction, every
    #: language asked for)
    main: tuple[str, ...] = ()
    #: str(collection.fts_dir) -> that collection's main languages; a
    #: collection absent here has its every index searched, as before
    main_by: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def main_for(self, collection) -> tuple[str, ...] | None:
        """The one-language groups the text leg searches in `collection`
        besides the query's own; None = every one of them."""
        return self.main_by.get(str(collection.fts_dir))

    def searches(self, group: str) -> bool:
        """Whether the text index group `group` may hold a wanted record."""
        return not self.only or any(fts.group_for(c) == group for c in self.only)

    def lang_filter(self, group: str) -> list[str] | None:
        """The languages to keep inside a MIXED group; None where the group
        is one language, and so already the filter."""
        if not self.only or group not in ("cjk", "other"):
            return None
        return [c for c in self.only if fts.group_for(c) == group]

    def variants(self, group: str, held: list[str] | None = None,
                 main: tuple[str, ...] | None = None) -> list[str]:
        """The query texts to run against `group`, one per language —
        `held` being the languages that group's index holds. A mixed group
        with a language no translation reached is also searched as typed.

        `main` (`main_for`): a one-language group that is neither main nor
        the query's own language gets NO variant — the text leg does not
        search it (05 §5.2, amended 2026-10-10: searched as typed it brought
        another stemmer's noise into the leg's top positions, and its
        translation the same). The mixed groups are searched as before.

        THE QUERY'S OWN LANGUAGE IS SEARCHED AS TYPED (amended 2026-10-10,
        05 §5.2): its group gets the reader's words, never a translation
        into the language they are already in — MADLAD rewrote 292 of 472
        measured queries into their own language ("Dumas novel about the
        diamond necklace scandal…" → "The Diamond Necklace Scandal at the
        Court of Louis XVI…"). A mixed group gets it the same way, its
        language having no translation."""
        if self.query_lang and group == self.query_lang and group not in MIXED:
            return [self.core]
        if main is not None and group not in MIXED and group not in main:
            return []
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
        _main_cache.clear()
    llm.reset_readings()


def main_of(counts: dict[str, int], share: float) -> tuple[str, ...]:
    """05 §5.2 (amended 2026-10-10): a collection's MAIN languages — walking
    its text index groups largest first (ties by name), the one-language
    groups of the shortest walk that holds `share` of its documents. A mixed
    group counts in the walk (its documents are the collection's) and is
    never named: it is searched as before. With no count at all, every
    one-language group — nothing is narrowed on a guess."""
    total = sum(max(0, n) for n in counts.values())
    if total <= 0:
        return tuple(sorted(g for g in counts if g not in MIXED))
    out: list[str] = []
    acc = 0
    for g, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if n <= 0 or acc / total >= share - 1e-12:
            break
        acc += n
        if g not in MIXED:
            out.append(g)
    return tuple(out)


def translate_share() -> float:
    """`[ask] translate_share` (07 §7.8), re-read like every Tier-2 key; a
    broken platform.toml leaves the default rather than failing a search."""
    from . import platform_config
    default = platform_config.AskConfig().translate_share
    try:
        return float(getattr(platform_config.load().ask, "translate_share", default))
    except Exception:   # noqa: BLE001
        return default


def main_languages(collection, share: float | None = None) -> tuple[str, ...]:
    """`main_of` for one collection, counted from its own indexes
    (`fts.group_documents`: the distinct documents of each) — never a scan
    of the manifest. Cached on the indexes' path and meta.json mtimes; a
    changed index is recounted at most every `_MAIN_TTL_S`, so a search does
    not recount an index the worker is still writing on every query."""
    share = translate_share() if share is None else share
    groups = tuple(fts.existing_groups(collection))
    key = (str(collection.fts_dir), groups, share)
    mtimes = []
    for g in groups:
        try:
            mtimes.append((collection.fts_dir / g / "meta.json").stat().st_mtime_ns)
        except OSError:
            mtimes.append(0)
    stamp = tuple(mtimes)
    now = time.monotonic()
    with _cache_lock:
        hit = _main_cache.get(key)
    if hit is not None and (hit[1] == stamp or now - hit[0] < _MAIN_TTL_S):
        return hit[2]
    try:
        counts = fts.group_documents(collection)
    except Exception:   # noqa: BLE001 — no count narrows nothing
        counts = {}
    main = main_of({g: counts.get(g, 0) for g in groups}, share)
    with _cache_lock:
        if len(_main_cache) >= _CACHE_MAX:
            _main_cache.clear()
        _main_cache[key] = (now, stamp, main)
    return main


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


def _guess_pool(collections, held: set[str]) -> set[str]:
    """The languages a query-language guess may name: every language the
    collections' text indexes hold, the mixed groups' included. An index
    that cannot say leaves the ones known by name."""
    pool = set(held)
    for c in collections:
        try:
            pool |= held_languages(fts.existing_groups(c)) | held_everywhere(c)
        except Exception:   # noqa: BLE001 — a guess is never worth a failed search
            pass
    return pool


def _query_lang(reading, text: str, collections, held: set[str]) -> tuple[str | None, str]:
    """05 §5.2 (amended 2026-10-10): the language the query is written in —
    the reading's when it names one, else the deterministic guess (the
    reading needs the chat model, which the node's runner evicts: without
    it the work was shown in the query's language for 0.59 of found
    queries, 0.79 with the guess, 0.83 with the reading;
    docs/notes/one-language-per-work.md)."""
    lang = getattr(reading, "lang", None)
    if lang:
        return lang, "reading"
    lang = query_language.guess(text, _guess_pool(collections, held))
    return lang, ("guess" if lang else "")


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
    not only the main ones (`main_languages`)."""
    if not translate:
        lang, by = _query_lang(None, query, collections, set())
        return LanguagePlan(core=query, query_lang=lang, query_lang_by=by)
    found = explicit(query)
    if found is not None and found[0] == ["all"]:
        every, query_core, found = True, found[1], None
    else:
        query_core = query
    held: set[str] = set()
    main_by: dict[str, tuple[str, ...]] = {}
    share = None if every else translate_share()
    for c in collections:
        groups = fts.existing_groups(c)
        if every:
            held |= held_languages(groups) | held_everywhere(c)
            continue
        # 05 §5.2 (amended 2026-10-10): its main languages, and CJK's three
        m = main_languages(c, share)
        path = str(c.fts_dir)
        main_by[path] = tuple(dict.fromkeys(main_by.get(path, ()) + m))
        held |= set(m) | held_languages(g for g in groups if g == "cjk")
    key = (query, every, tuple(sorted(held)), tuple(sorted(main_by.items())))
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    status = "ok"
    if found is None:
        found = llm.language_restriction(query_core)
        if found is None:
            status, found = "unavailable", ([], query_core)
    only, core = found
    query_lang, query_lang_by = _query_lang(found, core or query_core, collections, held)
    if only:
        # a restriction names its languages, main or not, and alone narrows
        main_by = {}
    main = tuple(dict.fromkeys(lang for m in main_by.values() for lang in m))
    targets = sorted(only) if only else sorted(held)
    # the query's own language is searched as typed, never translated into
    # (05 §5.2, amended 2026-10-10; `LanguagePlan.variants`)
    ask = [t for t in targets if t != query_lang]
    translations, by = _translations(core, ask) if ask else ({}, "")
    if ask and not translations:
        status = "unavailable"
    result = LanguagePlan(core=core or query_core, only=tuple(only),
                          translations=translations, status=status, by=by, every=every,
                          query_lang=query_lang, query_lang_by=query_lang_by,
                          main=main, main_by=main_by)
    if status == "ok":
        with _cache_lock:
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
            _cache[key] = result
    return result
