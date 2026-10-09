"""hold.carry — a collection's local state carried into a new collection with
the same records. `louise dev carry <from> <to> [--copy] [--keep producer:<a>]`
(07 §7.3; docs/notes/v5-migration.md G1, G2; amendment A1 of 02 §2.4/§2.7 and
03 §3.5).

RULE 1. A version-5/6 Codex is a new collection, and a by-the-book one would
re-derive its manifest, its text index and its vector table from records that
are byte for byte the old collection's. A1 lets a vector table cross into a
collection with the same records whose space differs ONLY by engine, or by a
declared input form equal to the one its vectors were produced in — once
`vectors-sample` under a validated run passes at the target's tolerance, before
use. So this moves (or with `--copy` copies) the three local things —
`state/manifest.sqlite` (after a TRUNCATE checkpoint), `index/`, `lance/` —
re-pins the manifest's `space` row to the target's Codex, and clears the
source's coverage rows (they name the source's collection and space).

What it refuses (nothing is written): a record-deciding difference
(hold/seed.py's agreement); a space that differs in anything but the engine
and the layers' input forms (weights, dim, dtype, ctx, pooling, normalise, the
query side, the epoch); a target whose latest snapshot's parts are not the
source's; a source table pinned to another space or of another width; a
non-empty target. The caller holds BOTH collections' write locks.

`keep_producer` (G2) then drops every vector that is not an archive's: rows
whose chunk has a `chunk_verification` row of that producer in state
`imported` or `verified` stay; every other row is deleted through
`embeddings.delete_chunks` and its document's `embed_done` reset, a window of
documents at a time, each window in three steps that can each be repeated —
the reset committed first (the safe direction: a document marked not done
whose vectors still exist is re-embedded, never lost), the rows deleted under
the vector write lock, the cursor committed last. Never one transaction over
the corpus (db.py's apply_reset says what that did on pubmed).
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .codex import Codex, CodexError
from .seed import latest_snapshot, refuse_unless_same_records

MARKER = "carry.json"
MANIFEST = "manifest.sqlite"
#: SQLite's companions of a database file, moved with it.
_COMPANIONS = ("-wal", "-shm")
#: chunk_verification states that are an archive's vectors (ledger.py).
KEPT_STATES = ("imported", "verified")
KEEP_KEY = "carry_keep:"
WINDOW = 500


class CarryMismatch(CodexError):
    """Carried, and the vector table does not hold what the keep says it should."""
    exit_code = 1


# ---- what must agree -------------------------------------------------------

def space_differences(a: Codex, b: Codex) -> list[str]:
    """A1: the space may differ by ENGINE and by the layers' input forms, and
    by nothing else that makes the function. `[]` when it is so."""
    def fn(c: Codex) -> dict[str, Any]:
        s = c.space
        q = s.query
        return {"encoder.weights": s.encoder.weights, "dim": s.dim, "dtype": s.dtype,
                "ctx": s.ctx, "pooling": s.pooling, "normalise": s.normalise,
                "query.encoder.weights": q.encoder.weights if q.encoder else None,
                "query.instruction": q.instruction, "epoch": s.epoch,
                "cleaning_version": c.cleaning_version}
    fa, fb = fn(a), fn(b)
    return [k for k in fa if fa[k] != fb[k]]


def _part_entries(snap) -> dict[str, list]:
    return {"shards": sorted((s.slice, s.n, s.root, s.records_root, s.chunks_root, s.records,
                              s.chunks, s.bytes, s.first_id, s.last_id) for s in snap.shards),
            "relations": sorted((r.slice, r.n, r.root, r.edges, r.bytes) for r in snap.relations),
            "figures": sorted((f.slice, f.n, f.root, f.figures, f.bytes) for f in snap.figures)}


# ---- the files -------------------------------------------------------------

def _paths(root: Path, name: str) -> dict[str, Path]:
    base = Path(root) / name
    return {"manifest": base / "state" / MANIFEST, "index": base / "index",
            "lance": base / "lance", "state": base / "state"}


def _empty_dir(p: Path) -> bool:
    return not p.exists() or (p.is_dir() and not any(p.iterdir()))


def _documents(manifest: Path) -> int | None:
    if not manifest.is_file():
        return None
    con = sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    except sqlite3.OperationalError:
        return 0                                    # no documents table: nothing held
    finally:
        con.close()


def _pin(manifest: Path) -> tuple[str | None, int | None]:
    con = sqlite3.connect(f"file:{manifest.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT space_id, dim FROM space WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return (row[0], row[1]) if row else (None, None)


def _copy_tree(src: Path, dst: Path) -> None:
    """A reflink where the filesystem has one (`cp --reflink=auto`), else a
    copy; `dst` must not exist."""
    cp = shutil.which("cp")
    if cp:
        r = subprocess.run([cp, "-a", "--reflink=auto", str(src), str(dst)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        if dst.exists():
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def _transfer(src: Path, dst: Path, *, copy: bool) -> str:
    """`src` to `dst`, whole or not at all: a rename where it can, else a copy
    to a hidden name renamed into place (and, moving, the source removed
    after). Repeatable: a `dst` already there is what an earlier run put."""
    if dst.exists() and not _empty_dir(dst):
        if not copy and src.exists():
            # a move that crossed devices was interrupted removing its source
            shutil.rmtree(src) if src.is_dir() else src.unlink()
        return "present"
    if not src.exists():
        return "present" if dst.exists() else "absent"
    if dst.is_dir():
        dst.rmdir()                                   # empty: db.connect makes them
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not copy:
        try:
            os.replace(src, dst)
            return "moved"
        except OSError:
            pass                                       # another device: copy, then remove
    tmp = dst.with_name(f".{dst.name}.carry")
    if tmp.exists():
        shutil.rmtree(tmp) if tmp.is_dir() else tmp.unlink()
    _copy_tree(src, tmp)
    os.replace(tmp, dst)
    if copy:
        return "copied"
    shutil.rmtree(src) if src.is_dir() else src.unlink()
    return "moved"


def _checkpoint(manifest: Path) -> None:
    """Everything the WAL holds into the main file, the WAL truncated — the
    manifest is then one file, and the move cannot leave half of it behind."""
    con = sqlite3.connect(manifest, timeout=60)
    try:
        busy, _log, _done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        con.close()
    if busy:
        raise CodexError(f"{manifest}: the checkpoint could not complete (a reader or "
                         f"writer holds it); stop whatever uses the collection and retry")


# ---- G1 --------------------------------------------------------------------

def carry(root, from_ref: str, to_ref: str, *, copy: bool = False, apply: bool = False,
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Carry `from_ref`'s manifest, text index and vector table into `to_ref`
    (G1). `apply=False` checks everything and writes nothing."""
    from . import collection as col
    from app import spaces

    root = Path(root)
    from_name, src = col.resolve(root, from_ref)
    to_name, to = col.resolve(root, to_ref)
    refuse_unless_same_records(src, to, "carry")
    diff = space_differences(src, to)
    if diff:
        raise CodexError(
            f"carry: the spaces differ in {', '.join(diff)}. Amendment A1 lets a vector "
            f"table cross into a space that differs only by ENGINE or by a declared input "
            f"form (02 §2.4, 03 §3.5); any other difference is another function, and its "
            f"vectors are embedded again, never carried")
    a, b = latest_snapshot(root, from_name), latest_snapshot(root, to_name)
    if _part_entries(a) != _part_entries(b):
        raise CodexError(
            f"carry: {to_name}'s latest snapshot ({b.snapshot_id}) does not name the parts "
            f"{from_name}'s ({a.snapshot_id}) names — seed it first "
            f"(`hold snapshot create {to_name} --shards-from {from_name}`)")

    sp, tp = _paths(root, from_name), _paths(root, to_name)
    marker = tp["state"] / MARKER
    claim = {"from": src.collection_id, "to": to.collection_id,
             "mode": "copy" if copy else "move"}
    held = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else None
    if held is not None and {k: held.get(k) for k in claim} != claim:
        raise CodexError(f"{marker} records another carry ({held}); this one is {claim}")

    plan: dict[str, Any] = {**claim, "from_name": from_name, "to_name": to_name,
                            "space_id": to.space_id, "apply": apply}
    if held is None:
        if not sp["manifest"].is_file():
            raise CodexError(f"carry: {from_name} has no manifest at {sp['manifest']}")
        pinned, _dim = _pin(sp["manifest"])
        lance_dim = spaces.known_lance_dim(sp["lance"])
        if pinned not in (None, src.space_id) or (pinned is None and lance_dim is not None):
            raise CodexError(
                f"carry: {from_name}'s vector table is pinned to {str(pinned)[:23]}…, not its "
                f"Codex's space {src.space_id[:23]}…; nothing says what its vectors are")
        if lance_dim not in (None, src.space.dim):
            raise CodexError(f"carry: {from_name}'s vector table is {lance_dim}-dimensional "
                             f"and its space is {src.space.dim}")
        docs = _documents(tp["manifest"])
        busy = [k for k in ("index", "lance") if not _empty_dir(tp[k])]
        if docs or busy:
            raise CodexError(
                f"carry: {to_name} is not empty ({'its manifest holds documents' if docs else ''}"
                f"{', ' if docs and busy else ''}{', '.join(busy)}); a carry never overwrites")
        plan["documents"] = _documents(sp["manifest"])
        if not apply:
            log(f"[carry] {from_name} → {to_name}: would {claim['mode']} the manifest "
                f"({plan['documents']} documents), index/ and lance/, and re-pin to "
                f"{to.space_id[:23]}…; nothing written (--yes)")
            return plan
        _checkpoint(sp["manifest"])
        for suffix in ("", *_COMPANIONS):            # an empty target's own manifest goes
            Path(f"{tp['manifest']}{suffix}").unlink(missing_ok=True)
        tp["state"].mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(claim, sort_keys=True), encoding="utf-8")
    elif not apply:
        log(f"[carry] {from_name} → {to_name}: begun earlier; --yes finishes it")
        return {**plan, "begun": True}

    done: dict[str, str] = {}
    for k in ("lance", "index"):
        done[k] = _transfer(sp[k], tp[k], copy=copy)
    # Moving: the companions before the main file — the main file in place is
    # what says the manifest arrived — so none is left beside a source that no
    # longer has its database. After the checkpoint the WAL is empty, so a copy
    # takes the main file alone (a copied `-shm` would describe another file).
    if not copy:
        for suffix in _COMPANIONS:
            _transfer(Path(f"{sp['manifest']}{suffix}"), Path(f"{tp['manifest']}{suffix}"),
                      copy=False)
    done["manifest"] = _transfer(sp["manifest"], tp["manifest"], copy=copy)
    if not tp["manifest"].is_file():
        raise CodexError(f"carry: {tp['manifest']} is missing after the transfer")

    cleared = _repin(tp["manifest"], src, to)
    plan.update(done=done, coverage_rows_cleared=cleared, documents=_documents(tp["manifest"]))
    marker.write_text(json.dumps({**claim, "done": True}, sort_keys=True), encoding="utf-8")
    log(f"[carry] {from_name} → {to_name}: manifest {done['manifest']}, index/ "
        f"{done['index']}, lance/ {done['lance']}; space re-pinned to "
        f"{to.space_id[:23]}…; {cleared} coverage row(s) of {from_name} cleared. Before "
        f"use: `hold check {to_name} --checks vectors-sample` (amendment A1)")
    return plan


