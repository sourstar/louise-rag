"""An entity's NAME when it has no label in the languages asked nor in `mul`
(operator, 2026-10-24): "As a general rule showing an id is meaningless for
humans; in case there are multiple non English languages use in order:
1. the original name if it can be determined and if it's in latin
characters 2. Any other language in latin characters. 3. The name in the
original language if it can be determined." The order among several
labels in Latin characters, and the language lookup, decided the same day
("Go with your recommendation and the language lookup").

- THE ORIGINAL NAME, in this order:
  - what the entity states as its own name: the monolingual value of native
    label (P1705), name in native language (P1559), official name (P1448),
    title (P1476) or name (P2561);
  - its label in its ORIGINAL LANGUAGE, where the entity states one: the
    language of the work or name (P407), the original language of a film or
    series (P364), a person's native language (P103). Those name a language
    ITEM; its code is the item's Wikimedia language code (P424), read from
    the pack's `language` table (`codes`: item id → code). A place's
    official language (P37, on its country) would determine more —
    written down, not built.
  Preferred statements before normal ones, deprecated never.
- LATIN: every letter of the text is of the Latin script — Unicode's Script
  property (UAX #24), read through the characters' names, since Python's
  `unicodedata` has no Script lookup (the `regex` module's `\\p{Latin}` is
  the library call; it is not in the image). Modifier letters (the ʻokina)
  are neither.
- Among several labels: those in a language of the entity's own wiki pages
  (its sitelinks) first; then French, Spanish, Italian, German, in that
  order; then the order Wikidata lists the labels (the first one given);
  then the code — a pure function of the dump, the same on every peer.
- Last, the general rule: any label at all is a name, an id is not. Only an
  entity with no label and no name statement keeps its id.
"""
from __future__ import annotations

import unicodedata
from typing import Mapping

#: the statements whose monolingual value is the entity's own name, in order
NAME_PROPERTIES = ("P1705", "P1559", "P1448", "P1476", "P2561")
#: the statements naming the entity's original language (a language item), in order
LANGUAGE_PROPERTIES = ("P407", "P364", "P103")
#: a language item's Wikimedia language code
LANGUAGE_CODE = "P424"
#: among labels with no home language: these first, in this order (operator)
PREFERRED = ("fr", "es", "it", "de")
#: the label table's pseudo-language for these names (pack.py `name` rows)
NAME = "*"
#: Wikimedia project suffixes of a sitelink's site id (`frwiki`, `nlwikibooks`)
_PROJECTS = ("wiki", "wikibooks", "wikinews", "wikiquote", "wikisource", "wikiversity", "wikivoyage",
             "wiktionary")


def is_latin(text: str) -> bool:
    letters = [ch for ch in text if unicodedata.category(ch)[0] == "L" and unicodedata.category(ch) != "Lm"]
    return bool(letters) and all("LATIN" in unicodedata.name(ch, "") for ch in letters)


def _statements(obj: dict, pid: str) -> list[dict]:
    """A property's statements, deprecated ones left out, preferred first,
    each rank in the dump's order."""
    claims = obj.get("claims") if isinstance(obj.get("claims"), dict) else {}
    out = [s for s in claims.get(pid) or () if isinstance(s, dict) and s.get("rank") != "deprecated"]
    out.sort(key=lambda s: 0 if s.get("rank") == "preferred" else 1)
    return out


def _value(st: dict):
    snak = st.get("mainsnak") if isinstance(st.get("mainsnak"), dict) else {}
    if snak.get("snaktype", "value") != "value":
        return None
    return (snak.get("datavalue") or {}).get("value")


def language_code_of(obj: dict) -> str | None:
    """The Wikimedia language code a language item states (P424), or None."""
    for st in _statements(obj, LANGUAGE_CODE):
        v = _value(st)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def labels_of(obj: dict) -> list[tuple[str, str]]:
    """(language, label) in the order Wikidata lists them."""
    labels = obj.get("labels") if isinstance(obj.get("labels"), dict) else {}
    return [(lang, v["value"].strip()) for lang, v in labels.items()
            if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip()]


def original_names(obj: dict, codes: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(language, name) the entity's original name can be, in the rule's order."""
    out: list[tuple[str, str]] = []
    for pid in NAME_PROPERTIES:
        for st in _statements(obj, pid):
            v = _value(st)
            if isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip() \
                    and isinstance(v.get("language"), str) and v["language"]:
                out.append((v["language"], v["text"].strip()))
    if codes:
        labels = labels_of(obj)
        for pid in LANGUAGE_PROPERTIES:
            for st in _statements(obj, pid):
                v = _value(st)
                code = codes.get(v.get("id")) if isinstance(v, dict) and isinstance(v.get("id"), str) else None
                if not code:
                    continue
                # the label in that language, else in one of its variants (zh → zh-hans)
                got = next((p for p in labels if p[0] == code), None) or \
                    next((p for p in labels if p[0].startswith(code + "-")), None)
                if got is not None and got not in out:
                    out.append(got)
    return out


def _home_languages(obj: dict) -> set[str]:
    """The languages of the entity's own wiki pages: `frwiki` → fr, `zh_min_nanwiki` → zh-min-nan."""
    sitelinks = obj.get("sitelinks") if isinstance(obj.get("sitelinks"), dict) else {}
    out = set()
    for site in sitelinks:
        for suffix in _PROJECTS:
            if isinstance(site, str) and site.endswith(suffix) and len(site) > len(suffix):
                out.add(site[: -len(suffix)].replace("_", "-"))
                break
    return out


def _choose(pairs: list[tuple[str, str]], home: set[str]) -> tuple[str, str] | None:
    """The entity's own wikis' languages, then PREFERRED, then the listing order."""
    if not pairs:
        return None
    ranked = min(enumerate(pairs), key=lambda ip: (
        0 if ip[1][0] in home else 1,
        PREFERRED.index(ip[1][0]) if ip[1][0] in PREFERRED else len(PREFERRED),
        ip[0], ip[1][0]))
    return ranked[1]


def display_name(obj: dict, codes: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(language, name) by the operator's order; None when the entity has
    no label and no name statement at all. `codes`: language item id →
    Wikimedia language code (the pack's `language` table)."""
    originals = original_names(obj, codes)
    for lang, text in originals:
        if is_latin(text):
            return lang, text                                            # 1
    labels = labels_of(obj)
    home = _home_languages(obj)
    latin = _choose([p for p in labels if is_latin(p[1])], home)
    if latin is not None:
        return latin                                                     # 2
    if originals:
        return originals[0]                                              # 3
    return _choose(labels, home)                                         # the general rule


def has_label(obj: dict, languages: tuple[str, ...]) -> bool:
    """A label in one of `languages`, as the title's chain reads one (source.py)."""
    labels = obj.get("labels") if isinstance(obj.get("labels"), dict) else {}
    return any(isinstance(labels.get(lang), dict) and isinstance(labels[lang].get("value"), str)
               and labels[lang]["value"] for lang in languages)
