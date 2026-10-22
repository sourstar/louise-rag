"""gutenberg's SAME-TITLE work claims (operator, 2026-10-22: "use the
same-title rule") as a claims file for `louise hold collection create
annotations` (07 §7.3), merged with the claims gutenberg-book-editions
already makes.

    python -m tools.annotations_from_titles records.jsonl \\
        --existing editions-claims.jsonl --out claims.jsonl

INPUT: `records.jsonl` as tools/measure/one_language_records.py writes it
(uid, lang, title, creators). `--existing`: the claims file the current
editions collection was created from; its claims are kept as they are and
a pair they already join is not claimed again.

THE RULE (operator's correction, 2026-10-22: "different volumes of the
same collection may have different authors"). `base_title` is the title
lowercased, trimmed, PG's own branding prefix dropped ("The Gutenberg …",
"The Project Gutenberg …") and a trailing part designator dropped
(": Section A and B", ", Volume 2", "Part I", "(1st 100 Pages)", "(of 3)").
Two books are one work when their base titles are equal and EITHER
- both titles carried a designator — the volumes of one collection,
  whoever each volume credits: München measured 63 of 1,179 such groups
  whose creators differ by volume, all 10 sampled one collection (Legends
  of the Jews: Szold on vols 1–2, Radin on 3–4; the Cambridge Natural
  History, an anthology; a translator credited on one volume only);
- or they share at least one author — a bare title alone joins different
  works (Confidence Game, Harmon / McKimmey; The Eyes Have It, Garrett /
  McKimmey / Dick; The Snare; Galatea: 4 of 15 sampled groups), and the
  shared author separated every one of them. "Project Gutenberg",
  "Various", "Anonymous" and "Unknown" are not authors.
Known reach: it joins the VOLUMES of one work (History of Korea vol. 1 and
2; Henry IV Part 1 and 2), which is "one hit per work" (05 §5.3) and not
an edition in the strict sense; the claim's evidence says which test
joined it.

THE CLAIMS: `same-work`, a STAR per group — every book claimed against the
group's lowest ebook number, as tools/annotations_from_works.py does.
Confidence (11 + 1) / (11 + 2) = 0.9230769: the first sample's
right/checked (the volume test's 10 of 10 agrees), not certainty. Evidence: the base title, which test joined the pair, the shared
authors, both full titles.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CONTRIBUTOR = "Louise reference node (same-title rule, operator 2026-10-22)"
METHOD = {"kind": "matcher", "id": "gutenberg-same-title", "version": 1}
CONFIDENCE = "0.9230769"
NOT_AUTHORS = {"project gutenberg", "various", "anonymous", "unknown", "anonymous."}

_BRAND = re.compile(r"^the\s+(?:project\s+)?gutenberg\s+")
_ORD = (r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|one|two|three|four|five|"
        r"six|seven|eight|nine|ten|\d+(?:st|nd|rd|th)?|[ivxlcdm]+")
_LETTERS = r"[a-z](?:\s*(?:,|&|and)\s*(?:and\s+)?[a-z])*"
_DESIGNATOR = re.compile(
    r"\s*(?:[:,;.\-—–]\s*|\(\s*)?"
    r"(?:section|sections|volume|vol\.?|part|book|tome|tomo|band|teil|libro|parte|livre)\s+"
    rf"(?:{_ORD}|{_LETTERS})\.?"
    r"(?:\s*\(?\s*of\s+(?:\d+|[a-z]+)\s*\)?)?\s*\)?\s*$")
_PAGES = re.compile(r"\s*\([^()]*\bpages?\b[^()]*\)\s*$")
_OF_N = re.compile(r"\s*\(\s*of\s+\d+\s*\)\s*$")


def split_title(title: str) -> tuple[str, bool]:
    """(base title, whether a part designator was dropped)."""
    t = re.sub(r"\s+", " ", (title or "").lower()).strip()
    t = _BRAND.sub("", t)
    designated = False
    for _ in range(3):                     # "Vol. 2 (of 3)", "Section A (1st 100 Pages)"
        before = t
        t = _PAGES.sub("", t)
        t = _OF_N.sub("", t)
        t = _DESIGNATOR.sub("", t).strip(" :,;.-—–")
        if t == before:
            break
        designated = True
    return t, designated


def base_title(title: str) -> str:
    return split_title(title)[0]


def authors(creators) -> frozenset[str]:
    return frozenset(a for a in (re.sub(r"\s+", " ", str(c)).strip().lower() for c in creators or [])
                     if a and a not in NOT_AUTHORS)


def _num(uid: str) -> int:
    return int(uid.split(":", 1)[1])


def claims_of(records: list[dict], existing: list[dict] = ()) -> list[dict]:
    """The new claims, minus the pairs `existing` already joins (directly or
    through its stars): per base title, a star of the designated volumes on
    the lowest of them, then each book joined to the first book of the same
    base title it shares an author with. Every claim is a pair one of the
    two tests joined."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for c in existing:
        parent[find(c["subject"])] = find(c["object"])
    lang = {r["uid"]: (r.get("lang") or "und") for r in records}
    title = {r["uid"]: r.get("title") or "" for r in records}
    by_base: dict[str, list[tuple[str, bool, frozenset]]] = defaultdict(list)
    for r in sorted(records, key=lambda r: _num(r["uid"])):
        base, designated = split_title(r.get("title") or "")
        if base:
            by_base[base].append((r["uid"], designated, authors(r.get("creators"))))
    out = []

    def claim(uid: str, other: str, base: str, test: str, who: frozenset) -> None:
        if find(uid) == find(other):
            return                         # already one work
        parent[find(uid)] = find(other)
        out.append({"type": "same-work", "subject": uid, "object": other,
                    "subject_lang": lang[uid], "object_lang": lang[other], "method": METHOD,
                    "evidence": {"base_title": base, "test": test, "authors": sorted(who),
                                 "subject_title": title[uid], "object_title": title[other],
                                 "note": "same title; may be another volume of the work"},
                    "confidence": CONFIDENCE, "contributor": CONTRIBUTOR})
    for base in sorted(by_base, key=lambda b: _num(by_base[b][0][0])):
        books = by_base[base]
        if len(books) < 2:
            continue
        volumes = [b for b in books if b[1]]
        for uid, _d, _w in volumes[1:]:
            claim(uid, volumes[0][0], base, "both titles name a volume or part", frozenset())
        for i, (uid, _d, who) in enumerate(books):
            for other, _d2, who2 in books[:i]:
                shared = who & who2
                if shared:
                    claim(uid, other, base, "a shared author", shared)
                    break
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
    print(json.dumps({"existing": len(existing), "new": len(new), "books_joined": len(books)}),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
