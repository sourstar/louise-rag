"""DIAGNOSTIC (read-only): records whose chunk count in this node's manifest
(`documents.chunks`, written by the text-index stage from the LIVE parse)
differs from the count the records build wrote into the record itself
(`chunks` on the record line, the snapshot's). Invariant 3 says chunking is a
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
"""
import argparse
import json
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path


def drift(con, records: list[dict]) -> dict:
    """The part's records against the manifest rows of the same uids."""
    uids = [r["uid"] for r in records]
    rows: dict[str, dict] = {}
    for i in range(0, len(uids), 900):
        part = uids[i:i + 900]
        for row in con.execute(
                "SELECT uid, chunks, fts_version, entity_version, updated_at, in_scope, error FROM documents "
                f"WHERE uid IN ({','.join('?' * len(part))})", part):
            rows[row[0]] = {"chunks": row[1], "fts_version": row[2], "entity_version": row[3],
                            "updated_at": row[4], "in_scope": row[5], "error": row[6]}
    differs = []
    for r in records:
        m = rows.get(r["uid"])
        want = int(r.get("chunks") or 0)
        have = None if m is None else int(m["chunks"] or 0)
        if have != want:
            differs.append({"uid": r["uid"], "record_chunks": want, "manifest_chunks": have,
                            **({k: v for k, v in m.items() if k != "chunks"} if m else {"missing": True})})
    return {"records": len(records), "record_chunks": sum(int(r.get("chunks") or 0) for r in records),
            "manifest_chunks": sum(int((rows.get(r["uid"]) or {}).get("chunks") or 0) for r in records),
            "differs": differs}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("collection")
    p.add_argument("parts", nargs="*", help="as the snapshot names them: lang=de/00030")
    p.add_argument("--all", action="store_true", help="every part of the latest snapshot")
    p.add_argument("--root", type=Path, default=None, help="the collections root (default: the node's)")
    a = p.parse_args(argv)
    from app import collections as collections_mod
    from app.hold import collection as col, land, seed
    from app.share import coverage as cov
    root = a.root or collections_mod.louise_root() / "collections"
    name, _codex = col.resolve(root, a.collection)
    snapshot = seed.latest_snapshot(Path(root), name)
    parts = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards] if a.all else a.parts
    totals = Counter()
    versions: Counter = Counter()
    with closing(cov.manifest(root, name)) as con:
        for part in parts:
            got = drift(con, land.records_for(root, name, snapshot, part, None))
            got["part"] = part
            print(json.dumps(got, ensure_ascii=False), flush=True)
            totals.update(records=got["records"], record_chunks=got["record_chunks"],
                          manifest_chunks=got["manifest_chunks"], differing=len(got["differs"]),
                          parts_with_drift=1 if got["differs"] else 0)
            versions.update(str(d.get("fts_version")) for d in got["differs"])
            if not a.all:
                print(f"{part}: {got['records']} records, record chunks {got['record_chunks']}, manifest "
                      f"{got['manifest_chunks']}; {len(got['differs'])} record(s) differ", file=sys.stderr)
    print(json.dumps({"collection": name, "parts": len(parts), **totals,
                      "drifting_fts_versions": dict(versions)}), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
