"""One work, one hit — unless the reader asked about the translations.

THE PROBLEM (operator, 2026-09-12). Gutenberg holds a work and its
translations as separate books, so a search for a famous novel returns
it five times in five languages and the rest of the corpus is pushed off
the page. "they just saturate the results returned."

THE RULE:
  - if the corpus has the ORIGINAL-language version, the translations do
    not compete with it;
  - if it does not, the ENGLISH translation is the best alternative;
  - unless the query is ABOUT the translations, in which case they are
    exactly what was asked for.

WHAT MAKES IT SAFE. Only a book the ORIGIN marks as a translation — one
carrying a `trl` (translator) credit — is ever suppressed. That matters
because the grouping key is imperfect: measured over ~6,000 real books,
(author, LCSH set) groups genuine translation sets correctly ("Titus
Andronicus" nl with "Titus Andronicus" en) but also lumps fifteen issues
of Chambers's Journal into one "work", because a periodical's issues
share an author and a subject heading. Every one of those issues is an
original, so requiring an explicit translator credit leaves them all
standing. The weak key can only ever hide something the origin itself
called a translation.

`trl` appears on ~10% of this corpus (302 of 3,000 sampled RDFs), and
Project Gutenberg publishes NO link from a translation to its original —
no `dcterms:relation`, `isVersionOf` or `source` anywhere — which is why
the work key has to be inferred at all.
"""
from __future__ import annotations

import re

#: "translation" and its neighbours, in the languages the result picker
#: offers (app/web.py RESULT_LANGUAGES). A term list rather than a model
#: call: it costs nothing, it is predictable, and an operator can read
#: why a query did or did not count as being about translations.
_TRANSLATION_WORDS = {
    # en, de, fr, it, es, pt, nl, pl, ru, ja, zh, tr, fa
    "translation", "translations", "translated", "translating", "translator",
    "translators", "untranslated", "retranslation",
    "übersetzung", "übersetzungen", "übersetzt", "übersetzer",
    "traduction", "traductions", "traduit", "traduite", "traducteur",
    "traduzione", "traduzioni", "tradotto", "tradotta", "traduttore",
    "traducción", "traducciones", "traducido", "traducida", "traductor",
    "tradução", "traduções", "traduzido", "traduzida", "tradutor",
    "vertaling", "vertalingen", "vertaald", "vertaler",
    "tłumaczenie", "tłumaczenia", "przekład", "tłumacz",
    "перевод", "переводы", "переведён", "переведен", "переводчик",
    "翻訳", "訳文", "訳者",
    "翻译", "译本", "译者",
    "çeviri", "çevirisi", "çevrilmiş", "çevirmen",
    "ترجمه", "ترجمه‌ها", "مترجم",
}

#: CJK scripts do not put spaces between words, so a word-boundary split
#: never finds 翻訳 inside a sentence. Those terms are matched as
#: substrings instead; the Latin/Cyrillic ones keep boundaries so
#: "translationese" or a name does not trip the rule.
_SUBSTRING_TERMS = tuple(t for t in _TRANSLATION_WORDS
                         if not re.search(r"[a-zà-ÿа-яё]", t.lower()))
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def mentions_translation(query: str) -> bool:
    """Is the reader asking ABOUT the translations?

    True for the operator's own examples — "is this book translated in
    this language", "how is the main character called in the various
    translations of the book", "is the fidelity of the original text
    kept in the translation in this language" — and false for a query
    that merely happens to be written in another language, which is the
    trigger that would misfire on every short foreign query.
    """
    if not query:
        return False
    low = query.lower()
    if any(term in low for term in _SUBSTRING_TERMS):
        return True
    return any(w in _TRANSLATION_WORDS for w in _WORD_RE.findall(low))


