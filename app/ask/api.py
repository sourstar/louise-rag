"""`/api/ask/…` (07 §7.4): `ask`'s verbs over HTTP, one route each, the
body's keys the command's flags without their dashes, the answer the
command's `--json` output; the exit code is the status (0 → 200, 2 → 409,
3 → 503) and the body carries `exit` and the message. And `/api/ask/mcp`,
the MCP server of 05 §5.4 over HTTP (one JSON-RPC request per POST).
Migration step 11."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app.hold.codex import CodexError

router = APIRouter(prefix="/api/ask", tags=["ask"])
STATUS = {0: 200, 1: 500, 2: 409, 3: 503}


def _answer(fn, *args, **kwargs) -> JSONResponse:
    try:
        return JSONResponse(fn(*args, **kwargs))
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))


class SearchBody(BaseModel):
    query: str
    collections: list[str] | None = None
    space: str | None = None
    filters: dict[str, Any] | None = None
    limit: int = Field(10, ge=1, le=100)
    rerank_depth: int = Field(50, ge=1, le=300)
    as_of: str | None = None
    freshness: bool = False
    translate: bool = False
    all_languages: bool = False
    #: the annotations collections consulted, by local name; None = every
    #: one held, [] = none (05 §5.3) — as the MCP tool takes it
    annotations: list[str] | None = None


@router.post("/search")
async def search(body: SearchBody) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    from app.ask import results
    try:
        out = await run_in_threadpool(
            results.search, body.query, collections=body.collections, space=body.space, filters=body.filters,
            limit=body.limit, rerank_depth=body.rerank_depth, as_of=body.as_of, freshness=body.freshness,
            translate=body.translate, all_languages=body.all_languages, annotations=body.annotations)
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))
    return JSONResponse(out)


class ChatBody(BaseModel):
    question: str
    collections: list[str] | None = None
    history: list[dict[str, Any]] | None = None


@router.post("/chat")
async def chat(body: ChatBody) -> JSONResponse:
    """`ask chat` over HTTP (05 §5.4; 07 §7.4): the answer with every tool
    call made and what it returned. `/v1/chat/completions` stays what it
    was — retrieval then an answer, for clients that speak that protocol."""
    import json
    from starlette.concurrency import run_in_threadpool
    from app.ask import chat as chat_mod
    try:
        out = await run_in_threadpool(chat_mod.ask, body.question, collections=body.collections,
                                      history=body.history)
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))
    return JSONResponse(json.loads(json.dumps(out, default=str)))


class LookupBody(BaseModel):
    id: str


@router.post("/lookup")
async def lookup(body: LookupBody) -> JSONResponse:
    from app.ask import lookup as lookup_mod
    return _answer(lookup_mod.lookup, body.id)


class TraverseBody(BaseModel):
    id: str
    relation: str | None = None
    depth: int = Field(1, ge=1, le=6)
    collection: str | None = None
    resolve_in: list[str] | None = None


@router.post("/traverse")
async def traverse(body: TraverseBody) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    from app.ask import traverse as traverse_mod
    try:
        out = await run_in_threadpool(traverse_mod.traverse, body.id, relation=body.relation,
                                      depth=body.depth, collection=body.collection, resolve_in=body.resolve_in)
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))
    return JSONResponse(out)


@router.get("/facets/{collection}")
async def facets(collection: str, field: str | None = None, under: str | None = None) -> JSONResponse:
    from starlette.concurrency import run_in_threadpool
    from app.ask import facets as facets_mod
    try:
        out = await run_in_threadpool(facets_mod.listing, collection, field, under)
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))
    return JSONResponse(out)


@router.get("/figure")
async def figure(collection: str, uid: str, hash: str):
    """07 §7.4's one route that is not JSON: a plate's bytes and its
    `Content-Type`, never base64 — a search result carries the hash, and a
    client fetches the picture beside the chunk that cited it."""
    from fastapi.responses import Response
    from starlette.concurrency import run_in_threadpool
    from app.ask import figures as figures_mod
    try:
        data, media = await run_in_threadpool(figures_mod.figure_bytes, collection, uid, hash)
    except figures_mod.FigureNotServed as e:
        return JSONResponse({"exit": 2, "error": str(e)}, status_code=e.status)
    except CodexError as e:
        code = int(getattr(e, "exit_code", 2))
        return JSONResponse({"exit": code, "error": str(e)}, status_code=STATUS.get(code, 500))
    return Response(content=data, media_type=media)


@router.post("/mcp")
async def mcp_route(request: Request) -> JSONResponse:
    """The MCP server over HTTP — the Streamable HTTP transport with JSON
    answers: one JSON-RPC message in, its response out, 202 and no body for
    a notification. A client that sends a batch gets the batch's answers."""
    from starlette.concurrency import run_in_threadpool
    from app.ask import mcp
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "not JSON"}},
                            status_code=400)
    if isinstance(message, list):
        answers = [r for r in [await run_in_threadpool(mcp.handle, m) for m in message] if r is not None]
        return JSONResponse(answers) if answers else Response(status_code=202)
    response = await run_in_threadpool(mcp.handle, message)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)


@router.get("/mcp")
async def mcp_stream() -> Response:
    """No server-initiated stream: the tools answer the request that called
    them (the transport allows 405 here)."""
    return Response(status_code=405, headers={"Allow": "POST"})
