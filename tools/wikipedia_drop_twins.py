"""Remove the dump-layer documents of a wikipedia collection that are a
second copy of a page its archive already holds.

WHY THIS EXISTS. wikipedia-v8's origin reaches the archive through a link
(`origin/data` -> /corpora/...), and `Path.rglob` enters no symlinked
directory: the archive's coverage read empty, and the dump pass minted a
second document for every page the archive holds — 21,888,609 of them
(2026-10-30; app/plugins/walk.py, docs/notes/STATUS.md). The walk follows
links now, so no new twin is minted; this removes the ones that were.

WHAT A TWIN IS. A dump-layer row (native id >= 10^12, app/plugins/wikipedia/
ids.py) whose archive id — the same id less 10^12, the same language and
page — is in the manifest too. The archive's row is the page's document
(06 §6.3's `archive:` rule: a dump-read page is never inside the archive's
coverage), so the twin goes and the archive's row is not touched. A twin
that is EMBEDDED is counted and kept: it has vectors the archive's row does
not explain, and removing it is a decision, not a repair.

WHAT GOES, per batch, in this order, so a stop leaves nothing that answers
for a document that is gone:
  1. its text-index entries (one commit per language group),
  2. its catalogue entry (keyed on the row's rowid, one commit),
  3. any vector of it (one held only by a crash between the vector commit
     and the manifest's mark; found by ONE read of the table's book_id
     column, not a lookup per batch),
  4. its manifest row and its rows in the per-document tables, in one
     BEGIN IMMEDIATE transaction that re-checks it is still an unembedded
     twin. A row the re-check keeps has lost its entries, so it is marked
     for its stages again (rdf_done = fts_done = 0), never left done.
Each index is read first (a term-set count, no writer): a batch with no
entry in an index takes no writer for it. The worker keeps one writer per
index for as long as it runs, so where entries exist the worker must be
stopped (`louise node stop`) — the tool says so and stops before deleting
anything of that batch. The collection's write lock is held throughout: a
paused collection's lock is free (07 §7.3), anything else is refused.

Deleting renumbers no other row (invariant 6: nothing VACUUMs a manifest),
and a deleted twin's rowid is the catalogue's key only for an entry
deleted first. SIGTERM or SIGINT finishes the batch and exits 130
(invariant 2); a re-run finds what is left — every step is idempotent.

Never runs automatically; an operator invokes it.

Usage:
  python -m tools.wikipedia_drop_twins [--collection wikipedia-v8] [--batch N] [--apply]

A dry run unless --apply: it counts the twins, the kept, and the entries
each index holds for them, and writes nothing.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np

from app import catalog, collections as collections_mod, db, embeddings, fts
from app.plugins.wikipedia.ids import DUMP_BASE

#: twins removed per round of deletes: a tantivy commit per index per batch
#: is the cost, so a batch is big; its manifest transaction stays seconds
BATCH = 50_000
#: rowids read per window of the walk
WINDOW = 200_000

#: a dump-layer row of the window whose archive twin is held — the uid
#: primary key answers the join (no TEMP B-TREE, tests/test_wikipedia_drop_twins.py)
_TWINS = ("SELECT d.rowid AS rowid, d.uid AS uid, d.native_id AS native_id, d.lang AS lang, "
          "d.embed_done AS embed_done FROM documents d "
          "JOIN documents a ON a.uid = ? || ':' || (CAST(d.native_id AS INTEGER) - ?) "
          "WHERE d.rowid > ? AND d.rowid <= ? AND CAST(d.native_id AS INTEGER) >= ?")
_DELETE = ("DELETE FROM documents WHERE uid=? AND embed_done=0 "
           "AND EXISTS (SELECT 1 FROM documents WHERE uid=?)")
#: the per-document tables, keyed on the uid (app/db.py); `events` is a log
#: and keeps what happened
_BY_UID = ("embed_queue", "summaries", "doc_keys", "chunk_verification", "issues")


class WritersBusy(Exception):
    """An index this batch has entries in is held by another process."""


@dataclass(frozen=True)
class Twin:
    rowid: int
    uid: str
    book_id: int
    lang: str
    archive_uid: str


def dump_layer_vectors(col) -> np.ndarray:
    """The dump-layer book ids the vector table holds, sorted: ONE read of
    its book_id column (`embeddings.scan_columns`)."""
    seen: list[np.ndarray] = []
    for batch in embeddings.scan_columns(col, ["book_id"]):
        ids = batch.column(0).to_numpy(zero_copy_only=False)
        ids = ids[ids >= DUMP_BASE]
        if ids.size:
            seen.append(np.unique(ids))
    return np.unique(np.concatenate(seen)) if seen else np.empty(0, dtype=np.int64)


def twins(con, source: str, *, window: int = WINDOW):
    """(twins, kept) per window of rowids: kept are the embedded ones."""
    top = con.execute("SELECT MAX(rowid) FROM documents").fetchone()[0] or 0
    for lo in range(0, top, window):
        got, kept = [], 0
        for r in con.execute(_TWINS, (source, DUMP_BASE, lo, lo + window, DUMP_BASE)):
            nid = int(r["native_id"])
            if r["embed_done"]:
                kept += 1
                continue
            got.append(Twin(int(r["rowid"]), r["uid"], nid, r["lang"] or "",
                            f"{source}:{nid - DUMP_BASE}"))
        yield got, kept


def _held(col, batch: list[Twin], vectors: np.ndarray) -> tuple[dict[str, list[int]], int, int, list[int]]:
    """What each index holds for the batch: the text-index groups with an
    entry (and their book ids), how many entries, the catalogue's entries,
    the book ids with vectors."""
    by_group: dict[str, list[int]] = defaultdict(list)
    for t in batch:
        by_group[fts.group_for(t.lang)].append(t.book_id)
    held = {g: fts.books_held(g, ids, col) for g, ids in by_group.items()}
    groups = {g: by_group[g] for g, n in held.items() if n}
    cat = catalog.rows_held(col, [t.rowid for t in batch])
    books = np.fromiter((t.book_id for t in batch), dtype=np.int64, count=len(batch))
    vec = books[np.isin(books, vectors)].tolist() if vectors.size else []
    return groups, sum(held.values()), cat, vec


def _busy(what: str, e: Exception) -> WritersBusy:
    return WritersBusy(
        f"{what}'s writer is held by another process ({type(e).__name__}: {e}). The "
        f"worker keeps one per index while it runs: stop it (`louise node stop`), run "
        f"this again, then start it. Nothing of this batch was removed from the manifest; "
        f"a re-run takes it again")


def _remove(con, col, batch: list[Twin], vectors: np.ndarray, *, apply: bool) -> Counter:
    out: Counter = Counter()
    if not batch:
        return out
    groups, text, cat, vec = _held(col, batch, vectors)
    out["text_entries"] += text
    out["catalogue_entries"] += cat
    out["with_vectors"] += len(vec)
    if not apply:
        out["removed"] += len(batch)
        return out
    for group, ids in groups.items():
        try:
            fts.delete_books(group, ids, col)
        except Exception as e:                  # noqa: BLE001 — tantivy's LockBusy is a ValueError
            raise _busy(f"the text index {group!r}", e) from None
    if cat:
        try:
            catalog.delete_rows(col, [t.rowid for t in batch])
        except catalog.CatalogUnavailable as e:
            raise _busy("the catalogue", e) from None
    if vec:
        embeddings.delete_books(vec, col, lock_con=con)
    uids = [t.uid for t in batch]
    con.execute("BEGIN IMMEDIATE")
    try:
        con.executemany(_DELETE, [(t.uid, t.archive_uid) for t in batch])
        kept = set()
        for i in range(0, len(uids), 900):
            part = uids[i:i + 900]
            kept.update(r[0] for r in con.execute(
                f"SELECT uid FROM documents WHERE uid IN ({','.join('?' * len(part))})", part))
        if kept:
            # its entries are gone: its stages run for it again
            con.executemany("UPDATE documents SET rdf_done=0, fts_done=0 WHERE uid=?",
                            [(u,) for u in sorted(kept)])
        gone = [t for t in batch if t.uid not in kept]
        for table in _BY_UID:
            con.executemany(f"DELETE FROM {table} WHERE uid=?", [(t.uid,) for t in gone])
        con.executemany("DELETE FROM chunk_hashes WHERE native_id=?", [(t.book_id,) for t in gone])
        # a chunk id is `{uid}:{seq}:{version}`: the uid's range of the key
        con.executemany("DELETE FROM verify_queue WHERE chunk_id >= ? AND chunk_id < ?",
                        [(f"{t.uid}:", f"{t.uid};") for t in gone])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    out["removed"] += len(gone)
    out["rechecked_kept"] += len(kept)
    return out


def run(con, col, *, apply: bool = False, batch: int = BATCH, window: int = WINDOW,
        stop: threading.Event | None = None, log=print) -> dict:
    """Against a resolved collection and its manifest connection. Returns the
    counts: `twins` found unembedded, `removed` (or, dry, that would be),
    `kept_embedded`, `rechecked_kept`, and the entries each index held for
    the removed; `interrupted` when `stop` ended it at a batch boundary."""
    source = col.plugin
    t0 = time.monotonic()
    log(f"{col.name}: dump-layer twins of archive pages{'' if apply else '  [DRY RUN]'}")
    vectors = dump_layer_vectors(col)
    log(f"  vector table: {vectors.size:,} dump-layer document(s) with vectors  "
        f"[{time.monotonic() - t0:.0f} s]")
    total: Counter = Counter()
    pending: list[Twin] = []
    interrupted = False

    def take(part: list[Twin]) -> None:
        total.update(_remove(con, col, part, vectors, apply=apply))
        log(f"  {total['twins']:,} twins, {total['removed']:,} "
            f"{'removed' if apply else 'would be removed'}, {total['kept_embedded']:,} kept "
            f"(embedded); entries: text {total['text_entries']:,}, catalogue "
            f"{total['catalogue_entries']:,}, vectors {total['with_vectors']:,}  "
            f"[{(time.monotonic() - t0) / 60:.1f} min]")

    for got, kept in twins(con, source, window=window):
        total["twins"] += len(got)
        total["kept_embedded"] += kept
        pending.extend(got)
        while len(pending) >= batch and not interrupted:
            take(pending[:batch])
            pending = pending[batch:]
            interrupted = stop is not None and stop.is_set()
        if interrupted:
            break
    if not interrupted and pending:
        take(pending)
    if apply:
        fts.close_all()
        catalog.close_all()
    out = dict(total) | {"interrupted": interrupted}
    for k in ("twins", "removed", "kept_embedded", "rechecked_kept", "text_entries",
              "catalogue_entries", "with_vectors"):
        out.setdefault(k, 0)
    log(f"{'interrupted at a batch boundary; a re-run takes the rest' if interrupted else 'done'}: "
        f"{out['twins']:,} twins, {out['removed']:,} {'removed' if apply else 'would be removed'}, "
        f"{out['kept_embedded']:,} kept (embedded)")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collection", default="wikipedia-v8")
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--apply", action="store_true",
                    help="delete; without it, count and write nothing")
    args = ap.parse_args(argv)
    loaded = collections_mod.load_all(skip_unreadable=True)
    cols = loaded[0] if isinstance(loaded, tuple) else loaded
    col = next((c for c in cols if c.name == args.collection), None)
    if col is None:
        raise SystemExit(f"no such collection: {args.collection!r} "
                         f"(have {[c.name for c in cols]})")
    if col.plugin != "wikipedia":
        raise SystemExit(f"{col.name} is a {col.plugin!r} collection, not wikipedia")
    from app import licensing        # invariant 9: the collection's data is used
    licensing.require_ack_or_exit(col, use="its manifest and indexes are rewritten")
    lock = None
    if args.apply:
        from app import louise
        from app.hold import lock as lock_mod
        try:
            lock = lock_mod.take(lock_mod.state_dir(louise._default_root(), col.name), col.name,
                                 "tools.wikipedia_drop_twins")
        except lock_mod.Held as e:
            print(f"wikipedia_drop_twins: {e}", file=sys.stderr)
            return e.exit_code
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        out = run(db.connect(collection=col), col, apply=args.apply, batch=args.batch, stop=stop)
    except WritersBusy as e:
        print(f"wikipedia_drop_twins: {e}", file=sys.stderr)
        return 3
    finally:
        if lock is not None:
            lock.release()
    return 130 if out["interrupted"] else 0


if __name__ == "__main__":
    sys.exit(main())
