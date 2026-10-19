"""The first annotations collection's claims (02 §2.10;
docs/notes/annotation-collections.md): `classical_coverage.py`'s matches —
gutenberg's Latin and Greek books paired with an English version of the same
work held in gutenberg — as a claims file for `louise hold collection create
annotations` (07 §7.3).

    python -m tools.annotations_from_matches coverage.json --out claims.jsonl

INPUT: `coverage.json` as `tools/measure/classical_coverage.py match` wrote it
(on the reference node: /root/xlate-classical/out/coverage.json, 2026-10-09):
per la/el book its `matches`, each an English record of the same author with
the signals that matched it — `wikidata-edition`, `wikidata-wikipedia`,
`title <score>`, `llm`.

ONE CLAIM PER MATCHED PAIR, type `same-work`: subject the la/el book, object
the English record. Not `translation-of`: the matcher does not know which side
is the source — gutenberg's `el` is Modern Greek, often itself a translation
of the ancient text (Zervos' Thucydides and Crawley's are both translations),
and a Latin book may translate an English one (*Mysterium Arcae Boulé*). So
the claim is the one the method makes, that the two are versions of one work.

CONFIDENCE, by the pair's strongest signal, from the hand check of 30 matched
books (seed `precision-30`, docs/notes/classical-translation.md):

    tier       signals                       right/checked   confidence
    wikidata   wikidata-edition|-wikipedia   17/17 (with     0.95
    title      title >= 0.6                         title)   0.95
    llm        llm alone                     10/13           0.73

`confidence` is (right + 1) / (checked + 2), so a perfect small sample is not
written as certainty. The unit checked was the BOOK ("a correct English
version among its matches"), not the pair: a book the llm gave several
candidates carries some wrong pairs, so the llm tier's pair precision is below
its 0.73. Each claim's evidence says so.

THRESHOLD (`--min-confidence`, default 0.73): the 90 % precision (27 of 30)
was measured with all three tiers included, so the cut that gave it is the
llm tier's confidence and every match is included — 357 pairs over 141 books,
which is the 41 % of the la and of the el tokens. `--min-confidence 0.95`
keeps the wikidata and title tiers alone (17 of 17 on the check): 261 pairs
over 97 books.

ADAPTATIONS ARE NOT VERSIONS (2026-10-19). The hand review of the 62 pairs
the wikidata claims had not already joined (docs/notes/annotation-collections.md,
"The review of 2026-10-19") found 45 wrong: retellings, children's versions
and paraphrases matched on their title (Church's *Story of the Odyssey*,
*The Iliad for Boys and Girls*, Housman's Lysistrata), Campe's Latin
*Robinson* matched to Defoe's novel and even to its sequel. The title tier
cannot tell a translation from an adaptation, so a pair is left out when:

- either title marks an adaptation (`ADAPTATION_MARKERS`);
- the la/el book has a creator the English record lacks (`--records`, the
  measurement's records.jsonl: Campe beside Defoe). The reverse is allowed:
  an English volume holding several authors' works (*The Greek Romances of
  Heliodorus, Longus and Achilles Tatius*) does contain the book;
- the pair is in `--exclude`, a JSON list of `[subject, object, why]` judged
  by hand.

Each left-out pair is counted in the summary with its reason.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

METHOD = {"kind": "matcher", "id": "louise-classical-matcher", "version": 1}
CONTRIBUTOR = "Louise reference node (classical-translation measurement, 2026-10-09)"
#: the hand check (docs/notes/classical-translation.md, "Precision")
CHECK = {"wikidata": (17, 17), "title": (17, 17), "llm": (10, 13)}
CHECK_SEED = "precision-30"
THRESHOLD = "0.73"
TARGET_LANG = "en"
#: a title that says the book retells, abridges or adapts another (the review
#: of 2026-10-19: every one of these was a wrong `same-work` pair)
ADAPTATION_MARKERS = re.compile(
    r"\b(story of|stories from|for boys and girls|boys'? and girls'?|told to the children|"
    r"adapted|adaptation|paraphrase|retold|in words of one syllable)\b", re.IGNORECASE)


def _who(name: str) -> str:
    return " ".join(name.lower().replace(".", " ").split())


def adaptation(book: dict, match: dict, creators: dict[str, frozenset[str]] | None = None) -> str | None:
    """Why the pair is an adaptation rather than two versions of one work, or None."""
    for side, title in (("English", match.get("title") or ""), (book.get("lang", ""), book.get("title") or "")):
        m = ADAPTATION_MARKERS.search(title)
        if m:
            return f"the {side} title marks an adaptation ({m.group(0).lower()!r})"
    if creators is not None:
        mine, theirs = creators.get(book["uid"]), creators.get(match["uid"])
        if mine and theirs and mine - theirs:
            return f"the {book.get('lang')} book has a creator the English record lacks: {sorted(mine - theirs)}"
    return None


def read_creators(lines: Iterable[str]) -> dict[str, frozenset[str]]:
    """uid -> its creators, normalised, from the measurement's records.jsonl."""
    out = {}
    for line in lines:
        if line.strip():
            r = json.loads(line)
            out[r["uid"]] = frozenset(_who(c) for c in r.get("creators") or [])
    return out


