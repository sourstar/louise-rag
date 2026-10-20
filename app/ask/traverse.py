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
the collection and shard where it can: the uid's own collection and the
shard of the snapshot whose id range holds it, for a uid of this
collection — and, since 2026-10-07, for an edge into ANOTHER collection
when the caller names it (`resolve_in`): "nothing is resolved across
collections without being asked" (05 §5.5). A uid whose prefix a named
collection's plugin owns is `resolved` where that collection holds it,
else its shard is named; any other id, with one collection named, names
that collection and no shard — the plugin mapping a canonical id to a uid
is the target plugin's, and none has one yet.
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


def index_edges(con, shard_dir: Path, snapshot: sn.Snapshot | None) -> dict[str, int]:
    """Every relation shard the snapshot lists and the SHARD holds, indexed
    once per root: a shard already indexed at that root is skipped, one
    re-issued is replaced whole (delete-then-insert on the shard)."""
    from app.pack import records as pack_records
    ensure_schema(con)
    out = {"shards": 0, "edges": 0, "skipped": 0}
    if snapshot is None:
        return out
    done = {r["shard"]: r["root"] for r in con.execute("SELECT * FROM relations_indexed").fetchall()}
    for r in snapshot.relations:
        p = Path(shard_dir) / r.path
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


def _shard_of(snapshot: sn.Snapshot | None, uid: str, order: str = "bytes") -> str | None:
    """The one shard that can hold `uid`; None where no shard can, or where
    several slices can and only the record knows which."""
    if snapshot is None:
        return None
    try:
        s = snapshot.shard_of(uid.split(":", 1)[1], order)
    except Exception:   # noqa: BLE001 — a uid no shard's range holds
        return None
    return f"{s.slice}/{s.n:05d}" if s else None


class Elsewhere:
    """The collections the caller asked edges to be resolved against
    (05 §5.5), by the plugin that owns a uid's prefix: whether an edge into
    one of them resolves, and the shard it needs when it does not — the
    first by name, its current snapshot's range in its own id order.
    `unheld`: the names asked for that this peer does not hold. Manifests
    are opened once and closed with the walk."""

    def __init__(self, root: Path, named, unheld: tuple[str, ...] = ()) -> None:
        self.root = Path(root)
        self.named = sorted(named, key=lambda nc: nc[0])
        self.unheld = tuple(unheld)
        self.by_plugin: dict[str, list[tuple[str, Any]]] = {}
        for name, codex in self.named:
            self.by_plugin.setdefault(codex.plugin, []).append((name, codex))
        self._cons: dict[str, Any] = {}

    def __enter__(self) -> "Elsewhere":
        return self

    def __exit__(self, *exc) -> None:
        for c in self._cons.values():
            c.close()
        self._cons.clear()

    def _holds(self, name: str, uid: str) -> bool:
        from app.share import coverage as cov
        if name not in self._cons:
            if not (self.root / name / "state" / "manifest.sqlite").is_file():
                return False
            self._cons[name] = cov.manifest(self.root, name)
        return _held_uid(self._cons[name], uid)

    def judge(self, uid: str) -> tuple[bool, dict[str, Any] | None]:
        from app.hold import collection as col
        from app.share import seeder
        cands = self.by_plugin.get(uid.split(":", 1)[0], [])
        if not cands:
            # a canonical id, or a uid of a collection not named or not held:
            # with ONE collection asked, the answer names it (05 §5.5)
            asked = [c.collection_id for _, c in self.named] + list(self.unheld)
            return False, ({"collection_id": asked[0], "shard": None} if len(asked) == 1 else None)
        if any(self._holds(name, uid) for name, _ in cands):
            return True, None
        name, codex = cands[0]
        snapshot = seeder.current_snapshot(col.shard_dir_of(self.root, name))
        return False, {"collection_id": codex.collection_id,
                       "shard": _shard_of(snapshot, uid, (codex.slicing or {}).get("order", "bytes"))}


