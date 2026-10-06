"""The catalogue (07 §7.8: node.sqlite "the catalogue"; 09 §9.9 `codex`
events; 04 §4.2 "a peer can name and describe a collection it has not
fetched") — the Codexes this peer has heard announced, with their chains,
whether or not it holds them. Migration step 9e.

A `codex` event carries the new Codex first, then the Codexes it comes
from back to the root. Each is read as a Codex (`codex.from_dict`,
validated), the head's id must be the one the event names, and the chain
must hold every parent a member names; what passes is recorded once by
id with who announced it and when. Names decide nothing (08 §8.2): the
title is kept for display, the ids are what the catalogue is keyed on.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from app.hold import codex as cx
from app.hold.codex import CodexError, canonical_json

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS catalogue ("
    " collection_id TEXT PRIMARY KEY, lineage_id TEXT NOT NULL, relation TEXT NOT NULL,"
    " title TEXT, codex TEXT NOT NULL, announced_by TEXT, first_seen REAL NOT NULL)",
)


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def read_chain(payload: dict[str, Any]) -> list[cx.Codex]:
    """The event's `chain` as Codexes, the head first and the one the event
    names; every parent a member names present. Raises CodexError."""
    chain_raw = payload.get("chain")
    if not isinstance(chain_raw, list) or not chain_raw:
        raise CodexError("a codex event carries a non-empty chain (09 §9.9)")
    chain: list[cx.Codex] = []
    for obj in chain_raw:
        if not isinstance(obj, dict):
            raise CodexError("a chain is a list of Codex objects")
        codex = cx.from_dict(obj)
        cx.validate(codex)
        chain.append(codex)
    if chain[0].collection_id != payload.get("collection_id"):
        raise CodexError(f"the event names {payload.get('collection_id')} and the chain begins with "
                         f"{chain[0].collection_id}")
    ids = {c.collection_id for c in chain}
    for c in chain:
        for ref in (c.continues, c.derived_from, c.replaces.collection_id if c.replaces else None):
            if ref and ref not in ids:
                raise CodexError(f"the chain lacks {ref}, which {c.collection_id} comes from")
    return chain


def lineage_of(chain: list[cx.Codex]) -> str:
    by_id = {c.collection_id: c for c in chain}
    c = chain[0]
    while c.continues and c.continues in by_id:
        c = by_id[c.continues]
    return c.collection_id


def record(con, chain: list[cx.Codex], *, announced_by: str | None, now: float | None = None) -> int:
    """Every Codex of the chain not yet in the catalogue, recorded; how many
    were new. The head's lineage is read off the chain."""
    ensure_schema(con)
    now = time.time() if now is None else now
    lineage = lineage_of(chain)
    n = 0
    for i, c in enumerate(chain):
        lid = lineage if i == 0 else lineage_of(chain[i:])
        cur = con.execute(
            "INSERT OR IGNORE INTO catalogue(collection_id, lineage_id, relation, title, codex, "
            "announced_by, first_seen) VALUES(?,?,?,?,?,?,?)",
            (c.collection_id, lid, c.relation, c.descriptive.title, canonical_json(c.to_dict()).decode("utf-8"),
             announced_by, now))
        n += cur.rowcount
    return n


def entries(con) -> list[dict[str, Any]]:
    ensure_schema(con)
    return [{"collection_id": r["collection_id"], "lineage_id": r["lineage_id"], "relation": r["relation"],
             "title": r["title"], "announced_by": r["announced_by"], "first_seen": r["first_seen"]}
            for r in con.execute("SELECT * FROM catalogue ORDER BY first_seen, collection_id").fetchall()]


def codex_of(con, collection_id: str) -> cx.Codex | None:
    ensure_schema(con)
    r = con.execute("SELECT codex FROM catalogue WHERE collection_id=?", (collection_id,)).fetchone()
    if r is None:
        return None
    try:
        return cx.from_dict(json.loads(r["codex"]))
    except (ValueError, CodexError):
        return None
