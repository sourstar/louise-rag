"""One annotations collection for gutenberg's versions of a work (operator,
2026-10-19: "both collections belong to the same one"): the wikidata claims
(`tools/annotations_from_works.py`) and the classical matcher's
(`tools/annotations_from_matches.py`) as one claims file for `louise hold
collection create annotations` (07 §7.3).

    python -m tools.annotations_merge works.jsonl matches.jsonl --out claims.jsonl

The wikidata claims go in whole. A matcher claim goes in only when the
pair's two books are not already one work by the wikidata claims, through any
chain of them: the wikidata claims are stars on a work's lowest ebook, so a
matched pair inside one work states nothing new, and the same pair twice is
refused by `create` anyway. On 2026-10-19 that kept 16 of the matcher's 261
claims at the 0.95 cut, once the 45 adaptations and one unverified pair were
left out: 13 more books, two more works (annotation-collections.md, "The
review of 2026-10-19"). Every claim keeps its own method, evidence and
confidence; nothing is rewritten.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _lines(path: Path) -> list[bytes]:
    return [line for line in path.read_bytes().split(b"\n") if line.strip()]


class _Works:
    """The works the claims join, by union-find over their (subject, object) pairs."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)

    def together(self, a: str, b: str) -> bool:
        return a in self.parent and b in self.parent and self.find(a) == self.find(b)


def merge(works: list[bytes], matches: list[bytes]) -> tuple[list[bytes], dict]:
    """(the merged lines — the wikidata claims as they were, then the matcher
    claims that add a link, each line's bytes unchanged — and a summary)."""
    w = _Works()
    for line in works:
        c = json.loads(line)
        w.join(c["subject"], c["object"])
    before = {"books": len(w.parent), "works": len({w.find(u) for u in w.parent})}
    added: list[bytes] = []
    inside = 0
    for line in matches:
        c = json.loads(line)
        if w.together(c["subject"], c["object"]):
            inside += 1
            continue
        added.append(line)
        w.join(c["subject"], c["object"])
    after = {"books": len(w.parent), "works": len({w.find(u) for u in w.parent})}
    return works + added, {"claims": len(works) + len(added), "from_works": len(works),
                           "from_matches": len(added), "matches_already_one_work": inside,
                           "before": before, "after": after}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("works", type=Path, help="annotations_from_works.py's claims")
    ap.add_argument("matches", type=Path, help="annotations_from_matches.py's claims")
    ap.add_argument("--out", type=Path, required=True, help="the merged claims file (JSON Lines)")
    a = ap.parse_args(argv)
    lines, summary = merge(_lines(a.works), _lines(a.matches))
    a.out.write_bytes(b"".join(line + b"\n" for line in lines))
    print(json.dumps(summary, indent=1), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
