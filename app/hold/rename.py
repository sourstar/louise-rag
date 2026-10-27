"""`hold collection rename <collection> <new-name>` — the same collection
under another local name (07 §7.3, §7.8; operator, 2026-10-13: the complete
Normattiva, created as `normattiva-1861`, becomes `normattiva`).

A name is this peer's own (07 §7.8): no Codex, record, chunk id or vector
holds it, so a rename recomputes nothing. What carries it, and moves:

1. `<root>/<old>/` — the SHARD link, the state, the text index, the vector
   table — renamed in place (one directory rename);
2. `<SHARDS_ROOT>/<old>`, when the SHARD link points there: renamed and
   relinked, so the one-mount-per-kind layout stays a folder per name;
3. `<ORIGINS_ROOT>/<old>`, likewise — UNLESS another held collection links
   the same folder (both Normattiva collections read /corpora/normattiva):
   then the origin stays where it is and only this collection's link moves
   with its directory;
4. the paths the manifest stores (`documents.path`, `fetch_files.local_path`)
   — absolute, under `<root>/<old>/` — rewritten to `<root>/<new>/` in one
   transaction, so discovery finds them where they are and rescans nothing;
5. the held file `collections/<old>.toml` → `<new>.toml`, LAST: until then
   the node holds the collection under its old name;
6. a per-collection pause (`node pause <old>`) moves with it, so a paused
   collection stays paused under its new name.

The caller holds the collection's write lock: a running node holds it, and
`node pause <old>` releases it (07 §7.7). Every step is idempotent and the
plan is written to `<new>/state/rename.json` first, so an interrupted rename
is finished by running it again."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .codex import CodexError

MARKER = "rename.json"
#: the manifest's absolute paths, by table and column
PATH_COLUMNS = (("documents", "path"), ("fetch_files", "local_path"))


def _link_target(link: Path) -> Path | None:
    return Path(os.readlink(link)) if link.is_symlink() else None


def _origin_shared(root: Path, name: str, target: Path) -> list[str]:
    """Other held collections whose `origin` link points at `target`."""
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.name == name or not d.is_dir():
            continue
        t = _link_target(d / "origin")
        if t is not None and Path(os.path.normpath(t)) == Path(os.path.normpath(target)):
            out.append(d.name)
    return out


def plan(root: Path, old: str, new: str) -> dict[str, Any]:
    """What a rename of `old` to `new` would move, refused before anything
    moves when the new name is taken or not a name."""
    from . import collection as col, deposit
    root = Path(root)
    col.check_name(new)
    if old == new:
        raise CodexError(f"rename: {old!r} is already its name")
    base, dest = root / old, root / new
    held_old, held_new = deposit.held_file(root, old), deposit.held_file(root, new)
    marker = dest / "state" / MARKER
    resuming = marker.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("from") == old
    if not resuming:
        if not base.is_dir():
            raise CodexError(f"rename: {old!r} is not held under {root}")
        if dest.exists() or held_new.exists():
            raise CodexError(f"rename: {new!r} is taken here ({dest if dest.exists() else held_new}); "
                             f"a name holds one collection (07 §7.8)")
    src = dest if resuming and not base.exists() else base
    moves: dict[str, Any] = {"from": old, "to": new, "root": str(root),
                             "directory": [str(base), str(dest)],
                             "held_file": [str(held_old), str(held_new)]}
    shards = os.getenv("SHARDS_ROOT", "").strip()
    t = _link_target(src / col.SHARD_DIR)
    if shards and t is not None and t.name == old and Path(os.path.normpath(t.parent)) == \
            Path(os.path.normpath(shards)):
        moves["shard"] = [str(t), str(t.with_name(new))]
    bundles = os.getenv("BUNDLES_ROOT", "").strip()
    t = _link_target(src / "state" / col.BUNDLES_DIR)
    if bundles and t is not None and t.name == old and Path(os.path.normpath(t.parent)) == \
            Path(os.path.normpath(bundles)):
        moves["bundles"] = [str(t), str(t.with_name(new))]
    origins = os.getenv("ORIGINS_ROOT", "").strip()
    t = _link_target(src / "origin")
    if origins and t is not None and t.name == old and Path(os.path.normpath(t.parent)) == \
            Path(os.path.normpath(origins)):
        shared = _origin_shared(root, old, t)
        if shared:
            moves["origin_kept"] = {"folder": str(t), "also_linked_by": shared}
        else:
            moves["origin"] = [str(t), str(t.with_name(new))]
    # the fast drive's caches keyed on what moves (app/derived.py): carried
    # with it, or the next lookup rebuilds them from the origin
    from app import derived
    pairs = [moves[k] for k in ("shard", "origin", "bundles") if k in moves] + [moves["directory"]]
    caches = derived.carried(pairs)
    if caches:
        moves["caches"] = caches
    return moves


def _rewrite_paths(manifest: Path, old_prefix: str, new_prefix: str) -> dict[str, int]:
    """Every stored absolute path under `old_prefix` re-rooted under
    `new_prefix`, one transaction, BEGIN IMMEDIATE (the reader-then-writer
    rule). No rowid moves (invariant 6: an UPDATE renumbers nothing)."""
    counts: dict[str, int] = {}
    if not manifest.is_file():
        return counts
    con = sqlite3.connect(manifest, timeout=60, isolation_level=None)
    try:
        from app import db
        db.wal(con)                                     # invariant 6: a manifest is WAL
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.execute("BEGIN IMMEDIATE")
        for table, column in PATH_COLUMNS:
            if table not in tables:
                continue
            cur = con.execute(
                f"UPDATE {table} SET {column} = ? || substr({column}, ?) "
                f"WHERE substr({column}, 1, ?) = ?",
                (new_prefix, len(old_prefix) + 1, len(old_prefix), old_prefix))
            counts[f"{table}.{column}"] = cur.rowcount
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return counts


def _move(src: Path, dst: Path) -> str:
    if dst.exists() or dst.is_symlink():
        if src.exists() or src.is_symlink():
            raise CodexError(f"rename: both {src} and {dst} exist; nothing is moved over another")
        return "already"
    if not (src.exists() or src.is_symlink()):
        raise CodexError(f"rename: {src} is gone and {dst} is not there")
    os.rename(src, dst)
    return "moved"


def _relink(link: Path, target: Path) -> None:
    if _link_target(link) == target:
        return
    tmp = link.with_name(link.name + ".rename")
    if tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target, target_is_directory=True)
    os.replace(tmp, link)                    # the link swapped whole, never absent


def rename(root: Path, old: str, new: str, *, dry_run: bool = False) -> dict[str, Any]:
    from . import collection as col
    from app import node_state
    root = Path(root)
    moves = plan(root, old, new)
    if dry_run:
        return {**moves, "dry_run": True}
    base, dest = Path(moves["directory"][0]), Path(moves["directory"][1])
    # 0. the plan, where it survives the directory's own rename
    state = (dest if dest.exists() else base) / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / MARKER).write_text(json.dumps(moves, sort_keys=True), encoding="utf-8")
    done: dict[str, Any] = {}
    # 0b. the fast drive's caches, first: their keys are computed from the
    # folders' places before anything moves, and a resumed rename finds them
    # already carried (derived.carry is idempotent)
    if moves.get("caches"):
        from app import derived
        done["caches"] = derived.carry(moves["caches"])
    # 1. the shared folders, then the links that name them
    for kind, link_name in (("shard", col.SHARD_DIR), ("origin", "origin"),
                            ("bundles", f"state/{col.BUNDLES_DIR}")):
        if kind in moves:
            a, b = Path(moves[kind][0]), Path(moves[kind][1])
            done[kind] = _move(a, b)
            _relink((dest if dest.exists() else base) / link_name, b)
    # 2. the directory
    done["directory"] = _move(base, dest)
    # 3. the manifest's paths
    done["paths"] = _rewrite_paths(dest / "state" / "manifest.sqlite",
                                   str(base) + "/", str(dest) + "/")
    # 4. the pause, if any
    paused = old in node_state.paused_collections()
    if paused:
        node_state.set_paused(new, True)
        node_state.set_paused(old, False)
    done["paused"] = paused
    # 5. the held file, last: until here the node held it under its old name
    held_old, held_new = Path(moves["held_file"][0]), Path(moves["held_file"][1])
    done["held_file"] = _move(held_old, held_new) if (held_old.exists() or held_new.exists()) else "none"
    (dest / "state" / MARKER).unlink(missing_ok=True)
    return {**moves, "done": done}
