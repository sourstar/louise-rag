"""The five peer messages (09 §9.9; 04 §4.2) — their bodies, the rules a
receiver applies before reading a field, and the answers to pulls.
Migration step 9c.

Every message is a signed envelope (09 §9.2, app/hold/envelope.py) carried
as the body of an HTTP POST under `/api/share/` (app/share/api.py); the
sender is identified by `body.key` and nothing else. A receiver verifies
the signature, drops a body whose `time` is more than one interval in the
future, and answers a pull with self-verifying objects — the exact stored
bytes, base64, each with its hash — which are therefore not signed again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.hold import drag as drag_mod, envelope as env, snapshot as sn
from app.hold.codex import CodexError, canonical_json
from app.share import peers

DIGEST_KIND, EVENT_KIND, PULL_KIND = "louise-digest", "louise-event", "louise-pull"
DIGEST_VERSION, EVENT_VERSION, PULL_VERSION = 2, 1, 1
EVENT_TYPES = ("snapshot", "codex", "attestation", "flag", "bundle")
PULL_WHATS = ("codex", "snapshot", "attestations")


class MessageError(CodexError):
    """A message a receiver drops, and why."""


# ---- time ---------------------------------------------------------------------------

def time_text(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def parse_time(text: str) -> float:
    try:
        return time.mktime(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (TypeError, ValueError):
        raise MessageError(f"not a time of 09 §9.1: {text!r}") from None


def check_time(body: dict[str, Any], *, now: float, max_age_s: float | None = None) -> float:
    """The body's time as seconds; refused when more than one interval in
    the future, or — for an event — older than `max_age_s`."""
    t = parse_time(body.get("time"))
    if t > now + peers.FUTURE_S:
        raise MessageError(f"a message from the future ({body.get('time')}) is dropped (04 §4.2)")
    if max_age_s is not None and t < now - max_age_s:
        raise MessageError(f"an event older than seven days ({body.get('time')}) is dropped")
    return t


# ---- bodies -------------------------------------------------------------------------

def collection_entry(codex, snapshot: sn.Snapshot | None, members, *, codex_bytes: bytes,
                     lineage_id: str, coverage=()) -> dict[str, Any]:
    """One digest entry (09 §9.9): the collection as THIS peer holds it."""
    return {
        "collection_id": codex.collection_id,
        "lineage_id": lineage_id,
        "title": codex.descriptive.title,
        "snapshot_id": snapshot.snapshot_id if snapshot else None,
        "snapshot_hash": snapshot.hash if snapshot else None,
        "codex_hash": "sha256:" + hashlib.sha256(codex_bytes).hexdigest(),
        "license": codex.license.cls,
        "privacy": codex.privacy,
        "members": [m.to_dict() for m in members],
        "coverage": list(coverage),
    }


def digest_body(*, key: str, address: str, neighbours: list[dict[str, str]],
                benchmarks: list[dict[str, Any]], collections: list[dict[str, Any]],
                now: float | None = None) -> dict[str, Any]:
    """09 §9.9's digest body: who, where, how fast per space, what is held."""
    return {"kind": DIGEST_KIND, "version": DIGEST_VERSION, "key": key, "time": time_text(now),
            "address": address, "neighbours": neighbours[:peers.MAX_NEIGHBOURS_LISTED],
            "benchmarks": benchmarks, "collections": collections}


def event_body(*, key: str, type_: str, payload: dict[str, Any],
               now: float | None = None) -> dict[str, Any]:
    if type_ not in EVENT_TYPES:
        raise MessageError(f"an event is one of {EVENT_TYPES}, not {type_!r}")
    return {"kind": EVENT_KIND, "version": EVENT_VERSION, "key": key, "time": time_text(now),
            "type": type_, "payload": payload}


def event_id(body: dict[str, Any]) -> str:
    """`H(canonical JSON of body)` — computed by the receiver, carried nowhere."""
    return env.body_id(body)


def pull_body(*, key: str, what: str, collection_id: str, snapshot_id: str | None = None,
              since: str | None = None, now: float | None = None) -> dict[str, Any]:
    if what not in PULL_WHATS:
        raise MessageError(f"a pull is one of {PULL_WHATS}, not {what!r}")
    return {"kind": PULL_KIND, "version": PULL_VERSION, "key": key, "time": time_text(now),
            "what": what, "collection_id": collection_id, "snapshot_id": snapshot_id,
            "since": since}


# ---- receiving ----------------------------------------------------------------------

def open_digest(envelope: dict[str, Any], *, now: float) -> dict[str, Any]:
    body = env.verify(env.DIGEST, envelope)
    if body.get("kind") != DIGEST_KIND or body.get("version") not in (1, DIGEST_VERSION):
        raise MessageError("not a louise-digest this build reads (09 §9.9)")
    check_time(body, now=now)
    for field in ("address", "neighbours", "benchmarks", "collections"):
        if field not in body:
            raise MessageError(f"a digest carries {field!r} (09 §9.9)")
    return body


