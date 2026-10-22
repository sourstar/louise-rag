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

THE RULE. Two books are one work when their titles are equal after
`base_title` — lowercased, trimmed, PG's own branding prefix dropped ("The
Gutenberg …", "The Project Gutenberg …") and a trailing part designator
dropped (": Section A and B", ", Volume 2", "Part I", "(1st 100 Pages)",
"(of 3)") — AND their authors are the same set. "Project Gutenberg",
"Various", "Anonymous" and "Unknown" are not authors: a book with no
author left claims nothing, because a bare title ("Poems", "The Snare")
joins different works.

WHY AUTHORS. München's sample of the title rule alone (2026-10-22, 2,560
groups over 79,692 in-scope records): 15 random groups, 11 one work and 4
different works of one title (Confidence Game, Harmon / McKimmey; The Eyes
Have It, Garrett / McKimmey / Dick; The Snare; Galatea). Requiring the same
authors kept the 11 and dropped the 4. Known reach: it joins the VOLUMES
of one work too (History of Korea vol. 1 and 2; Henry IV Part 1 and 2),
which is "one hit per work" (05 §5.3) and not an edition in the strict
sense; the claim says so in its evidence.

THE CLAIMS: `same-work`, a STAR per group — every book claimed against the
group's lowest ebook number, as tools/annotations_from_works.py does.
Confidence (11 + 1) / (11 + 2) = 0.9230769: the sample's right/checked,
not certainty. Evidence: the base title, the authors, both full titles.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

CONTRIBUTOR = "Louise reference node (same-title rule, operator 2026-10-22)"
METHOD = {"kind": "matcher", "id": "gutenberg-same-title-authors", "version": 1}
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


def base_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").lower()).strip()
    t = _BRAND.sub("", t)
    for _ in range(3):                     # "Vol. 2 (of 3)", "Section A (1st 100 Pages)"
        before = t
        t = _PAGES.sub("", t)
        t = _OF_N.sub("", t)
        t = _DESIGNATOR.sub("", t).strip(" :,;.-—–")
        if t == before:
            break
    return t


def authors(creators) -> frozenset[str]:
    return frozenset(a for a in (re.sub(r"\s+", " ", str(c)).strip().lower() for c in creators or [])
                     if a and a not in NOT_AUTHORS)


def _num(uid: str) -> int:
    return int(uid.split(":", 1)[1])


def claims_of(records: list[dict], existing: list[dict] = ()) -> list[dict]:
    """The new claims: a star per (base title, authors) group of two or
    more, minus the pairs `existing` already joins (directly or through
    its stars)."""
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
    groups: dict[tuple[str, frozenset], list[str]] = defaultdict(list)
    for r in sorted(records, key=lambda r: _num(r["uid"])):
        who = authors(r.get("creators"))
        base = base_title(r.get("title") or "")
        if who and base:
            groups[(base, who)].append(r["uid"])
    out = []
    for (base, who), uids in sorted(groups.items(), key=lambda kv: _num(kv[1][0])):
        if len(uids) < 2:
            continue
        centre = uids[0]
        for uid in uids[1:]:
            if find(uid) == find(centre):
                continue                   # already one work by an existing claim
            parent[find(uid)] = find(centre)
            out.append({"type": "same-work", "subject": uid, "object": centre,
                        "subject_lang": lang[uid], "object_lang": lang[centre],
                        "method": METHOD,
                        "evidence": {"base_title": base, "authors": sorted(who),
                                     "subject_title": title[uid], "object_title": title[centre],
                                     "note": "same title and authors; may be another volume of the work"},
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
    print(json.dumps({"existing": len(existing), "new": len(new), "books_joined": len(books)}),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
