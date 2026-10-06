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
#
# Desired state lives in the TOML (CLAUDE.md invariant 10), so `share peers
# add` writes platform.toml and nothing here; the worker re-reads it at
# every beat (platform_config.load caches nothing). A peer given by hand
# receives this node's digests from the next beat and becomes a neighbour
# when its own digest arrives — a neighbour is a key at an address (09
# §9.9), and an address alone has no key to list.

def bootstrap_addresses() -> tuple[str, ...]:
    from app import platform_config
    return tuple(platform_config.load().peers.addresses)


def pinned_key(address: Any, cfg=None) -> str | None:
    """The key `[peers.pins]` binds to this address, if any (04 §4.2: a digest
    from that address under another key is discarded and logged)."""
    from app import platform_config
    cfg = cfg or platform_config.load()
    try:
        return cfg.peers.pins.get(platform_config.normalise_address(address))
    except Exception:   # noqa: BLE001 — an address that is not one pins nothing
        return None


def peer_list(cfg=None) -> list[dict[str, Any]]:
    """`[{address, key}]` — the hand-given bootstrap of 04 §4.2, pins included."""
    from app import platform_config
    cfg = cfg or platform_config.load()
    out = [{"address": a, "key": cfg.peers.pins.get(a)} for a in cfg.peers.addresses]
    for a, k in cfg.peers.pins.items():
        if a not in cfg.peers.addresses:
            out.append({"address": a, "key": k})
    return out


_SECTION_HEAD = r"^\[(peers|peers\.pins)\]\s*(#.*)?$"


def _render_peers(addresses: list[str], pins: dict[str, str]) -> list[str]:
    lines = ["[peers]\n",
             "addresses = [" + ", ".join(json.dumps(a) for a in addresses) + "]"
             "      # the bootstrap of 04 §4.2; `share peers add` writes here\n"]
    if pins:
        lines += ["\n", "[peers.pins]      # a digest from this address under another key is discarded\n"]
        lines += [f"{json.dumps(a)} = {json.dumps(k)}\n" for a, k in sorted(pins.items())]
    return lines


def rewrite_peer_list(text: str, addresses: list[str], pins: dict[str, str]) -> str:
    """platform.toml's text with its `[peers]` and `[peers.pins]` sections
    replaced — where `[peers]` stood, or at the end — and every other line
    kept byte for byte. Comments inside the two sections do not survive."""
    import re
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    at: int | None = None
    i = 0
    while i < len(lines):
        if re.match(_SECTION_HEAD, lines[i].rstrip("\r\n")):
            if at is None:
                at = len(kept)
            i += 1
            while i < len(lines) and not lines[i].startswith("["):
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    fresh = _render_peers(addresses, pins)
    if at is None:
        if kept and not kept[-1].endswith("\n"):
            kept[-1] += "\n"
        if kept and kept[-1].strip():
            kept.append("\n")
        return "".join(kept + fresh)
    while at < len(kept) and not kept[at].strip():
        del kept[at]
    tail = kept[at:]
    if tail and tail[0].strip():
        fresh = fresh + ["\n"]
    return "".join(kept[:at] + fresh + tail)


def add_to_peer_list(path, address: str, key: str | None = None) -> dict[str, Any]:
    """`share peers add <address> [--key]` (07 §7.3): the address into
    `[peers] addresses`, each once; with a key, the pin into `[peers.pins]`.
    The file is read as the node reads it and written whole, through a
    temporary file. Returns what the file now says for this address."""
    from pathlib import Path
    from app import platform_config
    from app.hold.codex import CodexError
    from app.hold.envelope import SignatureError, key_bytes
    path = Path(path)
    try:
        address = platform_config.normalise_address(address)
        if key is not None:
            key_bytes(key)
    except (platform_config.schedule_mod.ScheduleError, SignatureError) as e:
        raise CodexError(str(e)) from None
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        cfg = platform_config._load_one(path) if path.is_file() else platform_config.PlatformConfig()
    except platform_config.schedule_mod.ScheduleError as e:
        raise CodexError(f"{path}: {e}") from None
    addresses = list(cfg.peers.addresses)
    pins = dict(cfg.peers.pins)
    added = address not in addresses
    if added:
        addresses.append(address)
    pinned = key is not None and pins.get(address) != key
    if key is not None:
        pins[address] = key
    if added or pinned:
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text(rewrite_peer_list(text, addresses, pins), encoding="utf-8")
        tmp.replace(path)
    return {"address": address, "key": pins.get(address), "added": added, "pinned": pinned,
            "file": str(path), "peers": len(addresses)}


def show(con, *, now: float, cfg=None) -> dict[str, Any]:
    """`share peers show` (07 §7.3): the peer list, and every peer heard
    from — whether it is a neighbour now, its ban, and what its last digest
    says it holds."""
    from app.share import messages
    out_peers = []
    for row in all_peers(con):
        digest = None
        if row.get("digest"):
            try:
                digest = json.loads(row["digest"])
            except (TypeError, ValueError):
                digest = None
        banned = bool(row.get("banned_until") and row["banned_until"] > now)
        out_peers.append({
            "key": row["key"],
            "address": row.get("address"),
            "last_digest": messages.time_text(row["last_digest"]) if row.get("last_digest") else None,
            "neighbour": bool(row.get("last_digest") and row["last_digest"] >= now - NEIGHBOUR_WINDOW_S
                              and row.get("address") and not banned),
            "banned_until": messages.time_text(row["banned_until"]) if banned else None,
            "ban_why": row.get("ban_why") if banned else None,
            "pinned": pinned_key(row.get("address"), cfg) == row["key"] if row.get("address") else False,
            "collections": [
                {"collection_id": c.get("collection_id"), "title": c.get("title"),
                 "snapshot_id": c.get("snapshot_id"), "license": c.get("license"),
                 "privacy": c.get("privacy"), "members": len(c.get("members") or [])}
                for c in (digest or {}).get("collections", []) if isinstance(c, dict)],
        })
    n_events = con.execute("SELECT COUNT(*) FROM events_seen").fetchone()[0]
    return {"peer_list": peer_list(cfg), "peers": out_peers, "events_seen": n_events}


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
