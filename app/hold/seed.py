"""hold.seed — a SHARD seeded from another collection's byte-identical parts.

`louise hold snapshot create <to> --shards-from <from>[:<snapshot>]` (07 §7.3;
docs/notes/v5-migration.md G3). RULE 1: a new collection whose records are, by
its Codex, the records another collection already holds does not parse its
origin again. Its parts are the source snapshot's parts, linked (a hard link
where the two drags share a filesystem, a copy where they do not), each one
checked against the source snapshot's own `root` before it is put in place;
its staging claim and build database are written as a build of it would have
left them; and the snapshot that follows is the normal `snapshot create`.
The result is indistinguishable from a build: the same bytes, the same roots,
the same build database rows.

THE AGREEMENT IS THE RECORDS' (02 §2.7): every field that decides a record
byte — `plugin`, `origin`, `toolchain.platform_image`, `parser`,
`cleaning_version`, `slicing`, `license`, `privacy`, `erasure`, `modality` —
and `record_line`, `facets` and the layers' chunkers, parameters and rules
(the chunk counts and `chunks_root` the build database carries are theirs).
Anything else may differ: that is what a new space is.

Resumable and idempotent (invariant 2): a part already in place with the
entry's root is left; one with another root is refused, never overwritten; a
build database already written is compared with the snapshot, never rewritten.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .codex import Codex, CodexError

#: 09 §9.4's snapshot ids: `YYYY-MM` or `YYYY-Www`.
SNAPSHOT_ID_RE = re.compile(r"^\d{4}-(\d{2}|W\d{2})$")

#: The shard entry's fields the build database carries, as (snapshot, build db).
_ROW_FIELDS = (("root", "root"), ("records_root", "records_root"),
               ("chunks_root", "chunks_root"), ("first_id", "first_native_id"),
               ("last_id", "last_native_id"), ("records", "records"),
               ("chunks", "chunks"), ("bytes", "bytes"))


def split_ref(ref: str) -> tuple[str, str | None]:
    """`<from>[:<snapshot>]`. A collection id carries a colon of its own
    (`sha256:…`), so only a trailing segment that IS a snapshot id splits."""
    head, sep, tail = ref.rpartition(":")
    if sep and SNAPSHOT_ID_RE.match(tail):
        return head, tail
    return ref, None


def record_differences(a: Codex, b: Codex) -> list[str]:
    """What decides a record byte and differs between two Codexes: 02 §2.7's
    list, `record_line`, `facets`, and each layer's chunker (id, version,
    params) and rule. `[]` means their records are the same records."""
    def fields(c: Codex) -> dict[str, Any]:
        return {
            "plugin": c.plugin,
            "origin": c.origin.to_dict(),
            "toolchain.platform_image": c.toolchain.platform_image,
            "parser": c.parser.to_dict(),
            "cleaning_version": c.cleaning_version,
            "slicing": c.slicing,
            "license": c.license.to_dict(),
            "privacy": c.privacy,
            "erasure": c.erasure,
            "modality": c.modality,
            "record_line": int(c.record_line or 1),
            "facets": [f.to_dict() for f in c.facets],
            "layers": [(l.chunker.to_dict(), l.rule) for l in c.layers],
        }
    fa, fb = fields(a), fields(b)
    return [k for k in fa if fa[k] != fb[k]]


def refuse_unless_same_records(src: Codex, dst: Codex, what: str) -> None:
    if src.collection_id == dst.collection_id:
        raise CodexError(f"{what}: the source and the target are one collection")
    diff = record_differences(src, dst)
    if diff:
        raise CodexError(
            f"{what}: {dst.collection_id} and {src.collection_id} differ in "
            f"{', '.join(diff)}, which decide record bytes or chunks (02 §2.7) — their "
            f"parts are not the same parts, and nothing is carried")


def latest_snapshot(root: Path, name: str, snapshot_id: str | None = None):
    """The named snapshot of `name`'s drag, or its latest."""
    from . import collection as col, drag, snapshot as sn
    d = col.drag_of(Path(root), name)
    if snapshot_id:
        p = d / drag.snapshot_path(snapshot_id)
        if not p.is_file():
            raise CodexError(f"{name} holds no snapshot {snapshot_id!r}")
        return sn.load(p)
    ids = sorted(p.stem for p in (d / "snapshots").glob("*.json")
                 if not p.name.endswith(".sig.json")) if (d / "snapshots").is_dir() else []
    if not ids:
        raise CodexError(f"{name} holds no snapshot: there are no parts to name")
    return sn.load(d / "snapshots" / f"{ids[-1]}.json")


def parts_of(snap) -> list[tuple[str, int, str]]:
    """Every part a snapshot names, as (path, bytes, root)."""
    out = [(s.path, s.bytes, s.root) for s in snap.shards]
    out += [(r.path, r.bytes, r.root) for r in snap.relations]
    out += [(f.path, f.bytes, f.root) for f in snap.figures]
    return out


def _link(src: Path, dst: Path, root: str) -> str:
    """`dst` as `src`'s bytes, checked against `root` before it appears:
    "linked", "copied", or "present" when it already was."""
    from . import drag
    if dst.exists():
        if drag.pieces_root_of(dst) == root:
            return "present"
        raise CodexError(
            f"{dst} exists and its pieces root is not the source snapshot's "
            f"{root[:23]}…; a part is never overwritten here — move it aside")
    if not src.is_file():
        raise CodexError(f"{src}: the source snapshot names this part and the source "
                         f"drag does not hold it")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.seed")
    tmp.unlink(missing_ok=True)
    try:
        os.link(src, tmp)
        how = "linked"
    except OSError:
        shutil.copyfile(src, tmp)
        how = "copied"
    try:
        got = drag.pieces_root_of(tmp)
        if got != root:
            raise CodexError(
                f"{src}: its pieces root is {got[:23]}… and the source snapshot "
                f"names {root[:23]}… — the source drag no longer holds that "
                f"snapshot's part (re-issued since?); nothing is seeded from it")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return how


