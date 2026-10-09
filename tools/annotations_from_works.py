"""gutenberg's WORK claims (02 §2.10; 05 §5.3 "one hit per work", amended
2026-10-09; docs/notes/one-language-per-work.md §1) as a claims file for
`louise hold collection create annotations` (07 §7.3).

    python -m tools.annotations_from_works records.jsonl wiki_map.json --out claims.jsonl

INPUTS, both written by the one-language measurement
(tools/measure/one_language_records.py and one_language_works.py wikidata):

- `records.jsonl`: per gutenberg record its uid and lang, the URLs of its
  RDF description's "Wikipedia page about this book: <url>" lines (`wiki`)
  and the ebook numbers of its "Project Gutenberg has several editions of
  this eBook" note (`editions`, `editions_note`) — what the ORIGIN states;
- `wiki_map.json`: each Wikipedia page's Wikidata item, through the
  Wikipedia API's `pageprops.wikibase_item`, redirects followed.

WHY AN ANNOTATIONS COLLECTION. Two books naming Wikipedia pages that
Wikidata's sitelinks join are one work by WIKIDATA's statement, not
Project Gutenberg's; 05 §5.5 (amended 2026-10-09) puts such a link in an
annotations collection, never in gutenberg's records — and nothing of
gutenberg is rebuilt for it (CLAUDE.md RULE 1: its records, vectors and
indexes stay what they are). The search reads the claims from the held
copy's in-memory index; nothing asks Wikidata at search time.

THE CLAIMS, all `same-work` (the method does not know which side is the
source; the original language is not stated per pair):

- `wikidata`: the books whose Wikipedia lines resolve to ONE item are one
  work. A STAR per item — every book claimed against the item's lowest
  ebook number — so a work of n books is n-1 claims, not n(n-1)/2 (Don
  Quixote: 60, not 1,830); the search walks the star. A book naming
  SEVERAL items is an anthology or an omnibus, not an edition, and claims
  nothing through them (grouping through one chained King Lear to
  Grillparzer). Evidence: the item and both books' Wikipedia URLs.
  Confidence 0.96875 = (30 + 1) / (30 + 2): 30 of 30 hand-checked
  cross-language works were one work (crosslang_sample, seeded).
- `gutenberg-editions-note`: a book and each held ebook its editions note
  names — Project Gutenberg's own statement, read by us; skipped where the
  pair is already one Wikidata work. Confidence 1. Evidence: the note.

A pair is claimed once, either way round (`read_claims` refuses a repeat).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path

CONTRIBUTOR = "Louise reference node (one-language-per-work measurement, 2026-10-09)"
WIKIDATA = {"kind": "wikidata"}
EDITIONS = {"kind": "matcher", "id": "gutenberg-editions-note", "version": 1}
WIKIDATA_CONFIDENCE = "0.96875"
EDITIONS_CONFIDENCE = "1"


def split_wiki(url: str) -> tuple[str, str] | None:
    """('en.wikipedia.org', 'Title with spaces') or None — as
    tools/measure/one_language_works.py keyed `wiki_map.json`."""
    u = urllib.parse.urlparse(url.strip())
    host = u.netloc.lower()
    if not host.endswith("wikipedia.org"):
        return None
    host = host.replace(".m.wikipedia.org", ".wikipedia.org")
    if u.path.startswith("/wiki/"):
        title = urllib.parse.unquote(u.path[len("/wiki/"):])
    else:
        q = urllib.parse.parse_qs(u.query)
        if "title" not in q:
            return None
        title = q["title"][0]
    title = title.split("#", 1)[0].replace("_", " ").strip()
    return (host, title) if title else None


def _num(uid: str) -> int:
    return int(uid.split(":", 1)[1])


def claims_of(records: list[dict], wiki_map: dict) -> list[dict]:
    lang = {r["uid"]: (r.get("lang") or "und") for r in records}
    item_books: dict[str, list[tuple[str, str]]] = defaultdict(list)   # item -> [(uid, url)]
    for r in sorted(records, key=lambda r: _num(r["uid"])):
        found: dict[str, str] = {}
        for url in r.get("wiki") or []:
            s = split_wiki(url)
            q = wiki_map.get("titles", {}).get(f"{s[0]}|{s[1]}") if s else None
            if q:
                q = (wiki_map.get("items", {}).get(q) or {}).get("redirect_to") or q
                found.setdefault(q, url)
        if len(found) == 1:                    # several items: an anthology, no claim through it
            (q, url), = found.items()
            item_books[q].append((r["uid"], url))
    out: list[dict] = []
    claimed: set[tuple[str, str]] = set()
    work_of: dict[str, str] = {}

    def claim(subject: str, obj: str, method: dict, evidence: dict, confidence: str) -> None:
        pair = tuple(sorted((subject, obj)))
        if subject == obj or pair in claimed:
            return
        claimed.add(pair)
        out.append({"type": "same-work", "subject": subject, "object": obj,
                    "subject_lang": lang[subject], "object_lang": lang[obj],
                    "method": method, "evidence": evidence, "confidence": confidence,
                    "contributor": CONTRIBUTOR})

    for q in sorted(item_books, key=lambda q: (int(q[1:]) if q[1:].isdigit() else 0, q)):
        books = item_books[q]
        if len(books) < 2:
            continue
        centre, centre_url = books[0]
        for uid, url in books:
            work_of[uid] = q
        for uid, url in books[1:]:
            claim(uid, centre, WIKIDATA,
                  {"wikidata": q, "subject_wikipedia": url, "object_wikipedia": centre_url},
                  WIKIDATA_CONFIDENCE)
    for r in sorted(records, key=lambda r: _num(r["uid"])):
        for n in r.get("editions") or []:
            other = f"gutenberg:{n}"
            if other not in lang or other == r["uid"]:
                continue                       # an edition this collection does not hold
            if work_of.get(other) is not None and work_of.get(other) == work_of.get(r["uid"]):
                continue                       # already one Wikidata work
            claim(r["uid"], other, EDITIONS, {"note": r.get("editions_note") or ""},
                  EDITIONS_CONFIDENCE)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("records", type=Path)
    ap.add_argument("wiki_map", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    records = [json.loads(line) for line in a.records.open(encoding="utf-8") if line.startswith("{")]
    claims = claims_of(records, json.loads(a.wiki_map.read_text("utf-8")))
    with a.out.open("w", encoding="utf-8", newline="\n") as f:
        for c in claims:
            f.write(json.dumps(c, ensure_ascii=False, sort_keys=True) + "\n")
    by = defaultdict(int)
    for c in claims:
        by[c["method"].get("id", c["method"]["kind"])] += 1
    books = {u for c in claims for u in (c["subject"], c["object"])}
    print(json.dumps({"claims": len(claims), "by_method": dict(by), "books": len(books)}),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
