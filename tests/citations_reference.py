"""The citation scan of app/refs/normattiva.py as it was before its fast
path (commit 7f0b250), copied verbatim — TEST-ONLY. tests/test_citations_fast_path.py
compares `app.refs.normattiva.citations` with this on every text it has: the
fast path must give the same answer, byte for byte, on every input.

Never edit the copied lines below: they are the reference.
"""
# ruff: noqa: FURB167 — the code as it was
import re

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(fragment: str | None) -> str:
    return _WS.sub(" ", _TAGS.sub("", fragment or "")).strip()


#: how a citation names an act's type → the URN:NIR type token
_TYPES: tuple[tuple[str, str], ...] = (
    (r"legge\s+costituzionale|l\.\s*cost\.", "legge.costituzionale"),
    (r"decreto\s+legislativo|d\.\s*lgs\.?|dlgs|d\.\s*lg\.", "decreto.legislativo"),
    (r"decreto[-\s]legge|d\.\s*l\.|\bdl\b", "decreto.legge"),
    (r"decreto\s+del\s+presidente\s+della\s+repubblica|d\.\s*p\.\s*r\.|\bdpr\b",
     "decreto.del.presidente.della.repubblica"),
    (r"decreto\s+del\s+presidente\s+del\s+consiglio\s+dei\s+ministri|d\.\s*p\.\s*c\.\s*m\.|\bdpcm\b",
     "decreto.del.presidente.del.consiglio.dei.ministri"),
    (r"regio\s+decreto[-\s]legge|r\.\s*d\.\s*l\.", "regio.decreto.legge"),
    (r"regio\s+decreto|r\.\s*d\.", "regio.decreto"),
    # a consolidated text named by what it is, not by its legal form: the
    # form is the origin's to say ("testo unico … 13 novembre 1919, n. 2205"
    # is a regio decreto-legge), found by date and number (`resolve`)
    (r"testo\s+unico|t\.\s*u\.", "*"),
    (r"legge|\bl\.", "legge"),
)
_TYPE_RE = "|".join(f"(?:{p})" for p, _ in _TYPES)
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
     "settembre", "ottobre", "novembre", "dicembre"))}
_MONTH_RE = "|".join(_MONTHS)
_NUM = r"(?P<num>\d+(?:[-\s]?(?:bis|ter|quater|quinquies))?)"
#: "art. 5, comma 3, della" before an act: the article and comma kept for
#: the label, the preposition not
_ART = r"(?:(?P<art>art(?:icolo|\.)?\s*\d+(?:-\w+)?(?:\s*,?\s*comma\s*\d+)?)\s*,?\s*(?:del(?:la)?\s+)?)?"
#: "legge 31 ottobre 2003, n. 306" — the full form; a few words of title may
#: stand between the form and the date ("legge sull'emigrazione testo unico
#: 13 novembre 1919, n. 2205", operator 2026-10-13: the citation went
#: unrecognised and the act was answered by ranking alone)
_CITE_DATED = re.compile(
    rf"{_ART}(?P<type>{_TYPE_RE})(?:\s+[^\s\d,;]+){{0,8}}?\s*,?\s+(?P<day>\d{{1,2}})[°]?\s+(?P<month>{_MONTH_RE})\s+(?P<year>\d{{4}})"
    rf"\s*,?\s*n\.?\s*{_NUM}", re.I)
#: "l. 306/2003", "d.lgs. n. 82 del 2005", "d.lgs. 82 del 7 marzo 2005"
_CITE_SHORT = re.compile(
    rf"{_ART}(?P<type>{_TYPE_RE})\s*n?\.?\s*{_NUM}\s*(?:/|del\s+(?:\d{{1,2}}\s+(?:{_MONTH_RE})\s+)?|,\s*)"
    rf"(?P<year>\d{{4}})", re.I)


#: a dated citation names its month: the month group of `_CITE_DATED` IS this
#: search, with the same flags, so a text where it finds nothing has no dated
#: citation — and it costs a third of the dated pattern's scan (2 M characters
#: of gutenberg text: 0.22 s against 0.67 s; the whole `citations` 1.12 s).
#: The text-index stage runs this over every chunk of every book (the parse
#: pool's children, app/entities.py `entity_ids_of`; München 2026-10-30: the
#: children of an fts batch of big books all in `citations`).
_MONTH_ANY = re.compile(_MONTH_RE, re.I)


def _type_token(text: str) -> str | None:
    for pattern, token in _TYPES:
        if re.fullmatch(pattern, text.strip(), re.I):
            return token
    return None


def citations(query: str) -> list[dict]:
    """The acts a text names: `{tipo, numero, anno, data?, art?}` each, in
    order of appearance, once — the dated form first where both name the
    same act, since it carries the date."""
    found: list[tuple[int, dict]] = []
    seen: set[tuple] = set()
    for pattern in ((_CITE_DATED, _CITE_SHORT) if _MONTH_ANY.search(query) else (_CITE_SHORT,)):
        for m in pattern.finditer(query):
            tipo = _type_token(m.group("type"))
            if tipo is None:
                continue
            data = None
            if pattern is _CITE_DATED:
                month = _MONTHS[m.group("month").lower()]
                data = f"{int(m.group('year')):04d}-{month:02d}-{int(m.group('day')):02d}"
            num = _WS.sub("", m.group("num").replace(" ", "")).lower()
            key = (tipo, num, int(m.group("year")))
            if key in seen:
                continue
            seen.add(key)
            found.append((m.start(), {
                "tipo": tipo, "numero": num, "anno": int(m.group("year")), "data": data,
                "art": _text(m.group("art")).rstrip(" ,") if m.group("art") else None}))
    return [c for _, c in sorted(found, key=lambda t: t[0])]
