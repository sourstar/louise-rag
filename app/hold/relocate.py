"""`louise hold records relocate <collection> --corpus DIR` — every record's
09 §9.5 `origin` written again from the manifest, by the build's own
function, and the parts whose bytes change rewritten by the build's own
writer (RULE 1).

WHY (wikipedia-v8, 2026-10-28). The archive layer's documents came into the
collection with manifest paths under `/corpora/wikivec`, outside the origin
base, and the build's locator fell back to the file's bare name —
`000.parquet#0::15` — which locates nothing and is ambiguous across the
archive's ten languages. `records-vs-origin` re-fetched none of 52,440
sampled records. The locator is fixed (`plugin_io.relative_to_base`: a path
reaches the base through a link the base holds, or the record is refused);
the records already built carry the old origins.

A rebuild re-parses 74.8 M records over days to change one field. This
recomputes that field instead, with exactly what the build computes it with
— `plugin_io.origin_locator(plugin, <the manifest's path>, <the origin
base>)` — and rewrites a part with `pack.records.write_shard`, the build's
writer. The part is then the part a rebuild would write, byte for byte: the
same lines in the same order, one field corrected, the same frames and
compression. Checked per part before anything is replaced: the same uids in
the same order, the same `records_root` (the content does not move), the
same record and chunk counts. Only `root`, `bytes` and `sha256` change in the
build database, where `hold snapshot create` reads them.

REFUSED while a snapshot lists the parts: a snapshot's parts never change
(02 §2.2). A snapshot that was never shared is removed first, by its
operator; one that was shared is superseded by the next period's, never
rewritten.
"""
from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .codex import CodexError


@dataclass
class Result:
    parts: int = 0               # parts read
    rewritten: int = 0           # parts whose bytes changed
    records: int = 0             # records read
    relocated: int = 0           # records whose origin changed
    examples: tuple = ()         # a few (uid, old, new), for the log


#: per child: the manifest connection and the plugin, opened once
_child: dict = {}


def _init(name: str, plugin_id: str, corpus: str, manifest: str, record_line: int) -> None:
    from app import collections as collections_mod
    plugin = collections_mod.PLUGIN_REGISTRY[plugin_id]
    _child.update(plugin=plugin, corpus=Path(corpus), record_line=record_line,
                  con=sqlite3.connect(f"file:{manifest}?mode=ro", uri=True))


def _relocate_part(path: str, dry_run: bool) -> dict:
    """One part: its records with their origins recomputed; rewritten when one
    changed. Runs in a child; returns what the parent records."""
    from app import plugin_io
    from app.pack import records as pack_records
    from . import records as hold_records, shard as shard_mod

    plugin, corpus, con = _child["plugin"], _child["corpus"], _child["con"]
    records = list(pack_records.iter_records(Path(path)))
    # THE PART'S CHUNK TOTAL is in its index and in no line (`chunks` is not
    # a record key): the writer sums each record's `chunks`, so the total
    # rides on the first record — the lines are unchanged by it
    total_chunks = pack_records.read_index(Path(path)).chunks
    changed, examples = 0, []
    for r in records:
        row = con.execute("SELECT path FROM documents WHERE uid=?", (r["uid"],)).fetchone()
        if row is None:
            raise CodexError(f"{r['uid']}: in part {path} but not in the manifest; "
                             f"its origin cannot be recomputed")
        new = plugin_io.origin_locator(plugin, row[0], corpus)
        if new != r.get("origin"):
            if len(examples) < 3:
                examples.append((r["uid"], r.get("origin"), new))
            r["origin"] = new
            changed += 1
    out = {"path": path, "records": len(records), "changed": changed, "examples": examples}
    if not changed or dry_run:
        return out
    before_root = hold_records.records_root(r["content_hash"] for r in records)
    for i, r in enumerate(records):
        r["chunks"] = total_chunks if i == 0 else 0
    tmp = Path(path).with_name(Path(path).name + ".relocated")
    info = pack_records.write_shard(tmp, records, record_line=_child["record_line"])
    check = list(pack_records.iter_records(tmp))
    if [r["uid"] for r in check] != [r["uid"] for r in records]:
        tmp.unlink(missing_ok=True)
        raise CodexError(f"{path}: the rewritten part's uids differ; nothing replaced")
    if hold_records.records_root(r["content_hash"] for r in check) != before_root:
        tmp.unlink(missing_ok=True)
        raise CodexError(f"{path}: the rewritten part's records_root moved; nothing replaced")
    out.update(bytes=info.bytes, sha256=info.sha256, records_written=info.records,
               chunks=info.chunks, records_root=before_root,
               root=shard_mod.pieces_root_of(tmp), tmp=str(tmp))
    return out


