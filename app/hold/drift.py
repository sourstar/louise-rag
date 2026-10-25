"""What this node SERVES against what its latest snapshot's records say —
`louise dev index-drift` (07 §7.3). Read-only.

The 127-part gap (docs/notes/STATUS.md, 2026-10-25): a gutenberg node's
`documents.chunks`, and the text index they were counted from, came from an
earlier parse while the SHARD's records had moved on, and `fts_version`
stayed 1, so the worker never re-indexed them. Invariant 3 held — re-cutting
the records reproduces the snapshot's part totals exactly — and nothing
noticed: `hold check --checks chunks` re-cuts the records against the
snapshot, never against the manifest or the text index. This compares the
two sides, document by document, and names the ones that disagree:

  chunks        the manifest's chunk count is not the record's own cut
  index         the text index holds another number of chunks for the
                document than the manifest says (or than the record cuts)
  text          on the seeded sample of parts (`--sample`): the text index's
                stored chunks — id, heading, text — are not the record's cut
  content_hash  the manifest's `content_hash` is not the record's: the row
                predates the record (a later snapshot revised it)
  origin        the manifest's `txt_hash` (the live origin bytes) is not the
                record's `origin_hash`: this node's origin is another
                revision, so re-indexing from the live parse cannot reach
                the record

`--ids-out` lists the documents `louise dev reindex --ids` repairs — those
named for chunks, index or text and whose live origin is not known to have
moved. content_hash and origin are named in the report and left out of the
file: `reindex` re-cuts from the live parse and rewrites neither column.

COST. A window is `WINDOW` records streamed from one part: one indexed
`uid IN (…)` read of their manifest rows (900 a statement, the primary
key), one term-set aggregation per text-index group for their chunk counts
(`fts.chunk_counts`), and nothing else. Cutting records — the costly step
(the chunker over every section) — runs only for a part whose manifest
total is not the snapshot's `chunks` for that part (09 §9.4, which the cut
reproduces exactly), or which has records the node does not serve, or
everywhere with `cut_all`. So a node that agrees reads its parts once and
cuts nothing. Every cost is per record walked; nothing scales with what was
walked before it. Measured in tests/hold/test_drift.py's timing and
docs/notes/STATUS.md (2026-10-25).
"""
from __future__ import annotations

import itertools
import json
import os
import sqlite3
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .codex import CodexError

#: records a window: the manifest read, the index aggregation and the cut
#: hold one window in memory, never a part (a wikipedia part is 50k records)
WINDOW = 2_000
#: host parameters a statement — under SQLite's 999 default on old builds
SQL_BATCH = 900
#: the kinds `dev reindex` repairs; the others are named and left out
REINDEXABLE = ("chunks", "index", "text")
#: at most this many named documents in the printed report; `ids_out` has all
PRINT_LIMIT = 1_000

ROWS_SQL = ("SELECT uid, native_id, chunks, txt_hash, content_hash, in_scope, rdf_done, fts_done, "
            "fts_version, entity_version, updated_at, error FROM documents WHERE uid IN ({})")
#: rows the node serves from its text index: an index prefix, counted on the
#: covering index without reading a row
SERVED_COUNT_SQL = "SELECT COUNT(*) FROM documents WHERE in_scope=1 AND rdf_done=1 AND fts_done=1"


# ---- shared with tools/measure/chunks_drift.py ------------------------------

def record_chunks(codex, rec: dict) -> int:
    """How many chunks the record cuts into, by the one path every stage cuts
    by (invariant 1) — `hold.land.cut`'s. A record line carries no count."""
    return len(record_cut(codex, rec))


def record_cut(codex, rec: dict) -> list[dict]:
    from app import layers as layers_mod
    row = {"uid": rec["uid"], "native_id": rec.get("native_id", 0)}
    return layers_mod.chunks_for_document(codex, row, [tuple(x) for x in rec.get("sections") or []])