#: language NAMES to codes, for "French translation of …". Both the
#: English name and the endonym, because a reader asks in either — and
#: deliberately NOT every language's adjective for every other language
#: ("francese", "französisch", …), which is a 13x13 matrix of forms this
#: does not pretend to cover. A miss there costs nothing: the query still
#: counts as being about translations, so nothing is suppressed and the
#: reader sees them all. Only the NARROWING is missed, never the books.
_LANGUAGE_NAMES = {
    "english": "en", "german": "de", "deutsch": "de", "french": "fr",
    "français": "fr", "francais": "fr", "italian": "it", "italiano": "it",
    "spanish": "es", "español": "es", "espanol": "es", "portuguese": "pt",
    "português": "pt", "portugues": "pt", "dutch": "nl", "nederlands": "nl",
    "polish": "pl", "polski": "pl", "russian": "ru", "русский": "ru",
    "japanese": "ja", "日本語": "ja", "chinese": "zh", "中文": "zh",
    "turkish": "tr", "türkçe": "tr", "turkce": "tr", "persian": "fa",
    "farsi": "fa", "فارسی": "fa", "greek": "el", "latin": "la",
    "swedish": "sv", "danish": "da", "norwegian": "no", "finnish": "fi",
    "czech": "cs", "hungarian": "hu", "romanian": "ro", "catalan": "ca",
}


def named_language(query: str) -> str | None:
    """The language a query names, if it names exactly one.

    "French translation of Madame Bovary" wants the French one, not every
    translation there is. Two named languages mean a comparison ("the
    French and German translations"), so neither is preferred and the
    reader gets both.
    """
    if not query:
        return None
    low = query.lower()
    # A SHORT INFLECTIONAL TAIL, because adjectives inflect: "traduzione
    # italiana" and "die deutsche Übersetzung" are how people actually
    # write it, while the table holds "italiano" and "deutsch". Up to
    # three trailing letters, so "italian|a|o", "deutsch|e|en",
    # "français|e". Bounded rather than open so "latin" does not swallow
    # "latinamerican".
    #
    # It does over-match — "polish" is also an English verb — and that
    # costs nothing here, because narrowing only happens when the query
    # ALSO says "translation". "polish the silver" names no translation
    # and reaches none of this.
    found = {code for name, code in _LANGUAGE_NAMES.items()
             if re.search(rf"(?<![^\W\d_]){re.escape(name)}[^\W\d_]{{0,3}}(?![^\W\d_])",
                          low)}
    return found.pop() if len(found) == 1 else None


def work_key(hit: dict) -> tuple | None:
    """What makes two books the same work, as well as can be inferred.

    (author agent id, the LCSH heading set). None when either is missing,
    and a hit with no key is never grouped and so never suppressed.
    """
    author = hit.get("work_author") or ""
    subjects = hit.get("work_subjects") or ()
    if not author or not subjects:
        return None
    return (author, frozenset(subjects))


def prefer_originals(hits: list[dict], query: str = "",
                     *, english: str = "en") -> list[dict]:
    """Drop translations that compete with something better, in place of
    nothing — the order and every surviving hit are untouched.

    A hit needs `work_author`, `work_subjects`, `translated` and `lang`
    to take part; one missing any of them is passed through, because a
    guess about what a book IS must never cost a reader the book.
    """
    if not hits:
        return list(hits)
    asked_about = mentions_translation(query)
    wanted = named_language(query) if asked_about else None
    if asked_about and wanted is None:
        # "the various translations of the book" — no language named, so
        # every one of them is the answer
        return list(hits)

    groups: dict[tuple, list[dict]] = {}
    for h in hits:
        key = work_key(h)
        if key is not None:
            groups.setdefault(key, []).append(h)

    drop: set[int] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        translations = [m for m in members if m.get("translated")]
        if not translations:
            continue                      # every issue of a periodical
        originals = [m for m in members if not m.get("translated")]
        if wanted is not None:
            # "French translation of Madame Bovary": that one, and the
            # original it is a translation OF, which is the thing being
            # asked about. The other twelve languages are the noise the
            # rule exists to remove, even here.
            keep = [m for m in translations if (m.get("lang") or "") == wanted]
        elif originals:
            keep: list[dict] = []         # the original wins outright
        else:
            # no original held: the English translation is the best
            # alternative, and if there is none nothing is suppressed —
            # a reader is left with what exists rather than with nothing
            keep = [m for m in translations if (m.get("lang") or "") == english][:1]
            if not keep:
                continue
        for m in translations:
            if m not in keep:
                drop.add(id(m))
    return [h for h in hits if id(h) not in drop]
