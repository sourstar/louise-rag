"""The certifier's pointer on the wire (03 §3.7; 09 §9.11): the signed
pointer `hold collection certify` writes (app/hold/certify.py) is
published twice with the same bytes — as a BEP 44 mutable item under the
certifier's Ed25519 key with `salt` = the 32 raw bytes of `collection_id`,
and at `https://<anchor domain>/.well-known/louise-pointer/<hex>.json`,
the fallback when the DHT is unreachable — and read back from either; a
peer keeps the highest verified `seq` and discards lower ones. Migration
step 10.

The BEP 44 `seq` is the DHT's own (09 §9.11 amended 2026-10-07): the
libtorrent binding this build runs on manages it — `dht_put_mutable_item`
takes no sequence number and increments the stored one — so the number a
peer judges by is the envelope's `seq`, which the certifier signed; the
DHT's is what keeps the newest put winning in the table. The envelope's
`sig` is what a peer verifies against the Codex.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from app.hold import drag as drag_mod, envelope as env
from app.hold.codex import CodexError, canonical_json

DHT_WAIT_S = 20.0
TIMEOUT_S = 20.0


def salt_of(codex) -> bytes:
    """`salt` = the 32 raw bytes of `collection_id` (09 §9.11)."""
    return bytes.fromhex(drag_mod._hex(codex.collection_id))


def well_known_url(codex) -> str:
    """`/.well-known/louise-pointer/<collection_id hex>.json` on the anchor's domain."""
    parts = urlsplit(codex.certifier.anchor)
    return urlunsplit((parts.scheme, parts.netloc, f"/.well-known/louise-pointer/{drag_mod._hex(codex.collection_id)}.json",
                       "", ""))


def put(session, codex, doc: dict[str, Any], key) -> bytes:
    """The pointer into the DHT under this key; the bytes put. The 64-byte
    secret the DHT's Ed25519 takes is the seed followed by the public key."""
    from app.hold import certify
    certify.verify_pointer(doc, codex)
    pk = bytes(key.verify_key)
    sk = bytes(key) + pk
    data = canonical_json(doc)
    session.dht_put_mutable_item(pk, sk, data, salt_of(codex))
    return data


def _item_bytes(item) -> bytes | None:
    if isinstance(item, (bytes, bytearray)):
        return bytes(item)
    if isinstance(item, str):
        return item.encode("utf-8")
    if isinstance(item, dict):
        return canonical_json(item)
    return None


def get_dht(session, codex, *, wait_s: float = DHT_WAIT_S, sleep: Callable[[float], None] = time.sleep,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any] | None:
    """The pointer the DHT holds under the certifier's key, verified; None
    when nothing answered in time or what answered was not the pointer."""
    from app.hold import certify
    pk = env.key_bytes(codex.certifier.key)
    salt = salt_of(codex)
    session.dht_get_mutable_item(pk, salt)
    deadline = clock() + wait_s
    best: dict[str, Any] | None = None
    while clock() < deadline:
        for a in session.pop_alerts():
            if type(a).__name__ != "dht_mutable_item_alert":
                continue
            try:
                if bytes(a.key) != pk:
                    continue
                got_salt = a.salt
                if isinstance(got_salt, str):
                    got_salt = got_salt.encode("latin-1")
                if bytes(got_salt) != salt:
                    continue
                raw = _item_bytes(a.item)
                if not raw:
                    continue
                doc = json.loads(raw)
                body = certify.verify_pointer(doc, codex)
            except (CodexError, ValueError, TypeError, AttributeError, env.SignatureError):
                continue
            if best is None or int(body["seq"]) > int(best["body"]["seq"]):
                best = doc
            if getattr(a, "authoritative", True):
                return best
        if best is not None:
            return best
        sleep(0.2)
    return best


def get_well_known(codex, *, client=None) -> dict[str, Any] | None:
    """The pointer at the anchor domain's well-known path, verified; None
    when it is not there or does not verify."""
    import httpx
    from app.hold import certify
    c = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
    try:
        try:
            r = c.get(well_known_url(codex))
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            doc = json.loads(r.content)
            certify.verify_pointer(doc, codex)
        except (CodexError, ValueError, TypeError, env.SignatureError):
            return None
        return doc
    finally:
        if client is None:
            c.close()


def _ensure(node_con) -> None:
    node_con.execute("CREATE TABLE IF NOT EXISTS pointers_seen (collection_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, "
                     "snapshot_id TEXT NOT NULL, snapshot_hash TEXT NOT NULL, time TEXT, seen REAL)")


def remember(node_con, doc: dict[str, Any], codex, *, now: float | None = None) -> bool:
    """Keep the pointer when its `seq` beats the one kept; whether it did."""
    from app.hold import certify
    _ensure(node_con)
    body = certify.verify_pointer(doc, codex)
    row = node_con.execute("SELECT seq FROM pointers_seen WHERE collection_id=?", (codex.collection_id,)).fetchone()
    if row is not None and int(row[0]) >= int(body["seq"]):
        return False
    node_con.execute("INSERT OR REPLACE INTO pointers_seen(collection_id, seq, snapshot_id, snapshot_hash, time, seen) "
                     "VALUES(?,?,?,?,?,?)", (codex.collection_id, int(body["seq"]), body["snapshot_id"],
                                             body["snapshot_hash"], body.get("time"),
                                             time.time() if now is None else now))
    return True


def kept(node_con, codex) -> dict[str, Any] | None:
    _ensure(node_con)
    row = node_con.execute("SELECT seq, snapshot_id, snapshot_hash, time FROM pointers_seen WHERE collection_id=?",
                           (codex.collection_id,)).fetchone()
    return None if row is None else {"seq": int(row[0]), "snapshot_id": row[1], "snapshot_hash": row[2],
                                     "time": row[3]}


def current(codex, *, node_con, session=None, client=None, now: float | None = None,
            log: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """Which snapshot the certifier points at now: the DHT first, the
    well-known path when the DHT gave nothing, each remembered when newer;
    the newest kept, or None when none was ever seen."""
    if codex.publication != "certified" or codex.certifier is None:
        return None
    for where, doc in (("dht", get_dht(session, codex) if session is not None else None),
                       ("well-known", None)):
        if where == "well-known" and doc is None and (kept(node_con, codex) is None or session is None):
            doc = get_well_known(codex, client=client)
        if doc is not None:
            if remember(node_con, doc, codex, now=now) and log:
                log(f"pointer for {codex.collection_id[:23]}… from the {where}: snapshot "
                    f"{doc['body']['snapshot_id']} (seq {doc['body']['seq']})")
            break
    return kept(node_con, codex)
