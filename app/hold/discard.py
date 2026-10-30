"""Discarding a snapshot that nothing published names (RULE 1).

A snapshot is immutable once written (02 §2.2), and one a peer may hold is
never recalled — it is superseded (01 promise 8). A snapshot this node
NEVER SERVED and attested nothing of is held by nobody: superseding it would
only write its mistake into the chain as history. wikipedia-v8's 2026-09
(c6e71a2b, 2026-10-29) listed 21.9 M dump-layer twins of archive pages
(app/plugins/walk.py); a successor would have tombstoned every one of them,
a record of a defect no peer ever saw. It is DISCARDED instead, and the
snapshot made again is the one a build by the book makes.

What moves: the snapshot file, its signatures, and every part it lists
(records, relations, figures) — under `<SHARD>/.discarded/<snapshot id>/`,
at their paths inside the SHARD. INSIDE the SHARD, on its own filesystem:
a SHARD is usually a link to another mount (/shards/<name> on the node,
tank, while state/ is on the SSD), and a rename cannot cross a filesystem
(EXDEV — München, 2026-10-30, before the first apply; a copy would have
put 96 GB of parts on the SSD). Nothing reads a SHARD but its `snapshots/`
and `records/` and what a snapshot names, and the torrent leaves out what
is hidden. Moved, not deleted: their record lines are what the rebuild
takes (`hold records build --records-from-discarded`), none parsed. What is
derived from it goes: `index/facets/` (the facets, their postings, the
marker) — never the rest of `index/`, which is the collection's text index
and catalogue — and the edges its relation parts put into the manifest; the
index maintainer derives them again from the snapshot that replaces it.

Refused when it is not the latest snapshot (a later one's `previous` and
delta name it), when this node served it (the seeding control names it),
when this node published an attestation of the collection, or when the
discarded directory already holds that id.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import collection as col, shard as shard_mod, snapshot as sn
from .codex import CodexError

DISCARDED = ".discarded"
#: the collection's index directory: its fts_dir (text index, `_catalog`)
#: and, under it, the facets derived from a snapshot (app/ask/facets.py)
INDEX = "index"


def discarded_dir(root: Path, name: str, snapshot_id: str) -> Path:
    """Inside the SHARD, through its link: the parts' own filesystem."""
    return col.shard_dir_of(Path(root), name) / DISCARDED / snapshot_id


def _device(path: Path) -> int:
    return os.stat(path).st_dev


def held_records(root: Path, name: str, snapshot_id: str):
    """The record lines a discarded snapshot listed, for a build to take
    (`pack.build.HeldRecords`): the parts where the discard put them."""
    from app.pack import build as pack_build
    d = discarded_dir(root, name, snapshot_id)
    path = d / shard_mod.snapshot_path(snapshot_id)
    if not path.is_file():
        raise CodexError(f"{name}: no discarded snapshot {snapshot_id!r} under {d.parent}")
    snap = sn.load(path, container=sn.container_beside(col.shard_dir_of(Path(root), name)
                                                        / shard_mod.snapshot_path(snapshot_id)))
    missing = [s.path for s in snap.shards if not (d / s.path).is_file()]
    if missing:
        raise CodexError(f"{name}: discarded snapshot {snapshot_id} lists {len(missing)} part(s) "
                         f"not held under {d} (first: {missing[0]})")
    return pack_build.HeldRecords(d, tuple(s.path for s in snap.shards))


def _members(snap) -> list[str]:
    return ([s.path for s in snap.shards] + [r.path for r in snap.relations]
            + [f.path for f in getattr(snap, "figures", ())])


def _published(con, node_con, codex, snapshot_id: str) -> str | None:
    """Why the snapshot may be held elsewhere, or None."""
    from app.share import seeder
    got = seeder.served(con) or {}
    if got.get("snapshot_id") == snapshot_id:
        return f"this node served it (seeding control: {got.get('snapshot_id')})"
    if node_con is not None:
        try:
            n = node_con.execute("SELECT COUNT(*) FROM attestations_published WHERE collection_id=?",
                                 (codex.collection_id,)).fetchone()[0]
        except sqlite3.OperationalError:        # no attestation ever published here
            n = 0
        if n:
            return f"this node published {n} attestation(s) of the collection"
    return None


