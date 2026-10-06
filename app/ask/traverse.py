"""`ask traverse <id> [--relation T] [--depth N]` / `louise_traverse`
(05 §5.4, §5.5; 09 §9.14) — the records reached from a record's uid or a
canonical id along the declared relations, in both directions, `depth`
hops deep. Migration step 11.

The edges live in the collection's `relations/<slice>/NNNNN.jsonl.zst`
members (09 §9.14) and are indexed locally into the manifest —
`relations(from_uid, to_id, type, snapshot, shard)`, both directions
queried — by `index_edges`, which the node's index maintainer runs when a
snapshot's relation shards change and which `ask traverse` runs itself
when a held shard is not indexed yet. An edge whose `to` is a uid of this
collection present here is `resolved`; one pointing at a canonical id, or
at a uid this peer does not hold, is reported unresolved — `needs` names
the collection and shard where this brick can (the uid's own collection
and the shard of the snapshot whose id range holds it), and is null where
the target's plugin would have to say (05 §5.5).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.hold import snapshot as sn
from app.hold.codex import CodexError

MAX_DEPTH = 6
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS relations (from_uid TEXT NOT NULL, to_id TEXT NOT NULL, type TEXT NOT NULL,"
    " snapshot TEXT NOT NULL, shard TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_uid, type)",
    "CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_relations_shard ON relations(shard)",
    "CREATE TABLE IF NOT EXISTS relations_indexed (shard TEXT PRIMARY KEY, root TEXT NOT NULL)",
)


class TraverseError(CodexError):
    exit_code = 2


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def index_edges(con, drag_dir: Path, snapshot: sn.Snapshot | None) -> dict[str, int]:
    """Every relation shard the snapshot lists and the drag holds, indexed
    once per root: a shard already indexed at that root is skipped, one
    re-issued is replaced whole (delete-then-insert on the shard)."""
    from app.pack import records as pack_records
    ensure_schema(con)
    out = {"shards": 0, "edges": 0, "skipped": 0}
    if snapshot is None:
        return out
    done = {r["shard"]: r["root"] for r in con.execute("SELECT * FROM relations_indexed").fetchall()}
    for r in snapshot.relations:
        p = Path(drag_dir) / r.path
        if not p.is_file():
            continue
        if done.get(r.path) == r.root:
            out["skipped"] += 1
            continue
        rows = []
        for edge in pack_records.iter_records(p):
            try:
                rows.append((str(edge["from"]), str(edge["to"]), str(edge["type"]), str(edge["snapshot"]), r.path))
            except (KeyError, TypeError):
                continue
        # one transaction per shard: delete-then-insert, the mark last (invariant
        # 2); a writer that reads first takes BEGIN IMMEDIATE (CLAUDE.md), on a
        # connection not already inside one
        own_tx = not con.in_transaction
        if own_tx:
            con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("DELETE FROM relations WHERE shard=?", (r.path,))
            con.executemany("INSERT INTO relations(from_uid, to_id, type, snapshot, shard) VALUES(?,?,?,?,?)", rows)
            con.execute("INSERT OR REPLACE INTO relations_indexed(shard, root) VALUES(?,?)", (r.path, r.root))
            if own_tx:
                con.execute("COMMIT")
        except BaseException:
            if own_tx:
                con.execute("ROLLBACK")
            raise
        out["shards"] += 1
        out["edges"] += len(rows)
    return out


def _held_uid(con, uid: str) -> bool:
    return con.execute("SELECT 1 FROM documents WHERE uid=?", (uid,)).fetchone() is not None


def _shard_of(snapshot: sn.Snapshot | None, uid: str) -> str | None:
    if snapshot is None:
        return None
    try:
        s = snapshot.shard_of(uid.split(":", 1)[1])
    except Exception:   # noqa: BLE001 — a uid no shard's range holds
        return None
    return f"{s.slice}/{s.n:05d}" if s else None


def edges_of(con, codex, snapshot, node: str, *, relation: str | None = None) -> list[dict[str, Any]]:
    """The edges touching `node`, both directions, one relation type when given."""
    ensure_schema(con)
    sql = "SELECT from_uid, to_id, type, snapshot FROM relations WHERE (from_uid=? OR to_id=?)"
    args: list[Any] = [node, node]
    if relation:
        sql += " AND type=?"
        args.append(relation)
    out = []
    plugin = codex.plugin
    for r in con.execute(sql + " ORDER BY from_uid, type, to_id", args).fetchall():
        to = r["to_id"]
        is_uid_here = to.startswith(plugin + ":")
        resolved = bool(is_uid_here and _held_uid(con, to))
        needs = None
        if is_uid_here and not resolved:
            needs = {"collection_id": codex.collection_id, "shard": _shard_of(snapshot, to)}
        out.append({"from": r["from_uid"], "to": to, "type": r["type"], "snapshot": r["snapshot"],
                    "collection_id": codex.collection_id, "resolved": resolved, "needs": needs})
    return out


def walk(con, codex, snapshot, start: str, *, relation: str | None = None, depth: int = 1) -> list[dict[str, Any]]:
    """Breadth-first from `start`, `depth` hops, each edge once."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    seen_edges: set[tuple] = set()
    seen_nodes = {start}
    frontier = [start]
    out = []
    for _ in range(depth):
        nxt = []
        for node in frontier:
            for e in edges_of(con, codex, snapshot, node, relation=relation):
                key = (e["from"], e["to"], e["type"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                out.append(e)
                other = e["to"] if e["from"] == node else e["from"]
                if other not in seen_nodes and e["resolved"] or (other not in seen_nodes and e["from"] == other):
                    seen_nodes.add(other)
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    return out


def traverse(id_: str, *, relation: str | None = None, depth: int = 1, collection: str | None = None,
             root: Path | None = None) -> dict[str, Any]:
    """05 §5.4's `louise_traverse`: `{edges: [{from, to, type, snapshot,
    collection_id, resolved, needs}]}` over the collection named, or every
    held Codex collection whose plugin owns the id's prefix, or — for a
    canonical id — every held Codex collection."""
    from contextlib import closing
    from app import collections as collections_mod
    from app.hold import collection as col
    from app.share import coverage as cov, seeder
    if not isinstance(id_, str) or ":" not in id_:
        raise TraverseError(f"an id is a record's uid <plugin>:<native_id> or a canonical id <namespace>:<id>, got {id_!r}")
    root = Path(root) if root is not None else collections_mod.louise_root() / "collections"
    prefix = id_.split(":", 1)[0]
    if collection:
        name, codex = col.resolve(root, collection)
        targets = [(name, codex)]
    else:
        targets = [(n, c) for n, c in col.list_named(root)]
        owned = [(n, c) for n, c in targets if c.plugin == prefix]
        targets = owned or targets
    edges: list[dict[str, Any]] = []
    searched = []
    for name, codex in targets:
        drag = col.drag_of(root, name)
        snapshot = seeder.current_snapshot(drag)
        if not (root / name / "state" / "manifest.sqlite").is_file():
            continue
        with closing(cov.manifest(root, name)) as con:
            index_edges(con, drag, snapshot)
            edges.extend(walk(con, codex, snapshot, id_, relation=relation, depth=depth))
        searched.append(codex.collection_id)
    return {"id": id_, "relation": relation, "depth": max(1, min(int(depth), MAX_DEPTH)),
            "collections": searched, "edges": edges}
