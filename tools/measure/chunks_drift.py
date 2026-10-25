"""DIAGNOSTIC (read-only): records whose chunk count in this node's manifest
(`documents.chunks`, written by the text-index stage from the LIVE parse)
differs from the count of the record ITSELF — its `sections` cut through
`layers.chunks_for_document`, as `hold.land.cut` cuts them (a record line
carries no chunk count; the snapshot keeps totals per part, and the cut
reproduces them exactly: 7,714 / 8,820 / 7,085, München 2026-10-25). Invariant 3 says chunking is a
pure function of the cleaned text and the chunker's version, so the two can
differ only when one of them is stale or when the live parse and the record
disagree on the text — and the coverage count, which converts covered
records to chunks through the manifest, is then wrong (2026-10-25: three
gutenberg parts counted 6,975 / 7,814 / 7,066 here against the snapshot's
7,714 / 8,820 / 7,085, every record covered).

  python tools/measure/chunks_drift.py gutenberg lang=de/00030 lang=en/00494 [--all] [--root DIR]

Per part on stdout, one JSON object: records, the two totals, and every
record that differs with its manifest columns (fts_version, entity_version,
updated_at, in_scope, error). On stderr a line per part and, with --all, the
collection's totals and the drifting records' fts_version histogram.

The routine check is `louise dev index-drift` (07 §7.3, 2026-10-25): the
same comparison in bounded windows, cutting only the parts whose totals
disagree, plus the text index and the content and origin hashes. This tool
stays for its per-part form, which cuts every record of the parts named.
"""
import argparse
import json
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path


# The two functions live in app/hold/drift.py since 2026-10-25, where
# `louise dev index-drift` — this tool's check, windowed, over the text index too —
# calls them; they are re-exported here for this tool's callers.
from app.hold.drift import drift, record_chunks  # noqa: E402,F401


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("collection")
    p.add_argument("parts", nargs="*", help="as the snapshot names them: lang=de/00030")
    p.add_argument("--all", action="store_true", help="every part of the latest snapshot")
    p.add_argument("--root", type=Path, default=None, help="the collections root (default: the node's)")
    p.add_argument("--ids-out", type=Path, default=None,
                   help="write the differing uids here, one a line: `louise dev reindex --ids` reads it")
    a = p.parse_args(argv)
    from app import collections as collections_mod
    from app.hold import collection as col, land, seed
    from app.share import coverage as cov
    root = a.root or collections_mod.louise_root() / "collections"
    name, codex = col.resolve(root, a.collection)
    snapshot = seed.latest_snapshot(Path(root), name)
    parts = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards] if a.all else a.parts
    totals = Counter()
    versions: Counter = Counter()
    drifting: list[str] = []
    with closing(cov.manifest(root, name)) as con:
        for part in parts:
            got = drift(con, land.records_for(root, name, snapshot, part, None),
                        lambda rec: record_chunks(codex, rec))
            got["part"] = part
            print(json.dumps(got, ensure_ascii=False), flush=True)
            totals.update(records=got["records"], record_chunks=got["record_chunks"],
                          manifest_chunks=got["manifest_chunks"], differing=len(got["differs"]),
                          parts_with_drift=1 if got["differs"] else 0)
            versions.update(str(d.get("fts_version")) for d in got["differs"])
            drifting.extend(d["uid"] for d in got["differs"] if not d.get("missing"))
            if not a.all:
                print(f"{part}: {got['records']} records, record chunks {got['record_chunks']}, manifest "
                      f"{got['manifest_chunks']}; {len(got['differs'])} record(s) differ", file=sys.stderr)
    print(json.dumps({"collection": name, "parts": len(parts), **totals,
                      "drifting_fts_versions": dict(versions)}), file=sys.stderr)
    if a.ids_out:
        a.ids_out.write_text("".join(f"{u}\n" for u in drifting), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
