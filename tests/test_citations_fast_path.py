"""The citation scan's fast path (app/refs/normattiva.py `_matches`;
docs/notes/python-hot-spots.md B): the patterns are tried only where a
citation can start, and the answer is the plain scan's on every input —
compared byte for byte with the verbatim reference
(tests/citations_reference.py) on every fixture, thousands of generated
texts and a 400 KB book."""
import json
import random
import re
from pathlib import Path

from app.refs import normattiva as nref
from tests import citations_reference as ref

FIXTURES = Path(__file__).parent / "fixtures"


def _answer(citations, text: str) -> str:
    """The answer as bytes to compare — or the exception, which must be the
    same too."""
    try:
        return json.dumps(citations(text), sort_keys=True)
    except Exception as e:   # noqa: BLE001 — compared, not hidden
        return f"raises {type(e).__name__}: {e}"


def _same(texts) -> int:
    """Asserts both answers equal on every text; returns how many cite."""
    citing = 0
    for t in texts:
        new, old = _answer(nref.citations, t), _answer(ref.citations, t)
        assert new == old, t[:200]
        citing += old != "[]" and not old.startswith("raises")
    return citing


# ---- what the fast path rests on --------------------------------------------------

def test_the_patterns_are_the_reference_s_byte_for_byte():
    for name in ("_CITE_SHORT", "_CITE_DATED"):
        new, old = getattr(nref, name), getattr(ref, name)
        assert (new.pattern, new.flags) == (old.pattern, old.flags), name


def _openings() -> set[str]:
    """The first two characters of every way a citation begins, read from
    the patterns' own source: `_ART` ("art") and every form in `_TYPES`
    (none has a group, so `|` splits them; `\\b` reads no character)."""
    assert nref._ART.startswith("(?:(?P<art>art")
    out = {"ar"}
    for pattern, _ in nref._TYPES:
        for form in pattern.split("|"):
            form = form.replace(r"\b", "")
            assert form[0].isalpha() and (form[1].isalpha() or form[1:3] == r"\."), form
            out.add(form[0] + ("." if form[1] == "\\" else form[1]))
    return out


def test_a_citation_can_start_only_where_the_start_pattern_holds():
    """`_START` reads its first letter case-sensitively, so that `re` can skip
    to it: right only if a, d, l, r and t fold to their two cases alone under
    re.I — every code point checked, the non-ASCII folds (Kelvin's K, ſ, ı,
    İ) included — and then it holds at every opening, in every case."""
    every = "".join(map(chr, range(0x110000)))
    openings = _openings()
    assert openings == {"ar", "le", "l.", "de", "d.", "dl", "dp", "re", "r.", "te", "t."}
    for letter in {c for o in openings for c in o if c.isalpha()}:
        for pattern in (letter, f"[{letter}]"):
            folds = {every[m.start()] for m in re.compile(pattern, re.IGNORECASE).finditer(every)}
            assert folds == {letter, letter.upper()}, pattern
    for first, second in openings:
        for a in {first, first.upper()}:
            for b in {second, second.upper()}:
                assert nref._START.match(a + b), a + b
    assert not nref._START.match("al") and not nref._START.match("dr"), "two letters, not one"


def test_the_longest_citations_are_found():
    """The white-space runs before the digits, counted to their maximum —
    15 short, 23 dated — in every kind of white space `\\s` holds."""
    ws = iter([" ", "\n", "\t", "\u00a0", "\u2003", "  ", "\r\n", "\u3000"] * 10)
    art = "art{}5{},{}comma{}3{},{}della{}".format(*[next(ws) for _ in range(7)])
    form = "decreto{}del{}presidente{}del{}consiglio{}dei{}ministri".format(*[next(ws) for _ in range(6)])
    short = f"x {art}{form}{next(ws)}n.{next(ws)}306/2003 e"
    title = "".join(f"{next(ws)}parola{c}" for c in "abcdefgh")
    dated = f"x {art}{form}{title}{next(ws)},{next(ws)}31 ottobre 2003, n. 306 e"
    for text, data in ((short, None), (dated, "2003-10-31")):
        got = nref.citations(text)
        assert [(c["tipo"], c["numero"], c["anno"], c["data"], c["art"]) for c in got] == [
            ("decreto.del.presidente.del.consiglio.dei.ministri", "306", 2003, data,
             "art 5 , comma 3")], text
        assert _same([text, "; " + text, text.replace("art", ";art", 1)]) >= 2