def drift(con, records: list[dict], count: Callable[[dict], int]) -> dict:
    """A part's records (`count(record)` -> its chunks) against the manifest
    rows of the same uids — chunks_drift's per-part report, unchanged."""
    uids = [r["uid"] for r in records]
    rows: dict[str, dict] = {}
    for i in range(0, len(uids), SQL_BATCH):
        part = uids[i:i + SQL_BATCH]
        for row in con.execute(
                "SELECT uid, chunks, fts_version, entity_version, updated_at, in_scope, error FROM documents "
                f"WHERE uid IN ({','.join('?' * len(part))})", part):
            rows[row[0]] = {"chunks": row[1], "fts_version": row[2], "entity_version": row[3],
                            "updated_at": row[4], "in_scope": row[5], "error": row[6]}
    differs = []
    total = 0
    for r in records:
        m = rows.get(r["uid"])
        want = int(count(r))
        total += want
        have = None if m is None else int(m["chunks"] or 0)
        if have != want:
            differs.append({"uid": r["uid"], "record_chunks": want, "manifest_chunks": have,
                            **({k: v for k, v in m.items() if k != "chunks"} if m else {"missing": True})})
    return {"records": len(records), "record_chunks": total,
            "manifest_chunks": sum(int((rows.get(r["uid"]) or {}).get("chunks") or 0) for r in records),
            "differs": differs}


# ---- the check ---------------------------------------------------------------

def open_manifest(path: Path) -> sqlite3.Connection:
    """The manifest READ-ONLY (`mode=ro`): nothing this check runs can write
    it, not even a schema ensure."""
    if not Path(path).is_file():
        raise CodexError(f"no manifest at {path}: this node serves nothing of the collection")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    return con


def manifest_rows(con, uids: list[str]) -> dict[str, sqlite3.Row]:
    out: dict[str, sqlite3.Row] = {}
    for i in range(0, len(uids), SQL_BATCH):
        part = uids[i:i + SQL_BATCH]
        for row in con.execute(ROWS_SQL.format(",".join("?" * len(part))), part):
            out[row["uid"]] = row
    return out


def _pending_reason(row, fts_version: int, entity_version: int) -> str | None:
    """Why the node does not serve this row from the record's side yet, or
    None when it does: the worker owns these, and they are counted, not named."""
    if not row["in_scope"]:
        return "out_of_scope"
    if row["error"] is not None:
        return "error"
    if not row["rdf_done"]:
        return "not_extracted"
    if not row["fts_done"]:
        return "not_indexed"
    if (row["fts_version"] or 0) < fts_version or (row["entity_version"] or 0) < entity_version:
        return "queued"           # below the current versions: the worker re-indexes it itself
    return None


def _windows(it: Iterable[dict], n: int) -> Iterator[list[dict]]:
    it = iter(it)
    while chunk := list(itertools.islice(it, n)):
        yield chunk


def _named(found: dict[str, dict], uid: str, part: str, kind: str, row, **detail) -> None:
    e = found.get(uid)
    if e is None:
        e = found[uid] = {"uid": uid, "part": part, "kinds": [],
                          "manifest_chunks": int(row["chunks"] or 0),
                          "fts_version": row["fts_version"], "entity_version": row["entity_version"],
                          "updated_at": row["updated_at"], "fts_done": row["fts_done"]}
    if kind not in e["kinds"]:
        e["kinds"].append(kind)
    e.update(detail)