def tier_of(signals: list[str]) -> str:
    """The pair's strongest signal: wikidata, then title, then llm."""
    if any(s.startswith("wikidata") for s in signals):
        return "wikidata"
    if any(s.startswith("title") for s in signals):
        return "title"
    if "llm" in signals:
        return "llm"
    raise ValueError(f"no signal the matcher writes: {signals!r}")


def confidence(tier: str) -> str:
    """(right + 1) / (checked + 2), two decimals, as a decimal string (09 §9.1:
    no floats in anything hashed)."""
    right, checked = CHECK[tier]
    hundredths = (100 * (right + 1) + (checked + 2) // 2) // (checked + 2)
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def _dec(s: str) -> tuple[int, int]:
    whole, _, frac = s.partition(".")
    return int(whole), int((frac + "00")[:2])


def claims(coverage: dict, *, min_confidence: str = THRESHOLD,
           contributor: str = CONTRIBUTOR, creators: dict[str, frozenset[str]] | None = None,
           exclude: dict[tuple[str, str], str] | None = None,
           left_out: list[dict] | None = None) -> list[dict]:
    """The claims, sorted by (subject, object). A pair that is an adaptation
    (`adaptation`) or excluded by hand is not a claim; it is appended to
    `left_out` with its reason when a list is given."""
    out = []
    for book in coverage["books"]:
        if book["lang"] not in ("la", "el"):
            continue
        for m in book.get("matches") or []:
            tier = tier_of(m["why"])
            conf = confidence(tier)
            if _dec(conf) < _dec(min_confidence):
                continue
            why = adaptation(book, m, creators) or (exclude or {}).get((book["uid"], m["uid"]))
            if why:
                if left_out is not None:
                    left_out.append({"subject": book["uid"], "object": m["uid"], "why": why})
                continue
            right, checked = CHECK[tier]
            out.append({
                "type": "same-work", "subject": book["uid"], "object": m["uid"],
                "subject_lang": book["lang"], "object_lang": TARGET_LANG,
                "method": METHOD,
                "evidence": {
                    "candidate": "an English record sharing an author with the book (PG agent id "
                                 "or normalised name)",
                    "signals": list(m["why"]), "tier": tier,
                    "precision": {"right": right, "checked": checked, "unit": "book",
                                  "seed": CHECK_SEED,
                                  "note": "a book's matches were judged together; pair precision "
                                          "is lower where a book has several"}},
                "confidence": conf, "contributor": contributor})
    out.sort(key=lambda c: (c["subject"].encode(), c["object"].encode()))
    return out


def summary(cs: list[dict], left_out: list[dict] | None = None) -> dict:
    books = {c["subject"]: c["subject_lang"] for c in cs}
    out = {"claims": len(cs), "books": len(books),
           "books_by_lang": dict(sorted(Counter(books.values()).items())),
           "claims_by_tier": dict(sorted(Counter(c["evidence"]["tier"] for c in cs).items()))}
    if left_out is not None:
        out["left_out"] = len(left_out)
        out["left_out_by_reason"] = dict(sorted(Counter(re.sub(r"[:(].*", "", x["why"]).strip()
                                                        for x in left_out).items()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("coverage", type=Path, help="classical_coverage.py's coverage.json")
    ap.add_argument("--out", type=Path, required=True, help="the claims file to write (JSON Lines)")
    ap.add_argument("--min-confidence", default=THRESHOLD,
                    help=f"keep claims at or above this confidence (default {THRESHOLD}: the cut "
                         f"measured at 90%% precision)")
    ap.add_argument("--contributor", default=CONTRIBUTOR, help="who makes the claims")
    ap.add_argument("--records", type=Path, default=None,
                    help="the measurement's records.jsonl: its creators leave out a la/el book "
                         "that has a creator the English record lacks (an adaptation's author)")
    ap.add_argument("--exclude", type=Path, default=None,
                    help="JSON list of [subject, object, why]: pairs judged wrong or unverified by hand")
    a = ap.parse_args(argv)
    creators = read_creators(a.records.read_text(encoding="utf-8").splitlines()) if a.records else None
    exclude = {(s, o): f"excluded by hand: {why}"
               for s, o, why in json.loads(a.exclude.read_text(encoding="utf-8"))} if a.exclude else None
    left_out: list[dict] = []
    cs = claims(json.loads(a.coverage.read_text(encoding="utf-8")),
                min_confidence=a.min_confidence, contributor=a.contributor,
                creators=creators, exclude=exclude, left_out=left_out)
    a.out.write_bytes(b"".join(json.dumps(c, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8") + b"\n"
                               for c in cs))
    print(json.dumps(summary(cs, left_out), indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