def test_a_citation_longer_than_the_first_look_back_is_found():
    """`_reach` reads a slice before the digits and widens it while the
    match fills it: a long article suffix, long white space, long title
    words."""
    texts = [f"art. 5-{'x' * 700} legge 306/2003", f"legge{' ' * 3000}306/2003",
             f"decreto-legge {' '.join('t' * 300 for _ in range(8))} 31 ottobre 2003, n. 306",
             f"{'parola ' * 400}legge n.{chr(10) * 600}306 del 7 marzo 2003"]
    assert _same(texts) == 4
    assert _same([t.replace(" t", " ;t", 1) for t in texts]) == 3, "no `;` in a title"


# ---- the answer, compared ---------------------------------------------------------

def test_every_fixture_gives_the_reference_answer():
    texts = [p.read_bytes().decode("utf-8", errors="replace") for p in sorted(FIXTURES.rglob("*"))
             if p.is_file() and not any(part.startswith(".") for part in p.relative_to(FIXTURES).parts)]
    assert len(texts) > 20
    assert _same(texts) >= 4, "the normattiva fixtures cite acts"


_FORMS = ("legge", "legge costituzionale", "l.", "l. cost.", "l.cost.", "decreto legislativo",
          "d.lgs.", "d.lgs", "d. lgs.", "dlgs", "d.lg.", "decreto-legge", "decreto legge", "d.l.",
          "d. l.", "dl", "decreto del presidente della repubblica", "d.p.r.", "d. p. r.", "dpr",
          "decreto del presidente del consiglio dei ministri", "d.p.c.m.", "dpcm",
          "regio decreto-legge", "regio decreto legge", "r.d.l.", "regio decreto", "r.d.",
          "testo unico", "t.u.", "decreto", "regio", "testo", "lex", "costituzione")
_ARTS = ("art. 5", "art.5", "articolo 12", "art 3-bis", "art. 5, comma 3,", "art. 7 comma 2 della",
         "art. 2 del", "dell'art. 4", "art. 10-quater, comma 1, della", "art.", "articolo")
_MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
           "settembre", "ottobre", "novembre", "dicembre",
           "January", "February", "March", "April", "May", "June", "July", "August", "September",
           "October", "November", "December",
           "Januar", "Februar", "März", "Mai", "Juni", "Juli", "Oktober", "Dezember",
           "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
           "octobre", "décembre")
_WORDS = ("the", "and", "of", "il", "la", "del", "della", "che", "sulla", "emigrazione",
          "obblighi", "comunitari", "Roma", "München", "Straße", "café", "naïve", "東京", "😀",
          "allegge", "decretolegge", "xdl", "dl5", "l", "d", "r", "t", "n", "n.", "bis", ";", ":",
          "(", ")", '"', "'", "«", "»", "—", "…", "²", "½", "e\u0301", "\ud800", "June 3, 1865",
          "12/2003", "pp. 12, 1999", "1, 2004", "del 2005", "N°", "°")
_WS = (" ", " ", " ", "  ", "\n", "\t", "\u00a0", "\u2003", "", " \n ", "\r\n")
_FOLD = {"s": "\u017f", "i": "\u0131\u0130", "k": "\u212a"}


def _mangle(rng: random.Random, word: str) -> str:
    if rng.random() < 0.5:
        return word
    out = []
    for ch in word:
        r = rng.random()
        if r < 0.03 and ch in _FOLD:
            ch = rng.choice(_FOLD[ch])
        elif r < 0.4:
            ch = ch.upper()
        out.append(ch)
    return "".join(out)


def _digits(rng: random.Random, n: int) -> str:
    s = "".join(rng.choice("0123456789") for _ in range(n))
    if rng.random() < 0.04:    # `\d` holds every decimal digit, not only ASCII
        s = s.translate(str.maketrans("0123456789", rng.choice(("٠١٢٣٤٥٦٧٨٩", "０１２３４５６７８９"))))
    return s


