"""`share attest` (07 §7.3; 04 §4.2; 03 §3.6; 09 §9.8, §9.9), migration
step 9e, second brick: this peer's completed checks, published once each
as `attestation` events, and the attestation events this peer receives,
kept beside its catalogue.

- PUBLISH: for every attestation `hold check` just wrote, one event whose
  payload is the attestation envelope, signed with this peer's key, kept
  in the node's state database as seen (hops 8) for the worker's beat to
  forward to every neighbour (app/share/gossip.py). Never twice: the
  attestation ids published are recorded, and an id already there is
  skipped however often the command runs.
- RECEIVE (`store_received`): an `attestation` event that arrived for a
  collection held here — its payload verifying as an attestation of that
  collection — is written at 09 §9.8's path if absent, where
  copy_state, vitality and the trust level read it. Events of the other
  types are left for the bricks that act on them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from app.hold import collection as col, shard as shard_mod, envelope as env
from app.hold.codex import CodexError, canonical_json
from app.share import messages, peers

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS attestations_published ("
    " attestation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, collection_id TEXT NOT NULL,"
    " published REAL NOT NULL)",
)


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    peers.ensure_schema(con)
    for stmt in _SCHEMA:
        con.execute(stmt)
    try:
        con.execute("ALTER TABLE events_seen ADD COLUMN acted INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass                                   # already there
    return con


def attestation_path(root: Path, name: str, codex, attestation_id: str) -> Path:
    return (Path(root) / name / "state" / "attestations" / shard_mod._hex(codex.collection_id)
            / f"{shard_mod._hex(attestation_id)}.json")


def published(con) -> dict[str, str]:
    """attestation id -> event id, for every attestation this peer published."""
    ensure_schema(con)
    return {r["attestation_id"]: r["event_id"]
            for r in con.execute("SELECT * FROM attestations_published").fetchall()}


def publish(root: Path, name: str, codex, attestation_ids: Iterable[str], *, key, own_key: str,
            con, now: float | None = None, log: Callable[[str], None] | None = None
            ) -> dict[str, Any]:
    """Each attestation named, read from where `hold check` wrote it, into one
    `attestation` event kept as seen for the beat to forward; the ids
    published before are skipped. `{published: [...], already: [...],
    missing: [...]}`."""
    now = time.time() if now is None else now
    ensure_schema(con)
    done = published(con)
    out: dict[str, Any] = {"published": [], "already": [], "missing": []}
    for aid in attestation_ids:
        if aid in done:
            out["already"].append(aid)
            continue
        p = attestation_path(root, name, codex, aid)
        try:
            envelope = json.loads(p.read_bytes())
            body = env.verify(env.ATTESTATION, envelope)
        except (OSError, ValueError, env.SignatureError, CodexError):
            out["missing"].append(aid)
            continue
        if body.get("collection_id") != codex.collection_id:
            out["missing"].append(aid)
            continue
        event = messages.event_body(key=own_key, type_="attestation", payload=envelope, now=now)
        signed = env.sign(env.EVENT, event, key)
        eid = messages.event_id(event)
        peers.mark_seen(con, eid, now=now, time_text=event["time"], type_="attestation",
                        hops=peers.HOPS, envelope=signed)
        con.execute("INSERT OR REPLACE INTO attestations_published(attestation_id, event_id, "
                    "collection_id, published) VALUES(?,?,?,?)",
                    (aid, eid, codex.collection_id, now))
        con.execute("UPDATE events_seen SET acted=1 WHERE id=?", (eid,))   # ours: nothing to act on
        out["published"].append(aid)
        if log:
            log(f"[{name}] share attest: {body.get('check')} {body.get('result')} published as "
                f"{eid[:23]}…")
    return out


# ---- receiving -----------------------------------------------------------------------

def unacted(con) -> list[dict[str, Any]]:
    """Events received and not yet acted on, oldest first."""
    ensure_schema(con)
    rows = con.execute("SELECT id, type, envelope FROM events_seen WHERE acted=0 "
                       "AND envelope IS NOT NULL ORDER BY seen").fetchall()
    out = []
    for r in rows:
        try:
            out.append({"id": r["id"], "type": r["type"], "envelope": json.loads(r["envelope"])})
        except (TypeError, ValueError):
            con.execute("UPDATE events_seen SET acted=1 WHERE id=?", (r["id"],))
    return out


def mark_acted(con, event_id: str) -> None:
    con.execute("UPDATE events_seen SET acted=1 WHERE id=?", (event_id,))


def store_received(root: Path, con, *, log: Callable[[str], None] | None = None) -> dict[str, int]:
    """Every unacted event: an `attestation` whose payload verifies and names
    a collection held here is written at 09 §9.8's path (if absent). Events
    of the other types are left UNACTED for their handlers (app/share/accept.py:
    snapshot, codex, flag, bundle) — marking them here, as the first brick
    did when no handler existed, raced the import thread and won: the node
    never took in the second peer's bundles (2026-10-07). Counts.
    """
    root = Path(root)
    out = {"stored": 0, "held_elsewhere": 0, "dropped": 0, "other": 0}
    resolve_cache: dict[str, str | None] = {}
    for ev in unacted(con):
        body = (ev["envelope"] or {}).get("body") or {}
        if ev["type"] != "attestation":
            out["other"] += 1                       # another handler's
            continue
        if body.get("type") != "attestation":
            out["dropped"] += 1                     # an envelope that is not what its row says
            mark_acted(con, ev["id"])
            continue
        payload = body.get("payload")
        try:
            att = env.verify(env.ATTESTATION, payload)
        except (env.SignatureError, CodexError, TypeError, KeyError):
            out["dropped"] += 1
            mark_acted(con, ev["id"])
            if log:
                log(f"share: attestation event {ev['id'][:23]}… dropped: its payload does not "
                    f"verify (09 §9.8)")
            continue
        cid = str(att.get("collection_id") or "")
        if cid not in resolve_cache:
            try:
                resolve_cache[cid] = col.name_of(root, cid)
            except (CodexError, FileNotFoundError, OSError):
                resolve_cache[cid] = None
        name = resolve_cache[cid]
        if name is None:
            out["held_elsewhere"] += 1               # not held here: nothing to keep it beside
            mark_acted(con, ev["id"])
            continue
        aid = env.body_id(att)
        codex = col.read(root, name)
        p = attestation_path(root, name, codex, aid)
        if not p.is_file():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(f".{p.name}.event")
            tmp.write_bytes(canonical_json(payload))
            tmp.replace(p)
            out["stored"] += 1
            if log:
                log(f"[{name}] share: attestation {att.get('check')} {att.get('result')} from "
                    f"{str(att.get('key'))[:23]}… kept")
        mark_acted(con, ev["id"])
    return out
