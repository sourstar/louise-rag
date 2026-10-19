"""The pubmed rows an ARCHIVE embedded from another input than the record cuts
to: a record whose only abstract is an `OtherAbstract` (POPLINE's "PIP"
citations, among others) is cut to its title alone (`sections` = [Title];
the other abstract is kept in `extra.other_abstracts`, never embedded), while
NCBI's MedCPT vector of it embeds title AND abstract — found 2026-10-19 by
pubmed-v8's vectors-sample (9 of one shard's 16, PMIDs ~12.3 M).

    python -m tools.pubmed_other_abstract_rows <collection> <out ids file>

One read of the latest snapshot's parts, through the Codex's layers (so the
chunk ids are the ones the table holds), kept to the rows a `genesis:`
producer made (`chunk_verification`). Prints the counts; writes one chunk id
a line, for `louise dev drop-rows <collection> --ids FILE --yes` (02 §2.4's
remedy: dropped, and embedded again from the record's own input).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def other_abstract_only(records: Iterable[dict]) -> Iterator[dict]:
    """The records whose sections are a title alone while the record carries
    an other abstract: their cut input is not the text the origin states."""
    for r in records:
        sections = r.get("sections") or []
        others = (r.get("extra") or {}).get("other_abstracts") or []
        if len(sections) == 1 and any((o or {}).get("text") for o in others):
            yield r


def main(argv: list[str] | None = None) -> int:
    from app import collections as cols
    from app.hold import collection as col, land
    from app.pack import records as pack_records

    p = argparse.ArgumentParser(prog="tools.pubmed_other_abstract_rows")
    p.add_argument("collection")
    p.add_argument("out", type=Path)
    a = p.parse_args(argv)
    root = cols.louise_root() / "collections"
    name, codex = col.resolve(root, a.collection)
    snaps = sorted((col.shard_dir_of(root, name) / "snapshots").glob("*.json"))
    snap = land.snapshot_named(root, name, snaps[-1].stem)
    tree = col.shard_dir_of(root, name)
    ids: list[str] = []
    records = 0
    for e in snap.shards:
        found = list(other_abstract_only(pack_records.iter_records(tree / e.path)))
        records += e.records
        ids.extend(land.cut(codex, found))
    c = cols.by_name(name)
    con = sqlite3.connect(f"file:{c.manifest_path}?mode=ro", uri=True)
    archive: list[str] = []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        archive.extend(r[0] for r in con.execute(
            f"SELECT chunk_id FROM chunk_verification WHERE chunk_id IN "
            f"({','.join('?' * len(part))}) AND producer LIKE 'genesis:%'", part))
    a.out.write_text("".join(f"{x}\n" for x in sorted(archive)), encoding="utf-8")
    print(f"{name}: {records} records; {len(ids)} cut to a title alone beside an other "
          f"abstract; {len(archive)} of them an archive's rows -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