def discard(root: Path, name: str, snapshot_id: str, *, con, node_con=None,
            dry_run: bool = False) -> dict[str, Any]:
    """Move the snapshot and its parts aside and drop what was derived from
    it; `con` is the collection's manifest. Returns what moved."""
    root = Path(root)
    codex = col.read(root, name)
    shard_dir = col.shard_dir_of(root, name)
    path = shard_dir / shard_mod.snapshot_path(snapshot_id)
    if not path.is_file():
        raise CodexError(f"{name}: no snapshot {snapshot_id!r} in {shard_dir / 'snapshots'}")
    from app.share import seeder
    latest = seeder.current_snapshot(shard_dir)
    snap = sn.load(path)
    if latest is None or latest.snapshot_id != snapshot_id:
        raise CodexError(f"{name}: {snapshot_id} is not the latest snapshot "
                         f"({latest.snapshot_id if latest else 'none'} is): a later one names it")
    why = _published(con, node_con, codex, snapshot_id)
    if why:
        raise CodexError(f"{name}: {snapshot_id} is not discarded — {why}; a snapshot a peer "
                         f"may hold is superseded, never recalled (01 promise 8)")
    dest = discarded_dir(root, name, snapshot_id)
    if (dest / shard_mod.snapshot_path(snapshot_id)).exists():
        raise CodexError(f"{name}: {dest} holds a discarded {snapshot_id} already; move it aside first")
    members = _members(snap)
    sig = shard_dir / shard_mod.snapshot_sig_path(snapshot_id)
    out = {"collection": name, "snapshot": snapshot_id, "hash": snap.hash, "parts": len(members),
           "to": str(dest), "discarded": not dry_run}
    if dry_run:
        return out
    dest.mkdir(parents=True, exist_ok=True)
    if _device(path.parent) != _device(dest):
        try:
            dest.rmdir()
        except OSError:
            pass
        raise CodexError(f"{name}: {dest} is not on the SHARD's filesystem ({path.parent}); a rename "
                         f"cannot cross one (EXDEV) and nothing was moved")
    # the parts first, the snapshot file last: an interrupted discard still
    # names its snapshot in the SHARD, and a re-run moves what is left
    for rel in members:
        src = shard_dir / rel
        if src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest / rel)
    relation_paths = [r.path for r in snap.relations]
    if relation_paths:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='relations'").fetchone()
        if has:
            con.execute("BEGIN IMMEDIATE")
            try:
                for i in range(0, len(relation_paths), 900):
                    part = relation_paths[i:i + 900]
                    marks = ",".join("?" * len(part))
                    con.execute(f"DELETE FROM relations WHERE shard IN ({marks})", part)
                    con.execute(f"DELETE FROM relations_indexed WHERE shard IN ({marks})", part)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    # ONLY the facets: `index/` is the collection's fts_dir — its text index
    # and catalogue, kept whole (82 GB on wikipedia-v8; München, 2026-10-30,
    # before the first --yes). The facets, their postings and their marker
    # are what the maintainer derives from a snapshot (app/ask/facets.py).
    from app.ask import facets as facets_mod
    derived = root / name / INDEX / facets_mod.CACHE
    if derived.is_dir():
        shutil.rmtree(derived)
    if sig.is_file():
        (dest / shard_mod.snapshot_sig_path(snapshot_id)).parent.mkdir(parents=True, exist_ok=True)
        os.replace(sig, dest / shard_mod.snapshot_sig_path(snapshot_id))
    (dest / shard_mod.snapshot_path(snapshot_id)).parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, dest / shard_mod.snapshot_path(snapshot_id))
    return out
