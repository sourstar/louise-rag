"""The LocalAGI routes — Louise as LocalAGI's knowledge base (07 §7.4;
migration step 15d; the design is docs/notes/deposit-collections.md).

The second protocol alias beside `/v1/chat/completions`: the API LocalAGI's
HTTP client speaks (`pkg/localrag/client.go`, read at 7e0947d), so an agent
pointed at this node — `LOCALAGI_LOCALRAG_URL`, or an agent's
`local_rag_url` — uses it as its knowledge base unchanged. Each route is one
command of the tree (`hold collection create deposit`, `hold deposit …`,
`ask search`), answered in LocalAGI's shape:

    {success, message, data, error: {code, message, details}}

— except the upload, whose answer is the bare `{status, filename, key}`
LocalAGI decodes, and create's 201. Nothing here implements a behaviour: a
route that did would be a second implementation of a command, which 07
§7.4 forbids.

Every route requires the deposit token (or the admin token), reads
included: a deposit collection is never returned by an open route.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import auth, collections as collections_mod

PREFIX = "/api/collections"

#: 02 §2.9's names for a collection a client creates (LocalAGI lower-cases
#: agent names already).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class LocalAgiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: str = ""):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def ok(data: Any = None, message: str = "", status: int = 200) -> JSONResponse:
    body: dict[str, Any] = {"success": True}
    if message:
        body["message"] = message
    if data is not None:
        body["data"] = data
    return JSONResponse(body, status_code=status)


def failed(e: LocalAgiError) -> JSONResponse:
    error = {"code": e.code, "message": e.message}
    if e.details:
        error["details"] = e.details
    return JSONResponse({"success": False, "error": error}, status_code=e.status)


def require_caller(authorization: str | None = Header(default=None)) -> str:
    who = auth.deposit_caller(authorization)
    if who is None:
        raise HTTPException(401, "the LocalAGI routes need the deposit token "
                                 "(DEPOSIT_TOKEN_FILE), as a bearer token")
    return who


router = APIRouter(prefix=PREFIX, dependencies=[Depends(require_caller)])


def _root():
    return collections_mod.louise_root() / "collections"


def _deposit_collection(name: str):
    """The collection a write names: held, and a deposit collection."""
    c = collections_mod.by_name(name)
    if c is None:
        raise LocalAgiError(404, "NOT_FOUND", f"no collection {name!r}")
    if not collections_mod.is_private(c):
        raise LocalAgiError(409, "NOT_A_DEPOSIT_COLLECTION",
                            f"{name!r} is not a deposit collection; it is read-only here "
                            f"(07 §7.4)")
    return c


async def _call(fn, *args, **kw):
    """A tree command, off the event loop, its refusals in LocalAGI's shape
    — 404 for what is not there, 409 for what is refused (07 §7.7's 2)."""
    from .hold.codex import CodexError
    from .hold.deposit import DepositError
    try:
        return await run_in_threadpool(fn, *args, **kw)
    except LocalAgiError:
        raise
    except DepositError as e:
        msg = str(e)
        status = 404 if ("no deposit" in msg or "no collection" in msg) else 409
        raise LocalAgiError(status, "REFUSED", msg) from e
    except CodexError as e:
        raise LocalAgiError(409, "REFUSED", str(e)) from e


def _wrap(handler):
    """Every handler answers failures in LocalAGI's envelope."""
    import functools

    @functools.wraps(handler)
    async def inner(*a, **kw):
        try:
            return await handler(*a, **kw)
        except LocalAgiError as e:
            return failed(e)
    return inner


class CreateBody(BaseModel):
    name: str


class DeleteBody(BaseModel):
    entry: str


class SearchBody(BaseModel):
    query: str
    max_results: int = 5


class SourceBody(BaseModel):
    url: str
    update_interval: int = 60


@router.post("")
@_wrap
async def create(body: CreateBody):
    from .louise import create_deposit
    if not _NAME_RE.match(body.name or ""):
        raise LocalAgiError(400, "INVALID_NAME",
                            f"{body.name!r}: a collection a client creates is named "
                            f"[a-z0-9][a-z0-9_-]{{0,63}} (02 §2.9)")
    if collections_mod.by_name(body.name) is not None:
        raise LocalAgiError(409, "EXISTS", f"{body.name!r} is held already")
    codex, _held = await _call(create_deposit, _root(), body.name)
    return ok({"name": body.name, "collection_id": codex.collection_id},
              message="collection created", status=201)


@router.get("")
@_wrap
async def list_collections():
    """Deposit collections, and every held collection read-only (D2)."""
    names = sorted(c.name for c in collections_mod.load_all())
    return ok({"collections": names})


@router.get("/{name}/entries")
@_wrap
async def entries(name: str):
    from .hold import deposit
    _deposit_collection(name)
    keys = await _call(deposit.keys, name)
    return ok({"entries": keys, "keys": keys, "count": len(keys)})


@router.get("/{name}/entries/{entry:path}")
@_wrap
async def entry(name: str, entry: str):
    from .hold import deposit
    _deposit_collection(name)
    shown = await _call(deposit.show, name, entry)
    return ok({"content": shown["content"], "chunk_count": shown["chunk_count"]})


@router.delete("/{name}/entry/delete")
@_wrap
async def delete_entry(name: str, body: DeleteBody):
    from .hold import deposit
    _deposit_collection(name)
    remaining = await _call(deposit.erase, name, body.entry)
    return ok({"deleted_entry": body.entry, "remaining_entries": remaining,
               "entry_count": len(remaining)})


@router.post("/{name}/reset")
@_wrap
async def reset(name: str):
    from .hold import deposit
    _deposit_collection(name)
    n = await _call(deposit.reset, name)
    return ok({"erased": n}, message="collection reset")


@router.post("/{name}/upload")
@_wrap
async def upload(name: str, request: Request):
    """LocalAGI decodes this answer bare — `{status, filename, key}` — and
    reads the key LocalAGI will list, show and delete the deposit by."""
    from .hold import deposit
    _deposit_collection(name)
    found = _multipart_file(request.headers.get("content-type", ""),
                            await request.body())
    if found is None:
        raise LocalAgiError(400, "NO_FILE", "the upload is multipart, field `file`")
    key, data = found
    got = await _call(deposit.add, name, key, data)
    return JSONResponse({"status": "ok", "filename": key, "key": got["key"]})


def _multipart_file(content_type: str, body: bytes) -> tuple[str, bytes] | None:
    """The `file` part of a multipart/form-data body: its file name and its
    bytes. The standard library's MIME parser rather than a new dependency
    (CLAUDE.md), which is all one field needs.

    THE NAME IS THE BASE NAME. LocalAGI's client names the part with the
    path it opened — `CreateFormFile("file", file.Name())`, a temp path such
    as `/tmp/localrag123/2026-09-30-…txt` — and Go's own server reads it
    back through `filepath.Base`. The key LocalAGI lists and deletes by is
    the last component, so that is the key here too."""
    from email import policy
    from email.parser import BytesParser
    if not content_type.lower().startswith("multipart/form-data"):
        return None
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body)
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") != "file":
            continue
        raw = part.get_param("filename", header="content-disposition") or ""
        if isinstance(raw, tuple):                  # RFC 2231: (charset, lang, value)
            raw = raw[2]
        name = raw.replace("\\", "/").rsplit("/", 1)[-1]
        return name, part.get_payload(decode=True) or b""
    return None