def check_part(con, local, codex, records: Callable[[], Iterable[dict]], *, part: str, part_chunks: int,
               cut_all: bool = False, text: bool = False, window: int = WINDOW,
               fts_version: int | None = None, entity_version: int | None = None) -> dict:
    """One part: `records()` streams its record lines (called again for the
    cut pass, never held whole). Returns the part's counts and its named
    documents (`found`, uid -> entry)."""
    from app import config, fts
    fv = config.FTS_VERSION if fts_version is None else fts_version
    ev = config.ENTITY_VERSION if entity_version is None else entity_version
    found: dict[str, dict] = {}
    pending: Counter = Counter()
    n_records = served = missing = manifest_sum = indexed = changed = 0
    #: uid -> (book_id, manifest chunks, (updated_at, fts_done)) as the first pass read it
    served_rows: dict[str, tuple[int, int, tuple]] = {}
    index_of: dict[str, int] = {}
    for win in _windows(records(), window):
        rows = manifest_rows(con, [r["uid"] for r in win])
        ids: list[int] = []
        for r in win:
            n_records += 1
            row = rows.get(r["uid"])
            if row is None:
                missing += 1
                continue
            indexed += int(bool(row["in_scope"] and row["rdf_done"] and row["fts_done"]))   # SERVED_COUNT_SQL's
            why = _pending_reason(row, fv, ev)
            if why:
                pending[why] += 1
                continue
            served += 1
            book = int(row["native_id"])
            served_rows[r["uid"]] = (book, int(row["chunks"] or 0), (row["updated_at"], row["fts_done"]))
            manifest_sum += int(row["chunks"] or 0)
            ids.append(book)
            if row["content_hash"] and r.get("content_hash") and row["content_hash"] != r["content_hash"]:
                _named(found, r["uid"], part, "content_hash", row,
                       manifest_content_hash=row["content_hash"], record_content_hash=r["content_hash"])
            if row["txt_hash"] and r.get("origin_hash") and row["txt_hash"] != r["origin_hash"]:
                _named(found, r["uid"], part, "origin", row,
                       txt_hash=row["txt_hash"], record_origin_hash=r["origin_hash"])
        counts = fts.chunk_counts(local, ids)
        for r in win:
            got = served_rows.get(r["uid"])
            if got is None:
                continue
            book, have, _when = got
            index_of[r["uid"]] = counts.get(book, 0)
            if index_of[r["uid"]] != have:
                _named(found, r["uid"], part, "index", rows[r["uid"]], index_chunks=index_of[r["uid"]])
    whole = served == n_records
    cut = cut_all or text or not whole or manifest_sum != part_chunks
    record_sum = None
    if cut and served:
        record_sum = 0
        for win in _windows(records(), window):
            mine = [r for r in win if r["uid"] in served_rows]
            stored = fts.stored_chunks(local, [served_rows[r["uid"]][0] for r in mine]) if text else {}
            rows = manifest_rows(con, [r["uid"] for r in mine]) if mine else {}
            for r in mine:
                book, have, when = served_rows[r["uid"]]
                row = rows.get(r["uid"])
                if row is None or (row["updated_at"], row["fts_done"]) != when:
                    changed += 1      # the worker moved it since the first pass: not judged here
                    found.pop(r["uid"], None)
                    continue
                pieces = record_cut(codex, r)
                want = len(pieces)
                record_sum += want
                if want != have:
                    _named(found, r["uid"], part, "chunks", row, record_chunks=want)
                if index_of.get(r["uid"], 0) != want:
                    _named(found, r["uid"], part, "index", row,
                           index_chunks=index_of.get(r["uid"], 0), record_chunks=want)
                if text:
                    cut_ = sorted((c["id"], c["chapter"] or "", c["text"]) for c in pieces)
                    if stored.get(book, []) != cut_:
                        _named(found, r["uid"], part, "text", row, record_chunks=want)
    return {"part": part, "records": n_records, "served": served, "missing": missing, "indexed": indexed,
            "changed": changed,
            "pending": dict(pending), "snapshot_chunks": part_chunks, "manifest_chunks": manifest_sum,
            "record_chunks": record_sum, "cut": bool(cut and served), "text": bool(text and served),
            "found": found}


