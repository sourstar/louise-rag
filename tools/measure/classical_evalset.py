"""MEASUREMENT (not product code): the Latin / Greek -> English reference set of
docs/notes/classical-translation.md, built from public-domain Project Gutenberg texts
read from gutenberg's own SHARD (dumped by classical_shard.py `dump` to OUT/texts/<id>.json).
Alignment is by explicit numbers only: book + chapter, numbered sections, poem numbers,
printed page pairs of a bilingual edition.

Sets:
  la   Caesar BG V-VII (PG 18837 / McDevitte & Bohn, PG 10657), Augustine Confessiones
       (PG 33849 / Pusey, PG 3296), Catullus (Ellis, PG 23294 / Smithers' prose, PG 20732)
  grc  Ancient Greek from bilingual editions: Galen (PG 43383), Dionysius of Halicarnassus
       (PG 50212), Didache (PG 42053)
  el   Katharevousa Greek AS GUTENBERG HOLDS IT: Herodotus I-II in Skalidis
       translation (PG 38055) against Macaulay (PG 2707) -- a LOOSE reference, both
       translate the same ancient text.
Writes OUT/evalset.json.  Usage: python3 classical_evalset.py OUT
"""
from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

MIN, MAX = 1200, 3200          # source chars per passage (~300-800 est. tokens, chars/4)
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman(s: str) -> int:
    n = 0
    for a, b in zip(s, s[1:] + " "):
        v = ROMAN[a]
        n += -v if b != " " and ROMAN.get(b, 0) > v else v
    return n


def secs(texts: Path, n: str) -> list[tuple[str, str]]:
    return [(h, b or "") for h, b in json.loads((texts / f"{n}.json").read_text())["sections"]]