def open_event(message: dict[str, Any], *, now: float) -> tuple[dict[str, Any], int]:
    """The verified body and the hops left; the envelope is {body, sig, hops}."""
    if not isinstance(message, dict) or set(message) != {"body", "sig", "hops"}:
        raise MessageError("an event is exactly {body, sig, hops} (09 §9.9)")
    hops = message["hops"]
    if not isinstance(hops, int) or isinstance(hops, bool) or hops < 0 or hops > peers.HOPS:
        raise MessageError(f"hops is an integer from 0 to {peers.HOPS}, got {hops!r}")
    body = env.verify(env.EVENT, {"body": message["body"], "sig": message["sig"]})
    if body.get("kind") != EVENT_KIND or body.get("version") != EVENT_VERSION:
        raise MessageError("not a louise-event this build reads (09 §9.9)")
    if body.get("type") not in EVENT_TYPES or not isinstance(body.get("payload"), dict):
        raise MessageError("an event names one of the five types and carries a payload object")
    check_time(body, now=now, max_age_s=peers.EVENT_TTL_S)
    return body, hops


def open_pull(envelope: dict[str, Any], *, now: float) -> dict[str, Any]:
    body = env.verify(env.EVENT, envelope)
    if body.get("kind") != PULL_KIND or body.get("version") != PULL_VERSION:
        raise MessageError("not a louise-pull this build reads (09 §9.9)")
    if body.get("what") not in PULL_WHATS or not isinstance(body.get("collection_id"), str):
        raise MessageError("a pull names what it wants and the collection")
    check_time(body, now=now)
    return body


# ---- answering pulls ------------------------------------------------------------------

def item(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
            "b64": base64.b64encode(data).decode("ascii")}


def verify_items(items: list[dict[str, Any]]) -> list[tuple[str, bytes]]:
    """The bytes of each item, once its hash is its own; a mismatch is a lie
    (04 §4.2) and raises naming the path."""
    out = []
    for it in items:
        data = base64.b64decode(it["b64"])
        got = "sha256:" + hashlib.sha256(data).hexdigest()
        if got != it.get("sha256"):
            raise MessageError(f"{it.get('path')}: the bytes hash {got}, the item says "
                               f"{it.get('sha256')} — a lie, dropped (04 §4.2)")
        out.append((it["path"], data))
    return out


def _chain(root: Path, codex, resolve) -> list:
    """The Codex and the ones it comes from, along `continues`, `derived_from`
    and `replaces`, back to the root (09 §9.9's codex event and pull)."""
    out, seen_ids, todo = [], set(), [codex]
    while todo:
        c = todo.pop(0)
        if c.collection_id in seen_ids:
            continue
        seen_ids.add(c.collection_id)
        out.append(c)
        for ref in (c.continues, c.derived_from,
                    getattr(c.replaces, "collection_id", None) if c.replaces else None):
            if ref:
                parent = resolve(ref)
                if parent is not None:
                    todo.append(parent)
    return out


def answer_pull(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """`{"items": [...]}` — the exact stored bytes of what was asked, or an
    empty list for a collection this peer does not hold."""
    from app.hold import collection as col
    root = Path(root)
    try:
        name = col.name_of(root, body["collection_id"])
        codex = col.read(root, name)
    except (CodexError, FileNotFoundError, OSError):
        return {"items": []}
    drag = col.drag_of(root, name)
    what = body["what"]
    if what == "codex":
        items = []
        for c in _chain(root, codex, col.resolver(root)):
            try:
                cname = col.name_of(root, c.collection_id)
                data = (col.drag_of(root, cname) / drag_mod.CODEX_PATH).read_bytes()
            except (CodexError, FileNotFoundError, OSError):
                data = canonical_json(c.to_dict())
            items.append(item(f"codex/{drag_mod._hex(c.collection_id)}.json", data))
        return {"items": items}
    if what == "snapshot":
        sid = body.get("snapshot_id")
        if sid is None:
            from app.share import seeder
            snap = seeder.current_snapshot(drag)
            sid = snap.snapshot_id if snap else None
        if not sid:
            return {"items": []}
        items = []
        for rel in (drag_mod.snapshot_path(sid), drag_mod.snapshot_sig_path(sid)):
            p = drag / rel
            if p.is_file():
                items.append(item(rel, p.read_bytes()))
        return {"items": items}
    since = parse_time(body["since"]) if body.get("since") else None
    d = root / name / "state" / "attestations" / drag_mod._hex(codex.collection_id)
    items = []
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        data = p.read_bytes()
        if since is not None:
            try:
                t = parse_time(json.loads(data)["body"]["time"])
            except (KeyError, TypeError, ValueError, MessageError):
                continue
            if t <= since:
                continue
        items.append(item(f"attestations/{p.name}", data))
    return {"items": items}