def _repin(manifest: Path, src: Codex, to: Codex) -> int:
    """The `space` row to `to`'s Codex, and `src`'s coverage rows gone, in
    one transaction. Repeatable: a second run finds nothing to clear."""
    from app.share import coverage as cov
    con = sqlite3.connect(manifest, timeout=60, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO space(id, space_id, dim, components, pinned_at) "
                    "VALUES(1, ?, ?, NULL, ?) ON CONFLICT(id) DO UPDATE SET "
                    "space_id=excluded.space_id, dim=excluded.dim, "
                    "components=NULL, pinned_at=excluded.pinned_at",
                    (to.space_id, to.space.dim, int(time.time())))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cleared = 0
        prefix = cov.shard_id(src, "")                # "<collection_id>/<space>/"
        like = prefix.replace("%", r"\%").replace("_", r"\_") + "%"
        for t in ("coverage", "coverage_shards"):
            if t in tables:
                cleared += con.execute(f"DELETE FROM {t} WHERE shard LIKE ? ESCAPE '\\'",
                                       (like,)).rowcount
        con.execute("COMMIT")
        return cleared
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# ---- G2 --------------------------------------------------------------------

def producer_of(keep: str) -> str:
    """`producer:<archive>` → the producer an archive's import records in
    `chunk_verification` (`genesis:<archive>`, app/bundles/genesis.py); a value
    already carrying its kind (`genesis:…`, `ed25519:…`) is taken as it is."""
    kind, sep, value = keep.partition(":")
    if kind != "producer" or not sep or not value:
        raise CodexError(f"--keep is producer:<archive>, got {keep!r}")
    return value if ":" in value else f"genesis:{value}"