def run(root, name: str, codex, snapshot, local, *, shards: list[str] | None = None, cut_all: bool = False,
        sample: str | None = "0.05", seed: bytes | None = None, window: int = WINDOW,
        log: Callable[[str], None] = lambda _m: None, every_s: float = 30.0,
        stop: Callable[[], bool] = lambda: False) -> dict:
    """Walk `snapshot`'s parts (or `shards`) part by part, window by window;
    the report, with `ids` — the uids `dev reindex` repairs — and `took_s`.
    `stop()` true ends the walk at the next part (07 §7.7): the report then
    covers the parts walked and says `interrupted`."""
    from app.pack import records as pack_records
    from . import codex as codex_mod, collection as col
    t0 = time.monotonic()
    by_id = {f"{s.slice}/{s.n:05d}": s for s in snapshot.shards}
    if shards:
        unknown = [p for p in shards if p not in by_id]
        if unknown:
            raise CodexError(f"{name}: snapshot {snapshot.snapshot_id} names no part {', '.join(unknown)}")
        walk = list(shards)
    else:
        walk = sorted(by_id)
    seed = seed if seed is not None else os.urandom(32)
    sampled: set[str] = set()
    if sample and sample != "0" and walk:
        sampled = set(codex_mod.sample(seed, codex_mod.sample_size(sample, len(walk)), walk))
    tree = col.shard_dir_of(Path(root), name)
    found: dict[str, dict] = {}
    totals: Counter = Counter()
    pending: Counter = Counter()
    not_held: list[str] = []
    last = t0
    interrupted = False
    with closing(open_manifest(Path(local.manifest_path))) as con:
        for k, part in enumerate(walk, 1):
            if stop():
                interrupted = True
                break
            s = by_id[part]
            path = tree / s.path
            if not path.is_file():
                not_held.append(part)
                continue
            got = check_part(con, local, codex, lambda p=path: pack_records.iter_records(p), part=part,
                             part_chunks=s.chunks, cut_all=cut_all, text=part in sampled, window=window)
            found.update(got.pop("found"))
            pending.update(got["pending"])
            totals.update(parts=1, records=got["records"], served=got["served"], missing=got["missing"],
                          indexed=got["indexed"], changed=got["changed"],
                          parts_cut=int(got["cut"]), parts_text=int(got["text"]),
                          parts_whose_total_differs=int(got["served"] == got["records"]
                                                        and got["manifest_chunks"] != s.chunks))
            now = time.monotonic()
            if now - last >= every_s:
                last = now
                log(f"[index-drift] {name}: {k}/{len(walk)} parts, {totals['records']:,} records, "
                    f"{len(found):,} named, {now - t0:.0f}s")
        served_rows = None
        if not shards and not not_held and not interrupted:
            served_rows = int(con.execute(SERVED_COUNT_SQL).fetchone()[0])
        # The worker may have re-indexed a named document between this walk's
        # manifest read and its index read: such a row is set aside, counted,
        # never named — a second run sees it settled. One read per named
        # document, so it costs what was found, not what was walked.
        moved = totals["changed"]
        now = manifest_rows(con, list(found)) if found else {}
        for uid, e in list(found.items()):
            row = now.get(uid)
            if row is None or (row["updated_at"], row["fts_done"]) != (e["updated_at"], e["fts_done"]):
                del found[uid]
                moved += 1
    took = time.monotonic() - t0
    named = sorted(found.values(), key=lambda e: e["uid"])
    kinds = Counter(k for e in named for k in e["kinds"])
    ids = [e["uid"] for e in named if set(e["kinds"]) & set(REINDEXABLE) and "origin" not in e["kinds"]]
    report = {
        "collection": name, "snapshot": snapshot.snapshot_id, "seed": seed.hex(), "sample": sample or "0",
        "cut_all": cut_all, "interrupted": interrupted, **{k: totals[k] for k in ("parts", "records", "served", "missing", "parts_cut",
                                                      "parts_text", "parts_whose_total_differs")},
        "parts_not_held": not_held, "pending": dict(pending),
        # rows the text index serves that no record of the snapshot accounts
        # for — a document the snapshot does not hold. Only over a whole walk
        # of a wholly held snapshot; counted, not named (no window answers it)
        "served_rows": served_rows,
        "served_without_record": None if served_rows is None else served_rows - totals["indexed"],
        "named": len(named), "kinds": dict(kinds), "reindexable": len(ids), "changed_during_walk": moved,
        "named_fts_versions": dict(Counter(str(e["fts_version"]) for e in named)),
        "documents": named[:PRINT_LIMIT], "documents_truncated": len(named) > PRINT_LIMIT,
        "took_s": round(took, 3),
        "us_per_record": round(took / totals["records"] * 1e6, 1) if totals["records"] else None,
    }
    return {**report, "ids": ids}


def write_ids(path: Path, ids: list[str]) -> None:
    """One uid a line — `louise dev reindex --ids FILE`'s input. Written
    beside and renamed into place, so a reader never sees half a list."""
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text("".join(f"{u}\n" for u in ids), encoding="utf-8")
    os.replace(tmp, path)


def summary_line(report: dict) -> str:
    return (f"[index-drift] {report['collection']} @ {report['snapshot']}: {report['records']:,} records in "
            f"{report['parts']} part(s), {report['served']:,} served; {report['named']} named "
            f"{json.dumps(report['kinds'], sort_keys=True)}, {report['reindexable']} for `dev reindex`; "
            f"{report['parts_cut']} part(s) cut, {report['parts_text']} text-compared; "
            f"{report['took_s']:.1f}s ({report['us_per_record']} µs a record)")