def relocate(root, ref: str, *, corpus_dir, workers: int | None = None,
             dry_run: bool = False, log=print) -> Result:
    from app import config
    from . import collection as col, records as hold_records

    name, codex = col.resolve(Path(root), ref)
    shard_dir, state_dir, staging = hold_records._paths(root, name)
    snaps = sorted((shard_dir / "snapshots").glob("*.json")) if (shard_dir / "snapshots").is_dir() else []
    if snaps and not dry_run:
        raise CodexError(
            f"{name}: {len(snaps)} snapshot file(s) list these parts "
            f"({', '.join(p.name for p in snaps)}), and a snapshot's parts never change "
            f"(02 §2.2). One never shared is removed by its operator first; one shared "
            f"is superseded by the next period's")
    db_path = staging / ".build" / "build.db"
    if not db_path.is_file():
        raise CodexError(f"{name}: no build database at {db_path}")
    local = hold_records.local_collection(root, name, codex, corpus_dir=corpus_dir)
    from app import db
    bdb = sqlite3.connect(db_path)
    db.wal(bdb)   # the build made it WAL; a no-op that keeps it so (invariant 6)
    rows = bdb.execute("SELECT slice, n, path, records, chunks, records_root FROM shards "
                       "ORDER BY slice, n").fetchall()
    result = Result()
    examples: list = []
    n = max(1, workers or config.available_cpus())
    init = (name, codex.plugin, str(corpus_dir), str(local.manifest_path),
            int(getattr(codex, "record_line", 1) or 1))
    expected = {str(shard_dir / r[2]): r for r in rows}
    # SPAWNED, as the parse pool's children are (app/parallel.py): a fork of a
    # process holding threads and SQLite connections is undefined
    import multiprocessing
    with ProcessPoolExecutor(max_workers=n, initializer=_init, initargs=init,
                             mp_context=multiprocessing.get_context("spawn")) as pool:
        for got in pool.map(_relocate_part, list(expected), [dry_run] * len(expected), chunksize=4):
            result.parts += 1
            result.records += got["records"]
            result.relocated += got["changed"]
            examples.extend(got["examples"][:max(0, 3 - len(examples))])
            if not got["changed"] or dry_run:
                continue
            slice_, num, rel, n_rec, n_chunks, records_root = expected[got["path"]]
            if (got["records_written"], got["chunks"], got["records_root"]) != (n_rec, n_chunks, records_root):
                Path(got["tmp"]).unlink(missing_ok=True)
                raise CodexError(f"{rel}: counts or records_root differ from the build's row; "
                                 f"nothing replaced")
            os.replace(got["tmp"], got["path"])
            bdb.execute("UPDATE shards SET bytes=?, sha256=?, root=? WHERE slice=? AND n=?",
                        (got["bytes"], got["sha256"], got["root"], slice_, num))
            bdb.commit()
            result.rewritten += 1
            if result.rewritten % 500 == 0:
                log(f"[records relocate] {name}: {result.rewritten} part(s) rewritten, "
                    f"{result.relocated:,} record(s) relocated, {result.parts} of {len(rows)} read")
    bdb.close()
    result.examples = tuple(examples)
    for uid, old, new in examples:
        log(f"[records relocate] {name}: {uid}: {old!r} -> {new!r}")
    log(f"[records relocate] {name}: {result.parts} part(s) read, {result.records:,} record(s); "
        f"{result.relocated:,} relocated in {result.rewritten} part(s)"
        + (" (dry run: nothing written)" if dry_run else ""))
    return result
