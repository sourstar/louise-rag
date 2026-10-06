"""Peers seen, neighbours, bans and the events already seen — in the node's
state database, `state/node.sqlite` (07 §7.8: "peers seen"), as 04 §4.2
rules them. Migration step 9c.

- A peer is one ed25519 key; its address is learnt from its own digest.
- A NEIGHBOUR is any peer whose digest arrived within the last three
  intervals (the interval is one hour).
- A peer that answers a pull with bytes whose hash differs from what it
  advertised is a liar: not a neighbour for twenty-four hours — its digests
  are ignored and it is not scored.
- An event's id is `H(canonical JSON of body)`; ids seen are kept seven
  days, and an event older than that or already seen is dropped.

Timestamps are floats of UNIX seconds; bodies carry 09 §9.1's time strings.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from typing import Any

from app import node_state

INTERVAL_S = 3600.0                   # the digest interval (04 §4.2)
NEIGHBOUR_WINDOW_S = 3 * INTERVAL_S   # a digest within this makes a neighbour
FUTURE_S = INTERVAL_S                 # a body's time may not be further ahead
EVENT_TTL_S = 7 * 86400.0             # ids seen are kept this long
BAN_S = 86400.0                       # a lie costs the liar a day
HOPS = 8                              # an event's hop count at birth
MAX_NEIGHBOURS_LISTED = 16            # a digest carries up to this many

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS peers ("
    " key TEXT PRIMARY KEY, address TEXT, first_seen REAL NOT NULL,"
    " last_digest REAL, digest TEXT, banned_until REAL, ban_why TEXT, pinned INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_peers_last ON peers(last_digest)",
    "CREATE TABLE IF NOT EXISTS events_seen ("
    " id TEXT PRIMARY KEY, seen REAL NOT NULL, time TEXT, type TEXT, hops INTEGER,"
    " envelope TEXT, forwarded INTEGER DEFAULT 0)",
    "CREATE INDEX IF NOT EXISTS idx_events_seen ON events_seen(seen)",
)


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def connect() -> sqlite3.Connection:
    return ensure_schema(node_state.connect())


# ---- peers ------------------------------------------------------------------------

def record_digest(con, body: dict[str, Any], *, now: float) -> None:
    """A verified digest from `body['key']`: the peer is seen, at the
    address its digest names; its digest is kept whole for the catalogue
    and the coverage map (9d, 9e)."""
    con.execute(
        "INSERT INTO peers(key, address, first_seen, last_digest, digest) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET address=excluded.address, "
        "last_digest=excluded.last_digest, digest=excluded.digest",
        (body["key"], body.get("address"), now, now, json.dumps(body, sort_keys=True)))


def peer(con, key: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM peers WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def digest_of(con, key: str) -> dict[str, Any] | None:
    row = peer(con, key)
    if not row or not row.get("digest"):
        return None
    try:
        return json.loads(row["digest"])
    except (TypeError, ValueError):
        return None


def neighbours(con, *, now: float) -> list[dict[str, str]]:
    """`[{key, address}]` of every peer whose digest arrived within the
    window and who is not banned — the recipients of this node's digests
    and events, and the live set the rendezvous of 04 §4.5 scores."""
    rows = con.execute(
        "SELECT key, address FROM peers WHERE last_digest >= ? AND address IS NOT NULL "
        "AND (banned_until IS NULL OR banned_until <= ?) ORDER BY key",
        (now - NEIGHBOUR_WINDOW_S, now)).fetchall()
    return [{"key": r["key"], "address": r["address"]} for r in rows]


def is_banned(con, key: str, *, now: float) -> bool:
    row = con.execute("SELECT banned_until FROM peers WHERE key=?", (key,)).fetchone()
    return bool(row and row["banned_until"] and row["banned_until"] > now)


def ban(con, key: str, *, now: float, why: str) -> None:
    """04 §4.2: a pull that returned bytes whose hash differs from what the
    digest advertised. Not a neighbour for a day; the event is logged by
    the caller with the key."""
    con.execute(
        "INSERT INTO peers(key, first_seen, banned_until, ban_why) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET banned_until=excluded.banned_until, "
        "ban_why=excluded.ban_why", (key, now, now + BAN_S, why[:300]))


def all_peers(con) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute("SELECT * FROM peers ORDER BY key").fetchall()]


# ---- events ----------------------------------------------------------------------

def seen(con, event_id: str) -> bool:
    return con.execute("SELECT 1 FROM events_seen WHERE id=?", (event_id,)).fetchone() is not None


def mark_seen(con, event_id: str, *, now: float, time_text: str, type_: str,
              hops: int, envelope: dict[str, Any] | None) -> None:
    con.execute(
        "INSERT OR IGNORE INTO events_seen(id, seen, time, type, hops, envelope) "
        "VALUES(?,?,?,?,?,?)",
        (event_id, now, time_text, type_, hops,
         json.dumps(envelope, sort_keys=True) if envelope is not None else None))


def to_forward(con) -> list[dict[str, Any]]:
    """Events received and not yet forwarded, with hops left."""
    rows = con.execute(
        "SELECT id, hops, envelope FROM events_seen WHERE forwarded=0 AND hops > 0 "
        "AND envelope IS NOT NULL ORDER BY seen").fetchall()
    out = []
    for r in rows:
        try:
            out.append({"id": r["id"], "hops": r["hops"], "envelope": json.loads(r["envelope"])})
        except (TypeError, ValueError):
            continue
    return out


def mark_forwarded(con, event_id: str) -> None:
    con.execute("UPDATE events_seen SET forwarded=1 WHERE id=?", (event_id,))


def sweep(con, *, now: float) -> int:
    """Ids older than seven days go; a spent ban is left to expire by time."""
    cur = con.execute("DELETE FROM events_seen WHERE seen < ?", (now - EVENT_TTL_S,))
    return cur.rowcount


# ---- the bootstrap list (platform.toml [peers]) --------------------------------------

def bootstrap_addresses() -> tuple[str, ...]:
    from app import platform_config
    return tuple(platform_config.load().peers.addresses)


def recipients(con, *, now: float) -> list[str]:
    """Where this node's digests go: every neighbour, and every address in
    the peer list (04 §4.2) — each address once."""
    out: list[str] = []
    for n in neighbours(con, now=now):
        if n["address"] not in out:
            out.append(n["address"])
    for a in bootstrap_addresses():
        if a not in out:
            out.append(a)
    return out


def with_connection(fn, *args, **kwargs):
    with closing(connect()) as con:
        return fn(con, *args, **kwargs)


def now() -> float:
    return time.time()
