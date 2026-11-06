"""DIAGNOSTIC (read-only): why a part counts the coverage it counts, bundle by
bundle (`possession.explain_part`).

Written for the Perugia hand-over (2026-10-25): two peers holding the same 380
bundles counted 9,010 chunks apart over 127 gutenberg parts. For each part
named, every accepted coverage row that could hold its records — this
collection's and its re-mint predecessors' — with the test it passes or
fails: `counted` (its shard id carries a prefix the index reads), `files`
(its possession.json is here), `uids` and `same_hash` (of the part's records,
how many the bundle holds, and how many with the CURRENT content hash).
Nothing is written but the caches the count itself keeps.

  python tools/measure/coverage_explain.py gutenberg lang=de/00030 lang=en/00494 [--root DIR]

One JSON object per part on stdout; a line per part on stderr naming the
rows that hold the part's records but are not credited, and why.
"""
import argparse
import json
import sys
from contextlib import closing
from pathlib import Path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("collection")
    p.add_argument("parts", nargs="+", help="as the snapshot names them: lang=de/00030")
    p.add_argument("--root", type=Path, default=None, help="the collections root (default: the node's)")
    a = p.parse_args(argv)
    from app import collections as collections_mod
    from app.hold import collection as col
    from app.share import coverage as cov, possession
    root = a.root or collections_mod.louise_root() / "collections"
    name, codex = col.resolve(root, a.collection)
    with closing(cov.manifest(root, name)) as con:
        for part in a.parts:
            out = possession.explain_part(root, name, codex, con, part)
            print(json.dumps(out, ensure_ascii=False), flush=True)
            missed = [b for b in out["bundles"]
                      if b["uids"] and not (b["counted"] and b["files"] and b["same_hash"] == b["uids"])]
            print(f"{part}: covered {out['covered']} of {out['chunks']} chunks, {len(out['bundles'])} bundle(s); "
                  + ("; ".join(f"{b['bundle_id'][7:19]}… {b['uids']} uids: "
                               + ", ".join(w for w, bad in (("not counted (shard key)", not b["counted"]),
                                                            ("no files here", not b["files"]),
                                                            (f"{b['uids'] - b['same_hash']} changed since",
                                                             b["same_hash"] < b["uids"])) if bad)
                               for b in missed) or "every holding row credited"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
