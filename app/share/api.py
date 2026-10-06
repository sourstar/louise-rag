"""`/api/share/…` — the five peer messages received (09 §9.9; 04 §4.2), in
the web process. Migration step 9c.

The web verifies a message with the SENDER's key, written in the body, so
it needs no key of its own; what it answers to a pull is self-verifying
bytes from the drag and the state directory, read only. What it learns —
a peer's digest, an event — goes into the node's state database
(app/share/peers.py); the WORKER, which holds this node's key, sends this
node's digests, forwards events and acts on what arrived (app/share/gossip.py).
"""
from __future__ import annotations

from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import collections as collections_mod, db
from app.hold.envelope import SignatureError
from app.share import messages, peers

router = APIRouter(prefix="/api/share", tags=["share"])


def _root():
    return collections_mod.louise_root() / "collections"


async def _json(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError:
        raise HTTPException(400, "a peer message is a JSON envelope (09 §9.9)")


def _log(level: str, text: str) -> None:
    try:
        with closing(db.connect()) as con:
            db.log_event(con, level, "share", text[:500])
    except Exception:   # noqa: BLE001 — the event is a courtesy
        pass


@router.post("/digest/share")
async def digest_share(request: Request) -> JSONResponse:
    """A neighbour's hourly digest: verified, timed, kept whole; its sender
    becomes a neighbour for three intervals."""
    envelope = await _json(request)
    now = peers.now()
    try:
        body = messages.open_digest(envelope, now=now)
    except (SignatureError, messages.MessageError, TypeError, KeyError) as e:
        raise HTTPException(400, str(e))
    pinned = peers.pinned_key(body["address"])
    if pinned is not None and pinned != body["key"]:
        # 04 §4.2: an address added by hand may pin a key; a digest from
        # that address under another key is discarded and logged
        _log("warning", f"share: digest claiming {body['address']} under {body['key']}: that "
                        f"address is pinned to {pinned}; discarded (04 §4.2)")
        raise HTTPException(400, "that address is pinned to another key (04 §4.2)")
    with closing(peers.connect()) as con:
        if peers.is_banned(con, body["key"], now=now):
            return JSONResponse({"accepted": False, "why": "not a neighbour today (04 §4.2)"},
                                status_code=202)
        peers.record_digest(con, body, now=now)
    return JSONResponse({"accepted": True, "collections": len(body["collections"])})


@router.post("/event/share")
async def event_share(request: Request) -> JSONResponse:
    """An event forwarded once: verified, timed, its id computed here; a
    seen id is answered and dropped; a new one is kept for the worker to
    act on and to forward with one hop fewer."""
    message = await _json(request)
    now = peers.now()
    try:
        body, hops = messages.open_event(message, now=now)
    except (SignatureError, messages.MessageError, TypeError, KeyError) as e:
        raise HTTPException(400, str(e))
    eid = messages.event_id(body)
    with closing(peers.connect()) as con:
        if peers.is_banned(con, body["key"], now=now):
            return JSONResponse({"accepted": False, "id": eid}, status_code=202)
        if peers.seen(con, eid):
            return JSONResponse({"accepted": False, "id": eid, "seen": True})
        peers.mark_seen(con, eid, now=now, time_text=body["time"], type_=body["type"],
                        hops=max(0, hops - 1), envelope={"body": body, "sig": message["sig"]})
    return JSONResponse({"accepted": True, "id": eid, "hops": max(0, hops - 1)})


async def _pull(request: Request, what: str) -> JSONResponse:
    envelope = await _json(request)
    now = peers.now()
    try:
        body = messages.open_pull(envelope, now=now)
    except (SignatureError, messages.MessageError, TypeError, KeyError) as e:
        raise HTTPException(400, str(e))
    if body["what"] != what:
        raise HTTPException(400, f"this route answers a pull of {what!r}, the body asks "
                                 f"for {body['what']!r}")
    with closing(peers.connect()) as con:
        if peers.is_banned(con, body["key"], now=now):
            raise HTTPException(403, "not a neighbour today (04 §4.2)")
    return JSONResponse(messages.answer_pull(_root(), body))


@router.post("/codex/fetch")
async def codex_fetch(request: Request) -> JSONResponse:
    return await _pull(request, "codex")


@router.post("/snapshot/fetch")
async def snapshot_fetch(request: Request) -> JSONResponse:
    return await _pull(request, "snapshot")


@router.post("/attestations/fetch")
async def attestations_fetch(request: Request) -> JSONResponse:
    return await _pull(request, "attestations")