def _citation(rng: random.Random) -> str:
    def ws() -> str:
        return rng.choice(_WS)

    parts = []
    if rng.random() < 0.3:
        parts += [_mangle(rng, rng.choice(_ARTS)), ws()]
    parts.append(_mangle(rng, rng.choice(_FORMS)))
    num = _digits(rng, rng.choice((1, 2, 3, 3, 4, 5))) + rng.choice(
        ("",) * 6 + ("bis", "-bis", " ter", "quater", "-quinquies", "BIS", "-x"))
    if rng.random() < 0.45:
        for _ in range(rng.choice((0, 0, 1, 2, 5, 8, 9))):
            parts += [ws() or " ", _mangle(rng, rng.choice(_WORDS))]
        parts += [rng.choice(("", ",", " ,", ", ")), ws() or " ",
                  _digits(rng, rng.choice((1, 2, 2, 3))), rng.choice(("", "°")), ws() or " ",
                  _mangle(rng, rng.choice(_MONTHS)), ws() or " ", _digits(rng, rng.choice((4, 4, 3, 5))),
                  rng.choice(("", ",", " ,")), ws(), rng.choice(("n.", "n", "N.", "", "nr.")), ws(), num]
    else:
        parts += [ws(), rng.choice(("", "n.", "n. ", "N.", "n ", "n")), ws(), num,
                  rng.choice(("/", " / ", " del ", "del ", ", ", ",", " , ", " - ")),
                  rng.choice(("",) * 4 + (f"{_digits(rng, 2)} {_mangle(rng, rng.choice(_MONTHS))} ",)),
                  _digits(rng, rng.choice((4, 4, 4, 2, 3, 5)))]
    return "".join(parts)


def generated(seed: int, n: int, *, longest: int = 3_000) -> list[str]:
    """`n` texts, seeded: citations in every form and spelling, cut and
    bent, among prose, numbers, dates and months in four languages,
    punctuation and non-ASCII."""
    rng = random.Random(seed)
    texts = []
    for _ in range(n):
        pieces: list[str] = []
        size, target = 0, rng.randint(1, longest)
        while size < target:
            r = rng.random()
            if r < 0.35:
                pieces.append(_citation(rng))
            elif r < 0.45:
                pieces.append(_digits(rng, rng.randint(1, 6)))
            elif r < 0.5:
                pieces.append(_mangle(rng, rng.choice(_MONTHS)))
            else:
                pieces.append(_mangle(rng, rng.choice(_WORDS)))
            pieces.append(rng.choice(_WS + (". ", ", ", "; ", "\n\n")))
            size += len(pieces[-2]) + len(pieces[-1])
        texts.append("".join(pieces))
    return texts


def test_generated_texts_give_the_reference_answer():
    texts = generated(20261102, 2_000)
    assert _same(texts) > 1_000, "most of them cite something"


def prose(size: int) -> str:
    """The hotspots measurement's synthetic text: the fixture books' prose,
    repeated to `size` (no digit in it)."""
    bodies = [next(d.glob("*.txt.utf8")).read_text().split("***", 2)[2].split("*** END", 1)[0].strip()
              for d in sorted((FIXTURES / "gutenberg-mini" / "corpus").iterdir())]
    base = "\n\n".join(bodies)
    return (base * (size // len(base) + 1))[:size]


def book(size: int = 400_000, seed: int = 7) -> str:
    """`prose` with, every few kilobytes, a citation, a date or a number."""
    base = prose(60_000)
    rng = random.Random(seed)
    out, n = [], 0
    while n < size:
        piece = base[rng.randrange(len(base) // 2):][:rng.randint(500, 4_000)]
        piece += " " + rng.choice((_citation(rng), f"June {rng.randint(1, 30)}, 18{rng.randint(10, 99)}",
                                   f"CHAPTER {rng.randint(1, 60)}", "")) + " "
        out.append(piece)
        n += len(piece)
    return "".join(out)[:size]


def test_a_400_kb_book_gives_the_reference_answer():
    assert _same([book(), prose(400_000)]) == 1