def _rows(con) -> dict[tuple[str, int], dict]:
    con.row_factory = sqlite3.Row
    return {(r["slice"], r["n"]): dict(r) for r in con.execute("SELECT * FROM shards")}


def _mismatch(rows: dict[tuple[str, int], dict], snap) -> list[str]:
    """Snapshot shards the build database rows do not describe exactly."""
    out = []
    for s in snap.shards:
        r = rows.get((s.slice, s.n))
        if r is None:
            out.append(f"{s.slice}/{s.n:05d}: no row")
            continue
        bad = [a for a, b in _ROW_FIELDS if getattr(s, a) != r.get(b)]
        if r.get("path") != s.path:
            bad.append("path")
        if bad:
            out.append(f"{s.slice}/{s.n:05d}: {', '.join(bad)}")
    return out


def _write_build_db(src_db: Path, dst_db: Path, snap) -> str:
    """The target's build database: the source's, as SQLite's backup copies
    it (plan, cursor, meta — a later `records build` of the target resumes
    exactly where the source's would), its shard rows those of the
    snapshot. Rows the source cut after the snapshot are left out: those
    parts are not in the target's drag, and its next build cuts them."""
    if dst_db.is_file():
        con = sqlite3.connect(dst_db)
        try:
            bad = _mismatch(_rows(con), snap)
            extra = set(_rows(con)) - {(s.slice, s.n) for s in snap.shards}
        finally:
            con.close()
        if bad or extra:
            detail = bad[:3] + ([f"{len(extra)} row(s) beyond it"] if extra else [])
            raise CodexError(
                f"{dst_db} exists and does not describe the source snapshot "
                f"({'; '.join(detail)}); the target's staging holds another build "
                f"— move it aside")
        return "present"
    if not src_db.is_file():
        raise CodexError(
            f"{src_db}: the source has no build database, so nothing says how its "
            f"parts were cut (the plan and cursor a later build resumes from); a "
            f"source whose parts were fetched, not built, cannot seed a build")
    tmp = dst_db.with_name(dst_db.name + ".seed")
    for p in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm"), Path(f"{tmp}-journal")):
        p.unlink(missing_ok=True)
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp, isolation_level=None)
    try:
        src.backup(dst)
        bad = _mismatch(_rows(dst), snap)
        if bad:
            raise CodexError(
                f"{src_db} does not describe the source snapshot ({'; '.join(bad[:3])}); "
                f"its parts' entries cannot be taken from it")
        keep = {(s.slice, s.n) for s in snap.shards}
        later = [k for k in _rows(dst) if k not in keep]
        dst.execute("BEGIN IMMEDIATE")
        dst.executemany("DELETE FROM shards WHERE slice=? AND n=?", later)
        dst.execute("COMMIT")
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        src.close()
        dst.close()
    os.replace(tmp, dst_db)
    for p in (Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
        p.unlink(missing_ok=True)
    return "written"


def seed_shards(root, to_ref: str, from_ref: str, *, from_snapshot: str | None = None,
                log: Callable[[str], None] = print) -> dict[str, Any]:
    """Seed `to_ref`'s drag and staging from `from_ref`'s snapshot (G3). The
    caller holds `to_ref`'s write lock; `snapshot create` follows."""
    from . import collection as col, records as rec
    from app.pack import build as pack_build

    root = Path(root)
    to_name, to = col.resolve(root, to_ref)
    from_name, src = col.resolve(root, from_ref)
    refuse_unless_same_records(src, to, "--shards-from")
    # invariant 9: the source's records are USED here, and the target's are
    # about to be — each through THE gate
    rec.local_collection(root, from_name, src)
    rec.local_collection(root, to_name, to)
    snap = latest_snapshot(root, from_name, from_snapshot)
    if snap.collection_id != src.collection_id:
        raise CodexError(f"{from_name}: snapshot {snap.snapshot_id} names "
                         f"{snap.collection_id}, not its Codex {src.collection_id}")

    staging = root / to_name / rec.STATE / rec.BUILD
    # the claim first: it says what this staging is a build OF, and a staging
    # holding another build is refused here as `records build` refuses it
    rec._claim_staging(staging, to)

    src_drag, dst_drag = col.drag_of(root, from_name), col.drag_of(root, to_name)
    counts = {"linked": 0, "copied": 0, "present": 0}
    for path, _bytes, part_root in parts_of(snap):
        counts[_link(src_drag / path, dst_drag / path, part_root)] += 1

    db = _write_build_db(root / from_name / rec.STATE / rec.BUILD / pack_build.BUILD_DIR
                         / pack_build.BUILD_DB,
                         staging / pack_build.BUILD_DIR / pack_build.BUILD_DB, snap)
    out = {"from": src.collection_id, "to": to.collection_id,
           "snapshot_id": snap.snapshot_id, "snapshot": snap.hash,
           "parts": counts, "build_db": db}
    log(f"[snapshot create] {to_name}: seeded from {from_name} {snap.snapshot_id} — "
        f"{counts['linked']} linked, {counts['copied']} copied, "
        f"{counts['present']} already in place; build database {db}")
    return out