def edges_of(con, codex, snapshot, node: str, *, relation: str | None = None,
             elsewhere: "Elsewhere | None" = None) -> list[dict[str, Any]]:
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
        if not is_uid_here and elsewhere is not None:
            resolved, needs = elsewhere.judge(to)
        elif is_uid_here and not resolved:
            needs = {"collection_id": codex.collection_id,
                     "shard": _shard_of(snapshot, to, (codex.slicing or {}).get("order", "bytes"))}
        out.append({"from": r["from_uid"], "to": to, "type": r["type"], "snapshot": r["snapshot"],
                    "collection_id": codex.collection_id, "resolved": resolved, "needs": needs})
    return out


def walk(con, codex, snapshot, start: str, *, relation: str | None = None, depth: int = 1,
         elsewhere: "Elsewhere | None" = None) -> list[dict[str, Any]]:
    """Breadth-first from `start`, `depth` hops, each edge once."""
    depth = max(1, min(int(depth), MAX_DEPTH))
    seen_edges: set[tuple] = set()
    seen_nodes = {start}
    frontier = [start]
    out = []
    for _ in range(depth):
        nxt = []
        for node in frontier:
            for e in edges_of(con, codex, snapshot, node, relation=relation, elsewhere=elsewhere):
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


def _targets_of(root: Path, codex) -> list[tuple[str, Any]]:
    """(name, Codex) of each collection an annotations Codex annotates and
    this peer holds (several at Codex version 9, 09 §9.3)."""
    from app.ask.annotations import answers_for
    from app.hold import collection as col
    from app.hold.codex import targets_of
    want = {t.collection_id for t in targets_of(codex)}
    if not want:
        return []
    # a held records twin of a target is one too (02 §2.4 A1f; annotations.answers_for)
    return [(n, c) for n, c in col.list_named(root)
            if want & set(answers_for(root, n, c.collection_id))]


def _target_of(root: Path, codex) -> tuple[str, Any] | None:
    """(name, Codex) of the first held collection an annotations Codex annotates."""
    held = _targets_of(root, codex)
    return held[0] if held else None


def _annotates_plugins(root: Path, codex) -> set[str]:
    """The plugins whose uids an annotations collection's edges join."""
    return {c.plugin for _, c in _targets_of(root, codex)}


def traverse(id_: str, *, relation: str | None = None, depth: int = 1, collection: str | None = None,
             root: Path | None = None, resolve_in: list[str] | None = None) -> dict[str, Any]:
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
        owned = [(n, c) for n, c in targets if c.plugin == prefix or prefix in _annotates_plugins(root, c)]
        targets = owned or targets
    edges: list[dict[str, Any]] = []
    searched = []
    held, unheld = [], []
    for asked in resolve_in or ():
        try:
            held.append(col.resolve(root, asked))
        except CodexError:
            unheld.append(asked)
    with Elsewhere(root, held, tuple(unheld)) as elsewhere:
        for name, codex in targets:
            shard_tree = col.shard_dir_of(root, name)
            snapshot = seeder.current_snapshot(shard_tree)
            annotations = getattr(codex, "annotates", None) is not None
            if not (root / name / "state" / "manifest.sqlite").is_file() and not annotations:
                continue
            with closing(cov.manifest(root, name)) as con:
                index_edges(con, shard_tree, snapshot)
                if annotations:
                    # 02 §2.10: an annotations collection's edges join records
                    # of the collection it annotates, so they are resolved
                    # against it — asked for by holding the annotations
                    with Elsewhere(root, held + _targets_of(root, codex), tuple(unheld)) as against:
                        edges.extend(walk(con, codex, snapshot, id_, relation=relation,
                                          depth=depth, elsewhere=against))
                else:
                    edges.extend(walk(con, codex, snapshot, id_, relation=relation, depth=depth,
                                      elsewhere=elsewhere))
            searched.append(codex.collection_id)
    return {"id": id_, "relation": relation, "depth": max(1, min(int(depth), MAX_DEPTH)),
            "collections": searched, "resolve_in": [c.collection_id for _, c in held] + unheld, "edges": edges}
