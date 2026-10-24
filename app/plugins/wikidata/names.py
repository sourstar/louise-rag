"""An entity's NAME when it has no label in the languages asked nor in `mul`
(operator, 2026-10-24): "As a general rule showing an id is meaningless for
humans; in case there are multiple non English languages use in order:
1. the original name if it can be determined and if it's in latin
characters 2. Any other language in latin characters. 3. The name in the
original language if it can be determined."

- THE ORIGINAL NAME is what the entity states as its own name in its own
  language: the monolingual value of native label (P1705), name in native
  language (P1559), official name (P1448), title (P1476) or name (P2561), in
  that order, preferred statements before normal ones, deprecated never.
  (The language of a work, P407, would determine it for more entities; it
  names a language ITEM, and reading its code — P424 — is a table across
  entities the pack does not keep. Written down, not built.)
- LATIN: every letter of the text is of the Latin script — Unicode's Script
  property (UAX #24), read through the characters' names, since Python's
  `unicodedata` has no Script lookup (the `regex` module's `\\p{Latin}` is
  the library call; it is not in the image). Modifier letters (the ʻokina)
  are neither.
- Among several labels: those in a language of the entity's own wiki pages
  (its sitelinks) first, then the value most languages give, then the
  smallest language code — a pure function of the entity, the same on every
  peer.
- Last, the general rule: any label at all is a name, an id is not. Only an
  entity with no label and no name statement keeps its id.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
#: the statements whose monolingual value is the entity's own name, in order
NAME_PROPERTIES = ("P1705", "P1559", "P1448", "P1476", "P2561")
#: the label table's pseudo-language for these names (pack.py `name` rows)
NAME = "*"
#: Wikimedia project suffixes of a sitelink's site id (`frwiki`, `nlwikibooks`)
_PROJECTS = ("wiki", "wikibooks", "wikinews", "wikiquote", "wikisource", "wikiversity", "wikivoyage",
             "wiktionary")


def is_latin(text: str) -> bool:
    letters = [ch for ch in text if unicodedata.category(ch)[0] == "L" and unicodedata.category(ch) != "Lm"]
    return bool(letters) and all("LATIN" in unicodedata.name(ch, "") for ch in letters)


def original_names(obj: dict) -> list[tuple[str, str]]:
    """(language, text) of the entity's own name statements, in the rule's order."""
    claims = obj.get("claims") if isinstance(obj.get("claims"), dict) else {}
    out: list[tuple[str, str]] = []
    for pid in NAME_PROPERTIES:
        statements = [s for s in claims.get(pid) or () if isinstance(s, dict) and s.get("rank") != "deprecated"]
        statements.sort(key=lambda s: 0 if s.get("rank") == "preferred" else 1)      # stable: dump order
        for st in statements:
            snak = st.get("mainsnak") if isinstance(st.get("mainsnak"), dict) else {}
            if snak.get("snaktype", "value") != "value":
                continue
            v = (snak.get("datavalue") or {}).get("value")
            if isinstance(v, dict) and isinstance(v.get("text"), str) and v["text"].strip() \
                    and isinstance(v.get("language"), str) and v["language"]:
                out.append((v["language"], v["text"].strip()))
    return out


def labels_of(obj: dict) -> list[tuple[str, str]]:
    labels = obj.get("labels") if isinstance(obj.get("labels"), dict) else {}
    return [(lang, v["value"].strip()) for lang, v in sorted(labels.items())
            if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"].strip()]


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
    if not pairs:
        return None
    given = Counter(v for _, v in pairs)
    pool = [p for p in pairs if p[0] in home] or pairs
    return min(pool, key=lambda p: (-given[p[1]], p[0]))


def display_name(obj: dict) -> tuple[str, str] | None:
    """(language, name) by the operator's order; None when the entity has
    no label and no name statement at all."""
    originals = original_names(obj)
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
