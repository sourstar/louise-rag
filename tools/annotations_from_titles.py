"""gutenberg's SAME-TITLE work claims (operator, 2026-10-22: "use the
same-title rule") as a claims file for `louise hold collection create
annotations` (07 §7.3), merged with the claims gutenberg-book-editions
already makes.

    python -m tools.annotations_from_titles records.jsonl \\
        --existing editions-claims.jsonl --out claims.jsonl

INPUT: `records.jsonl` as tools/measure/one_language_records.py writes it
(uid, lang, title, creators, and the catalogue's `agents` [role, agent id,
name], `scan` (MARC 904) and `lccn` (MARC 010)). A record without `agents`
falls back to its creator names as ids. `--existing`: the claims file the
current editions collection was created from; its claims are kept as they
are and a book they already join to its group is not claimed again.

THE TITLE. Lowercased, trimmed, PG's branding prefix dropped ("The
Gutenberg …"), and a trailing part designator dropped: "Volume 2 (of 3)",
", vol. I", "Part 1", ": Section A and B", "(1st 100 Pages)", and a bare
roman numeral after a genre word ("Kertomuksia II"; "Richard II" stays a
title). The designator's number is kept.

THE RULE, within one title — the catalogue's ids, not name strings
(München, 2026-10-22: every one of the 418 titles it leaves split read by
hand; the misses are series with no volume number, State of the Union
Addresses 5010–5049, which no title rule reaches):
1. a SHARED AGENT ID, any role (`2009/agents/N`): name variants are one
   person ("Bates, Lindon" and "Bates, Lindon, Jr." are agent 58742);
2. the same SOURCE SCAN (MARC 904) or the same LCCN (MARC 010);
3. a VOLUME RUN: both titles name a volume, whoever each volume credits
   (operator: "different volumes of the same collection may have different
   authors" — Legends of the Jews, Szold on vols 1–2, Radin on 3–4) — unless
   their numbers collide: two "Volume 1"s under different people are two
   sets (Beard's and Andrews' History of the United States);
4. NOBODY NAMED: books crediting no one (Anonymous, Various, Unknown,
   Project Gutenberg) join each other when no book of the title credits
   anyone (5 of 5 read right: The Boyhood of Jesus, 施公案…). An anonymous
   book beside a named one joins nothing: read over all 14 such pairs, 7
   were other works (The Rosary beside Barclay's novel, Genesis beside
   Piper's story, an anonymous "Winter" that bridged Sharp's and Thijsse's).
A GENRE title ("Poems", "Kertomuksia", "Essays"…) joins only the volumes of
one person: two "Poems" are not one work.

THE CLAIMS: `same-work`, a STAR per group — every book claimed against the
group's lowest ebook number, as tools/annotations_from_works.py does. The
evidence names the test that brought the book into its group and the book
it joined through. Confidence (n + 1) / (n + 2) over the read-through
(`CONFIDENCE`), not certainty.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CONTRIBUTOR = "Louise reference node (same-title rule, operator 2026-10-22)"
METHOD = {"kind": "matcher", "id": "gutenberg-same-title", "version": 2}
#: München's read-through of 2026-10-22, (n + 1) / (n + 2): 60 groups drawn at
#: random from the final rule's output, 60 right (an earlier draw found the
#: two faults rule 4 and the translator test now close)
CONFIDENCE = "0.9838710"

NOBODY = re.compile(r"^(?:anonymous|anonyme|anónimo|anonimo|unknown|various|project gutenberg)\b", re.I)
#: roles that name who made the work, as opposed to who produced the ebook
AUTHOR_ROLES = {"creator", "aut"}

_BRAND = re.compile(r"^the\s+(?:project\s+)?gutenberg\s+")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
          "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
          "xix": 19, "xx": 20}
_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
          "eighth": 8, "ninth": 9, "tenth": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUM = r"\d+(?:st|nd|rd|th)?|[ivxlcdm]+|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|" \
       r"one|two|three|four|five|six|seven|eight|nine|ten"
_LETTERS = r"[a-z](?:\s*(?:,|&|and)\s*(?:and\s+)?[a-z])*"
_KEYWORD = r"sections?|volume|vol\.?|part|book|tome|tomo|band|teil|libro|parte|livre|osa|deel|nide"
_DESIGNATOR = re.compile(
    r"\s*(?:[:,;.\-—–]\s*|\(\s*)?"
    rf"(?:{_KEYWORD})\s+(?P<n>{_NUM}|{_LETTERS})\.?"
    r"(?:\s*\(?\s*of\s+(?:\d+|[a-z]+)\s*\)?)?\s*\)?\s*$")
_BARE_ROMAN = re.compile(r"\s*[:,;.\-—–]?\s+(?P<n>i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii)\.?\s*$")
_PAGES = re.compile(r"\s*\([^()]*\bpages?\b[^()]*\)\s*$")
_OF_N = re.compile(r"\s*\(\s*of\s+\d+\s*\)\s*$")

_GENRE = (r"poems?|verses?|poetry|gedichte|runoja|runoelmia|runoelmat|kertomuksia|kertoelmia|novelleja|"
          r"novelle|novellen|novelas?|nouvelles|poesie|poésies|poesías|poesias|poemas|poèmes|poemes|essays?|"
          r"essais|stories|tales|short stories|plays|dramas?|letters|sermons|works|lyrics|songs|sonnets|"
          r"ballads|ballades|fables|sketches|miscellanies|selections|writings|addresses|speeches|lectures|"
          r"rhymes|erzählungen|geschichten|märchen|racconti|liriche|rime|versi|dikter|gedichten|verzen|"
          r"contos|satuja|tarinoita|elbeszélések|διηγήματα|novels|romances|hymns|no title|untitled")
_GENRE_TITLE = re.compile(
    r"^(?:(?:the|a|an|selected|collected|complete|new|short|early|later|last|miscellaneous|select|minor|"
    rf"little|more|other|some|valittuja|valikoima|and|of)\s+)*(?:{_GENRE})(?:\s+(?:and|ja|und)\s+(?:{_GENRE}))?[.!]?$")


def _number(tok: str) -> int | None:
    tok = tok.strip(". ").lower()
    m = re.fullmatch(r"(\d+)(?:st|nd|rd|th)?", tok)
    if m:
        return int(m.group(1))
    return _ROMAN.get(tok, _WORDS.get(tok))


def split_title(title: str) -> tuple[str, bool, int | None]:
    """(base title, whether a part designator was dropped, its number)."""
    t = re.sub(r"\s+", " ", (title or "").lower()).strip()
    t = _BRAND.sub("", t)
    designated, number = False, None
    for _ in range(4):                     # "Vol. 2 (of 3)", "Section A (1st 100 Pages)"
        before = t
        t = _PAGES.sub("", t)
        t = _OF_N.sub("", t)
        m = _DESIGNATOR.search(t)
        if m is None:                      # a bare numeral only after a genre word: "Richard II" is a title
            m = _BARE_ROMAN.search(t)
            if m and not is_genre(t[:m.start()].strip(" :,;.-—–")):
                m = None
        if m and m.start() > 0:
            if number is None:
                number = _number(m.group("n"))
            t = t[:m.start()]
        t = t.strip(" :,;.-—–")
        if t == before:
            break
        designated = True
    return t, designated, number


def base_title(title: str) -> str:
    return split_title(title)[0]


def is_genre(base: str) -> bool:
    return bool(_GENRE_TITLE.match(base))


def people(record: dict) -> tuple[frozenset[str], frozenset[str]]:
    """(every agent the catalogue credits, those credited as author), as
    ids; "nobody" credits (Anonymous, Various, Project Gutenberg) left out.
    A record without catalogue agents falls back to its creator names."""
    agents = record.get("agents")
    if agents is None:
        names = [re.sub(r"\s+", " ", str(c)).strip() for c in record.get("creators") or []]
        ids = frozenset(f"name:{n.lower()}" for n in names if n and not NOBODY.match(n))
        return ids, ids
    every, authors = set(), set()
    for role, agent_id, name in agents:
        if not agent_id or NOBODY.match(name or ""):
            continue
        every.add(agent_id)
        if role in AUTHOR_ROLES:
            authors.add(agent_id)
    return frozenset(every), frozenset(authors)


def _num(uid: str) -> int:
    return int(uid.split(":", 1)[1])


def groups_of(books: list[dict]) -> list[tuple[list[int], dict[int, tuple[int, str]]]]:
    """The works among `books` (one title, in ebook order): each a list of
    indexes and, per index but the first, (the index it joined through,
    the test)."""
    m = len(books)
    parent = list(range(m))
    numbers = [{b["number"]} - {None} for b in books]
    edges: list[tuple[int, int, str]] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int, test: str) -> None:
        a, b = find(i), find(j)
        if a == b:
            return
        parent[a] = b
        numbers[b] |= numbers[a]
        edges.append((i, j, test))
    genre = is_genre(books[0]["base"])
    for i in range(m):
        for j in range(i):
            a, b = books[i], books[j]
            if genre:
                if a["designated"] and b["designated"] and a["agents"] & b["agents"]:
                    join(i, j, "volumes of one person under a genre title")
            elif a["agents"] & b["agents"]:
                join(i, j, "a shared agent id")
            elif a["scan"] and a["scan"] == b["scan"]:
                join(i, j, "the same source scan (MARC 904)")
            elif a["lccn"] and a["lccn"] == b["lccn"]:
                join(i, j, "the same LCCN (MARC 010)")
    if not genre:
        for i in range(m):
            for j in range(i):
                if books[i]["designated"] and books[j]["designated"] and find(i) != find(j):
                    if not (numbers[find(i)] & numbers[find(j)]):
                        join(i, j, "a volume run")
        # nameless = credits nobody at all (Tagore credited as translator of
        # his own "The Crescent Moon" is not nameless beside F. B. Young's
        # novel), and joins only when no book of the title names anyone: an
        # anonymous book beside ONE named work was another work 7 times in 14
        # (The Rosary, Genesis, The Wright Brothers…), right twice
        nameless = [i for i in range(m) if not books[i]["agents"]]
        if len(nameless) == m:
            for i in nameless[1:]:
                join(i, nameless[0], "no book of the title names anyone")
    by_root: dict[int, list[int]] = defaultdict(list)
    for i in range(m):
        by_root[find(i)].append(i)
    out = []
    for members in by_root.values():
        if len(members) < 2:
            continue
        inside = set(members)
        adj: dict[int, list[tuple[int, str]]] = defaultdict(list)
        for i, j, test in edges:
            if i in inside:
                adj[i].append((j, test))
                adj[j].append((i, test))
        head = min(members)
        via: dict[int, tuple[int, str]] = {}
        todo = [head]
        while todo:                        # each book's way into the group, from its head
            x = todo.pop(0)
            for y, test in sorted(adj[x]):
                if y != head and y not in via:
                    via[y] = (x, test)
                    todo.append(y)
        out.append((sorted(members), via))
    return out


def claims_of(records: list[dict], existing: list[dict] = ()) -> list[dict]:
    """The new claims: per title, per work, a star on the work's lowest
    ebook number, minus the books `existing` already joins to it (directly
    or through its stars)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for c in existing:
        parent[find(c["subject"])] = find(c["object"])
    by_base: dict[str, list[dict]] = defaultdict(list)
    for r in sorted(records, key=lambda r: _num(r["uid"])):
        base, designated, number = split_title(r.get("title") or "")
        if not base:
            continue
        every, authors = people(r)
        by_base[base].append({"uid": r["uid"], "lang": r.get("lang") or "und", "title": r.get("title") or "",
                              "base": base, "designated": designated, "number": number,
                              "agents": every, "authors": authors,
                              "scan": re.sub(r"/(?:page|mode)/.*$", "", (r.get("scan") or "").strip()),
                              "lccn": (r.get("lccn") or "").strip()})
    out = []
    for base in sorted(by_base, key=lambda b: _num(by_base[b][0]["uid"])):
        books = by_base[base]
        if len(books) < 2:
            continue
        for members, via in groups_of(books):
            head = books[members[0]]
            for i in members[1:]:
                b = books[i]
                if find(b["uid"]) == find(head["uid"]):
                    continue               # already one work
                parent[find(b["uid"])] = find(head["uid"])
                through, test = via[i]
                out.append({"type": "same-work", "subject": b["uid"], "object": head["uid"],
                            "subject_lang": b["lang"], "object_lang": head["lang"], "method": METHOD,
                            "evidence": {"base_title": base, "test": test, "through": books[through]["uid"],
                                         "subject_title": b["title"], "object_title": head["title"],
                                         "note": "same title; may be another volume of the work"},
                            "confidence": CONFIDENCE, "contributor": CONTRIBUTOR})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("--existing", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    records = [json.loads(line) for line in a.records.open(encoding="utf-8") if line.startswith("{")]
    existing = ([json.loads(line) for line in a.existing.open(encoding="utf-8") if line.startswith("{")]
                if a.existing else [])
    new = claims_of(records, existing)
    with a.out.open("w", encoding="utf-8", newline="\n") as f:
        for c in [*existing, *new]:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    books = {u for c in new for u in (c["subject"], c["object"])}
    tests: dict[str, int] = defaultdict(int)
    for c in new:
        tests[c["evidence"]["test"]] += 1
    print(json.dumps({"existing": len(existing), "new": len(new), "books_joined": len(books),
                      "groups": len({c["object"] for c in new}), "by_test": dict(tests)}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