def keep_producer(root, to_ref: str, producer: str, *, window: int = WINDOW,
                  log: Callable[[str], None] = print) -> dict[str, Any]:
    """Keep only the vectors `producer` produced (G2). Resumable from its
    cursor in the manifest's `control` table."""
    from . import collection as col, records as rec
    from app import db, embeddings

    root = Path(root)
    name, codex = col.resolve(root, to_ref)
    coll = rec.local_collection(root, name, codex)
    key = KEEP_KEY + producer
    con = db.connect(coll)
    try:
        state = json.loads(db.get_control(con, key) or "{}") or \
            {"rowid": 0, "kept": 0, "dropped": 0, "documents_reset": 0, "done": False}
        while not state.get("done"):
            rows = con.execute("SELECT rowid, uid, native_id FROM documents WHERE rowid > ? "
                               "ORDER BY rowid LIMIT ?", (state["rowid"], window)).fetchall()
            if not rows:
                state["done"] = True
                con.execute("BEGIN IMMEDIATE")
                db.set_control(con, key, json.dumps(state, sort_keys=True))
                con.execute("COMMIT")
                break
            last = int(rows[-1]["rowid"])
            try:
                books = sorted({int(r["native_id"]) for r in rows})
            except ValueError:
                raise CodexError(
                    f"{name}: a native_id that is not an integer in rowids "
                    f"{rows[0]['rowid']}–{last}; the vector table keys rows on numeric "
                    f"book ids (embeddings.delete_chunks), so this collection's rows "
                    f"cannot be dropped by chunk here") from None
            present = set(embeddings.chunk_ids_of_books(books, collection=coll))
            uids = [r["uid"] for r in rows]
            marks = ",".join("?" * len(uids))
            kept = {r[0] for r in con.execute(
                f"SELECT chunk_id FROM chunk_verification WHERE uid IN ({marks}) "
                f"AND producer=? AND state IN ({','.join('?' * len(KEPT_STATES))})",
                (*uids, producer, *KEPT_STATES))}
            drop = sorted(present - kept)
            pending = state.get("pending") or {}
            if pending.get("rowid") != last:
                # step 1: the documents marked not done FIRST, with the count
                drop_uids = sorted({c.rsplit(":", 2)[0] for c in drop})
                con.execute("BEGIN IMMEDIATE")
                for i in range(0, len(drop_uids), 500):
                    part = drop_uids[i:i + 500]
                    con.execute(f"UPDATE documents SET embed_done=0 WHERE uid IN "
                                f"({','.join('?' * len(part))})", part)
                for i in range(0, len(drop), 500):
                    part = drop[i:i + 500]
                    con.execute(f"DELETE FROM chunk_verification WHERE chunk_id IN "
                                f"({','.join('?' * len(part))})", part)
                pending = {"rowid": last, "dropped": len(drop),
                           "documents_reset": len(drop_uids)}
                db.set_control(con, key, json.dumps({**state, "pending": pending},
                                                     sort_keys=True))
                con.execute("COMMIT")
            # step 2: the rows, under the vector write lock
            embeddings.delete_chunks(drop, collection=coll, lock_con=con)
            # step 3: the cursor
            state = {"rowid": last, "kept": state["kept"] + len(present & kept),
                     "dropped": state["dropped"] + pending["dropped"],
                     "documents_reset": state["documents_reset"] + pending["documents_reset"],
                     "done": False}
            con.execute("BEGIN IMMEDIATE")
            db.set_control(con, key, json.dumps(state, sort_keys=True))
            con.execute("COMMIT")
    finally:
        con.close()
    rows_now = embeddings.stats(coll)["vectors"]
    out = {"collection": name, "producer": producer, "kept": state["kept"],
           "dropped": state["dropped"], "documents_reset": state["documents_reset"],
           "vectors": rows_now, "verified": rows_now == state["kept"]}
    log(f"[carry] {name}: kept {state['kept']} vector(s) of {producer}, dropped "
        f"{state['dropped']}, {state['documents_reset']} document(s) to embed again; the "
        f"table holds {rows_now}")
    if not out["verified"]:
        raise CarryMismatch(
            f"{name}: the vector table holds {rows_now} rows and {state['kept']} were kept — "
            f"rows whose book_id is no document of the manifest were never visited")
    return out