@router.post("/{name}/search")
@_wrap
async def search(name: str, body: SearchBody):
    """`ask search` over this one collection — a deposit collection or a
    curated one (D2). NOT written to the query log: `/api/stats` shows the
    recent queries to anyone on the LAN, and an agent's queries are its
    memory's business."""
    from . import query_service
    c = collections_mod.by_name(name)
    if c is None:
        raise LocalAgiError(404, "NOT_FOUND", f"no collection {name!r}")
    k = max(1, min(int(body.max_results or 5), 100))
    try:
        result = await run_in_threadpool(query_service.run_search, [name], body.query,
                                          k, "hybrid", False, private=True)
    except HTTPException as e:
        raise LocalAgiError(e.status_code, "REFUSED", str(e.detail)) from e
    return ok({"query": body.query, "max_results": k,
               "results": [_result(c, h) for h in result.get("hits", [])[:k]],
               "count": len(result.get("hits", [])[:k])})


def _result(collection, hit: dict) -> dict:
    """LocalAGI's `Result`: `ID`, `Content`, `Metadata` (strings only),
    `Similarity`. The similarity is the vector leg's cosine where the chunk
    came from it, and 0 for a hit only the text index found — LocalAGI does
    not read it, and a client that does is not told a number nobody
    computed."""
    meta = {"collection": collection.name}
    for field in ("chunk_id", "title", "chapter", "lang", "source_collection"):
        if hit.get(field) not in (None, ""):
            meta[field] = str(hit[field])
    if hit.get("book_id") is not None:
        meta["uid"] = f"{collection.plugin}:{hit['book_id']}"
    # the dense leg's score is 1 - distance/2 over cosine distance
    # (embeddings.search); only a hit whose kept score IS that leg's has one
    sim = None
    if hit.get("source") == "dense" and hit.get("score") is not None:
        sim = 2.0 * float(hit["score"]) - 1.0
    return {"id": meta.get("uid", str(hit.get("chunk_id", ""))),
            "content": str(hit.get("text") or hit.get("snippet") or ""),
            "metadata": meta,
            "similarity": float(sim) if sim is not None else 0.0}


@router.get("/{name}/sources")
@_wrap
async def list_sources(name: str):
    _deposit_collection(name)
    return ok({"sources": []})


@router.post("/{name}/sources")
@router.delete("/{name}/sources")
@_wrap
async def sources(name: str):
    _deposit_collection(name)
    raise LocalAgiError(501, "NOT_BUILT", "URL sources arrive with migration step 15e")
