"""Several claims files into one, for ONE annotations collection
(02 §2.10; docs/notes/annotation-collections.md). Operator, 2026-10-19:
"the gutenberg graph collection" — gutenberg's works (Wikidata and PG's
editions notes, `annotations_from_works.py`) and the classical matcher's
same-work pairs (`annotations_from_matches.py`) as one collection of edges.

    python -m tools.annotations_merge works.jsonl classical.jsonl --out claims.jsonl

The claims reader refuses a claim made twice — the same pair, either way
round for `same-work` — so where two files state one pair, the claim with
the HIGHER confidence is kept (the first file's on a tie); `same-work` and
`translation-of` over one pair are two claims and both stay. Every claim
is checked as the reader checks it, so a refused line is refused here, by
file and line. The output is sorted by (subject, object, type) bytes, as
the tools write theirs, and the summary names what came from where.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.hold import annotations as ann


def merge(files: list[tuple[str, bytes]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """`(claims, summary)`; `files` are `(label, bytes)`."""
    kept: dict[tuple, tuple[str, dict[str, Any]]] = {}
    summary: dict[str, Any] = {"files": {}, "kept": 0, "dropped": {}, "by_type": {}, "by_method": {}}
    for label, data in files:
        n = 0
        for i, raw in enumerate(data.split(b"\n"), 1):
            raw = raw[:-1] if raw.endswith(b"\r") else raw
            if not raw.strip():
                continue
            try:
                c = ann.check_claim(json.loads(raw), f"{label} line {i}")
            except ValueError as e:
                raise ann.AnnotationsError(f"{label} line {i}: not JSON ({e})") from None
            n += 1
            k = ann._pair_key(c)
            held = kept.get(k)
            if held is None or Decimal(c["confidence"]) > Decimal(held[1]["confidence"]):
                if held is not None:
                    summary["dropped"][held[0]] = summary["dropped"].get(held[0], 0) + 1
                kept[k] = (label, c)
            else:
                summary["dropped"][label] = summary["dropped"].get(label, 0) + 1
        summary["files"][label] = n
    out = sorted((c for _l, c in kept.values()),
                 key=lambda c: (c["subject"].encode(), c["object"].encode(), c["type"].encode()))
    summary["kept"] = len(out)
    summary["by_type"] = dict(sorted(Counter(c["type"] for c in out).items()))
    summary["by_method"] = dict(sorted(Counter(
        c["method"]["kind"] + (f":{c['method']['id']}" if c["method"]["kind"] == "matcher" else "")
        for c in out).items()))
    summary["from"] = dict(sorted(Counter(l for l, _c in kept.values()).items()))
    return out, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path, help="claims files, JSON Lines; the first wins a tie")
    ap.add_argument("--out", type=Path, required=True, help="the merged claims file to write (JSON Lines)")
    a = ap.parse_args(argv)
    claims, summary = merge([(str(p), p.read_bytes()) for p in a.files])
    a.out.write_bytes(b"".join(json.dumps(c, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
                               for c in claims))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