def paras(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def group(units: list[tuple[str, str, str]], k: int, seed: str) -> list[dict]:
    """`units` are aligned (label, source, reference) in reading order. Consecutive units
    are joined into passages of MIN..MAX source chars; k of them are taken, spread evenly
    through the work (seeded)."""
    cands: list[list[tuple[str, str, str]]] = []
    cur: list[tuple[str, str, str]] = []
    for u in units:
        if len(u[1]) > MAX:
            cur = []
            continue
        if cur and sum(len(x[1]) for x in cur) + len(u[1]) > MAX:
            cur = []
        cur.append(u)
        if sum(len(x[1]) for x in cur) >= MIN:
            cands.append(cur)
            cur = []
    if len(cands) <= k:
        pick = cands
    else:
        step = len(cands) / k
        r = random.Random(seed)
        pick = [cands[min(len(cands) - 1, int(i * step + r.random() * step))] for i in range(k)]
    return [{"label": f"{c[0][0]} – {c[-1][0]}" if len(c) > 1 else c[0][0],
             "src": "\n\n".join(x[1] for x in c), "ref": "\n\n".join(x[2] for x in c)}
            for c in pick]


# ---- Latin --------------------------------------------------------------------------

def caesar(t: Path) -> list[dict]:
    lines = "\n".join(b for _, b in secs(t, "18837")).split("\n")
    books: list[dict[int, list[str]]] = []
    cur: dict[int, list[str]] | None = None
    last = 0
    for ln in lines:                      # a chapter starts at a line holding only its number
        m = re.fullmatch(r"\s*(\d+)\s*", ln)
        if m:
            n = int(m.group(1))
            if n == 1:
                cur = {}
                books.append(cur)
                last = 0
            if cur is not None and n == last + 1:
                last = n
                cur[n] = []
                continue
        if cur is not None and last in cur:
            cur[last].append(ln)
    la = {5 + i: {k: ws(" ".join(v)) for k, v in b.items()} for i, b in enumerate(books[:3])}
    en: dict[int, dict[int, str]] = {}
    for h, b in secs(t, "10657"):
        m = re.search(r"BOOK ([IVX]+)$", h)
        if not m or "CIVIL" in h or roman(m.group(1)) not in (5, 6, 7):
            continue
        ch = re.split(r"(?m)^([IVXL]+)\.—", b)
        en[roman(m.group(1))] = {roman(ch[i]): ws(ch[i + 1]) for i in range(1, len(ch) - 1, 2)}
    units = [(f"BG {bk}.{n}", la[bk][n], en[bk][n]) for bk in (5, 6, 7)
             for n in sorted(la.get(bk, {})) if n in en.get(bk, {}) and la[bk][n]]
    print("caesar units", len(units), {b: (len(la.get(b, {})), len(en.get(b, {}))) for b in (5, 6, 7)})
    return [dict(p, work="Caesar, De Bello Gallico (PG 18837 / McDevitte & Bohn, PG 10657)")
            for p in group(units, 8, "caesar")]


def augustine(t: Path) -> list[dict]:
    la = [(h, b) for h, b in secs(t, "33849") if h.startswith("LIBER")]
    en = [(h, b) for h, b in secs(t, "3296") if h.startswith("BOOK")]
    units = []
    for i, ((_, lb), (_, eb)) in enumerate(zip(la, en), start=1):
        parts = re.split(r"(?m)^(\d+)\. ", lb)
        lsec = {int(parts[j]): parts[j + 1] for j in range(1, len(parts) - 1, 2)}
        eparas = [p for p in paras(eb) if len(p) > 120]
        if len(lsec) != len(eparas):
            continue                       # a book whose counts differ is not used
        for k, (n, x) in enumerate(sorted(lsec.items())):
            x = re.sub(r"(?m)^[IVXL]+\. .*$", "", x)          # the chapter-argument lines
            x = re.sub(r"\^\{\[\d+\]\}", "", x)
            units.append((f"Conf. {i}.{n}", ws(x.replace("_", "")), ws(eparas[k])))
    print("augustine units", len(units))
    return [dict(p, work="Augustine, Confessiones (PG 33849 / Pusey, PG 3296)")
            for p in group(units, 8, "augustine")]


def catullus(t: Path) -> list[dict]:
    la = {}
    for h, b in secs(t, "23294"):
        if re.fullmatch(r"[IVXLC]+", h.strip()):
            lines = []
            for ln in b.split("\n"):
                if ("_" in ln or "Ω" in ln or ln.startswith(h.strip() + " ")
                        or re.match(r"\s*\d+(, \d+)? \S.*[:\]]", ln)):
                    continue                   # Ellis's critical apparatus, page by page
                if ln.strip() and not re.fullmatch(r"\s*\d+\s*", ln):
                    lines.append(re.sub(r"\s+\d+\s*$", "", ln))
            la[roman(h.strip())] = "\n".join(lines).strip()
    en_txt = "\n".join(b for h, b in secs(t, "20732") if h.startswith("C. VALERII"))
    blocks = re.split(r"(?m)^([IVXLC]+)\.\s*$", en_txt)
    en: dict[int, str] = {}
    for j in range(1, len(blocks) - 1, 2):
        long = [p for p in paras(blocks[j + 1]) if len(p) > 250 and "\n" not in p]
        if long:
            en[roman(blocks[j])] = long[-1]          # Smithers' prose follows Burton's verse
    units = [(f"Catullus {n}", la[n], en[n]) for n in sorted(la) if n in en
             and 300 <= len(la[n]) <= MAX and 0.8 <= len(en[n]) / len(la[n]) <= 2.0]
    print("catullus units", len(units))
    return [dict(p, work="Catullus, Carmina (Ellis, PG 23294 / Smithers' prose, PG 20732)")
            for p in group(units, 4, "catullus")]


# ---- Ancient Greek, bilingual editions ---------------------------------------------------

def galen(t: Path) -> list[dict]:
    s = secs(t, "43383")
    start = next(i for i, (h, _) in enumerate(s) if h.startswith("GALEN › ON THE NATURAL"))
    gk = next(i for i, (h, b) in enumerate(s) if i > start and "ΓΑΛΗΝΟΥ" in (h + b[:400]))

    def walk(rng: list[tuple[str, str]]) -> dict[tuple[int, int], str]:
        out: dict[tuple[int, int], str] = {}
        book = 1
        for h, b in rng:
            m = re.search(r"(?:BOOK ([IVX]+) › |([ΑΒΓ]) › )?([IVX]+)$", h.strip())
            if not m:
                continue
            if m.group(1):
                book = roman(m.group(1))
            elif m.group(2):
                book = " ΑΒΓ".index(m.group(2))
            out[(book, roman(m.group(3)))] = b
        return out

    en, gr = walk(s[start:gk]), walk(s[gk:])

    def clean_en(x: str) -> str:
        x = re.sub(r"(?<=[A-Za-z_”.,;:)\]])\d{1,3}\b", "", x)      # footnote markers
        x = re.sub(r"(?<=[A-Za-z_”.,;:)\]]) \d{1,3}(?=[\s,.;:])", "", x)
        return ws(x.replace("_", ""))

    def clean_gr(x: str) -> str:
        x = re.sub(r"ΓΑΛΗΝΟΥ ΠΕΡΙ ΦΥΣΙΚΩΝ ΔΥΝΑΜΕΩΝ [ΑΒΓ]?", " ", x)
        x = re.sub(r"\bK [IVX]+\. p\. \d+", " ", x)
        x = re.sub(r"\|\|\s*\d+", " ", x)
        x = re.sub(r"(?<!\w)\d{1,4}(?!\w)", " ", x)
        return ws(x)

    def greek_script(x: str) -> bool:      # part of Book III's Greek is transliterated
        g = len(re.findall(r"[Ͱ-Ͽἀ-῿]", x))
        return g > 0.5 * len(re.findall(r"\w", x))

    units = [(f"Nat. Fac. {b}.{c}", clean_gr(gr[(b, c)]), clean_en(en[(b, c)]))
             for (b, c) in sorted(gr) if (b, c) in en and greek_script(gr[(b, c)])]
    print("galen units", len(units), len(en), len(gr))
    return [dict(p, work="Galen, On the Natural Faculties (Brock's bilingual edition, PG 43383)")
            for p in group(units, 10, "galen")]


def dionysius(t: Path) -> list[dict]:
    s = secs(t, "50212")
    i0 = next(i for i, (h, _) in enumerate(s) if h.startswith("ΔΙΟΝΥΣΙΟΥ ΑΛΙΚΑΡΝΑΣΕΩΣ"))
    txt = "\n".join(b for _, b in s[i0:])
    units = []
    for unit in txt.split("* * * * *"):    # one unit: a Greek page, [page], its English page
        m = re.search(r"(?m)^\[(\d+)\]\s*$", unit)
        if not m or not re.search(r"[ἀ-῿]", unit[:m.start()]):
            continue
        g = "\n".join(re.sub(r"\s+\d{1,2}\s*$", "", ln) for ln in unit[:m.start()].strip().split("\n"))
        g = re.sub(r"-\n", "", g)
        eng = []
        for p in paras(unit[m.end():]):
            if re.match(r"\d", p) or re.search(r"[Ͱ-Ͽἀ-῿]{3}", p[:60]):
                break                        # the apparatus and the notes
            eng.append(p)
        if eng:
            units.append((f"p. {int(m.group(1)) - 1}", ws(g), ws(" ".join(eng))))
    print("dionysius units", len(units))
    return [dict(p, work="Dionysius of Halicarnassus, De compositione verborum "
                         "(Roberts' bilingual edition, PG 50212)")
            for p in group(units, 8, "dionysius")]


def didache(t: Path) -> list[dict]:
    b = next(b for h, b in secs(t, "42053") if h.strip() == "ΔΙΔΑΧΗ — ΤΩΝ ΔΩΔΕΚΑ ΑΠΟΣΤΟΛΩΝ.")
    split = b.find("Teaching\nof the")
    gparts = re.split(r"Κεφ\. [^\n]*?\^\{—(\d+)—\}", b[:split])
    eparts = re.split(r"Chap\.? [IVX]+[.:]*\^\{ ?(\d+) ?\}—", b[split:])

    def cg(x: str) -> str:
        x = re.sub(r"(?m)^\d+\s*", "", x)
        x = re.sub(r"–\d+–", "", x)
        return ws(re.sub(r"-\n", "", x))

    def ce(x: str) -> str:
        x = re.sub(r"–\d+–", "", x)
        return ws(re.sub(r"-\n", "", x).replace("_", ""))

    G = {int(gparts[i]): cg(gparts[i + 1]) for i in range(1, len(gparts) - 1, 2)}
    E = {int(eparts[i]): ce(eparts[i + 1]) for i in range(1, len(eparts) - 1, 2)}
    units = [(f"Did. {n}", G[n], E[n]) for n in sorted(G) if n in E]
    print("didache units", len(units), len(G), len(E))
    return [dict(p, work="Didache (Hitchcock & Brown's bilingual edition, PG 42053)")
            for p in group(units, 4, "didache")]


# ---- Katharevousa Greek as gutenberg holds it (LOOSE reference) ----------------------

def numbered_books(text: str) -> list[dict[int, str]]:
    """Chapters as paragraphs opening `N. `; a book starts where N falls back to 1."""
    books: list[dict[int, str]] = []
    last = 0
    for p in paras(text):
        m = re.match(r"(\d+)\. ", p)
        if not m:
            continue
        n = int(m.group(1))
        if n == 1 and (not books or last > 5):
            books.append({})
            last = 0
        if books and last < n <= last + 3:          # a missed chapter does not end the book
            books[-1][n] = p[m.end():]
            last = n
    return books


def herodotus(t: Path) -> list[dict]:
    g = numbered_books("\n\n".join(b for _, b in secs(t, "38055")))
    e = numbered_books("\n\n".join(b for _, b in secs(t, "2707")))

    def ce(x: str) -> str:                 # Macaulay's inline section numbers
        return ws(re.sub(r"(?<=[\s,;:.])\d{1,3}(?=\s)", "", x))

    units = [(f"Hdt. {bk}.{n}", ws(gb[n]), ce(eb[n])) for bk, (gb, eb) in enumerate(zip(g[:2], e[:2]), 1)
             for n in sorted(gb) if n in eb]
    print("herodotus units", len(units), [len(b) for b in g[:4]], [len(b) for b in e[:4]])
    return [dict(p, work="Herodotus I-II: Skalidis' Katharevousa translation (PG 38055) "
                         "/ Macaulay (PG 2707) -- LOOSE reference")
            for p in group(units, 10, "herodotus")]


def main() -> None:
    out = Path(sys.argv[1])
    t = out / "texts"
    sets = {"la": caesar(t) + augustine(t) + catullus(t),
            "grc": galen(t) + dionysius(t) + didache(t),
            "el": herodotus(t)}
    items = []
    for lang, ps in sets.items():
        for k, p in enumerate(ps):
            items.append({"id": f"{lang}-{k:02d}", "lang": lang, **p,
                          "src_chars": len(p["src"]), "ref_chars": len(p["ref"])})
    (out / "evalset.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))
    for lang in sets:
        xs = [i for i in items if i["lang"] == lang]
        print(lang, len(xs), "src chars", sum(i["src_chars"] for i in xs), "ref/src",
              round(sum(i["ref_chars"] for i in xs) / max(1, sum(i["src_chars"] for i in xs)), 2))


if __name__ == "__main__":
    main()
