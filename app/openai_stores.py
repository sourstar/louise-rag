"""OpenAI's vector stores and files API over Louise's collections (07 §7.4,
amended 2026-10-22; operator: "add the OpenAI missing interface calls").

A third protocol alias beside `/v1/chat/completions` and the LocalAGI routes:
code written against OpenAI's SDK points `base_url` at this node and finds
its knowledge bases here. Every route is a command of the tree, answered in
OpenAI's shape; nothing here implements a behaviour of its own (07 §7.4).

- A **vector store** is a collection, its id the collection's local name.
  A collection built from an origin is READ-ONLY here: listed, searched,
  its records listed and read. A **deposit collection** (02 §2.9) is the
  writable kind: creating a store creates one, attaching a file is
  `hold deposit add` (the key is the file's name), detaching is `hold
  deposit erase`, deleting the store is `hold collection forget`. Writes
  are done by the worker, as the LocalAGI routes' are.
- A **file** uploaded to `/v1/files` waits in `state/openai/files/` until a
  store takes it; OpenAI's file id stays the id of the deposit it became.
  The pairing and a store's `metadata` are kept in `state/openai/stores.json`
  (runtime state, not desired state: nothing in a TOML file describes them).
- **Search** is `ask search` over the one collection, hybrid with the
  reranker, not logged (as the LocalAGI search). OpenAI's `filters` map to
  the collection's facets (`eq`, `ne`, `in`, `and`); `ranking_options.
  score_threshold` cuts the list; the ranker is always Louise's; a query
  given as a list is searched as one text; `rewrite_query` is not done.
- **Who:** reading a curated store is an `ask` route (a user token once
  users exist); anything about a deposit collection, and every write, needs
  the deposit token or an admin (as the LocalAGI routes).
- Not here: the Responses API's `file_search` tool (it needs `/v1/responses`,
  a second chat API), chunking strategies (a collection's chunker is fixed
  in its Codex, invariant 1), expiry policies (stored and echoed only).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import auth, collections as collections_mod

router = APIRouter(prefix="/v1")


class OpenAIError(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error",
                 param: str | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.message, self.kind, self.param, self.code = status, message, kind, param, code

    def response(self) -> JSONResponse:
        return JSONResponse({"error": {"message": self.message, "type": self.kind,
                                       "param": self.param, "code": self.code}}, status_code=self.status)


def _wrap(handler):
    import functools

    @functools.wraps(handler)
    async def inner(*a, **kw):
        try:
            return await handler(*a, **kw)
        except OpenAIError as e:
            return e.response()
    return inner


# ---- who, and which collection ----------------------------------------------

def _depositor(request: Request) -> bool:
    """The deposit token or an admin: who may see deposit collections and
    write anything here."""
    if auth.deposit_caller(request.headers.get("authorization")) is not None:
        return True
    return auth.is_admin(auth.tokens_of(request.headers, request.cookies))


def _store(request: Request, store_id: str, *, write: bool = False):
    c = collections_mod.by_name(store_id)
    private = c is not None and collections_mod.is_private(c)
    if c is None or (private and not _depositor(request)) or collections_mod.is_annotations(c):
        raise OpenAIError(404, f"No vector store found with id '{store_id}'.", code="not_found")
    if write:
        if not _depositor(request):
            raise OpenAIError(401, "writing a vector store needs the deposit token or an admin "
                                   "(07 §7.4)", kind="authentication_error")
        if not private:
            raise OpenAIError(409, f"'{store_id}' is a collection built from its origin: read-only "
                                   f"here; a deposit collection is the writable kind (02 §2.9)")
    return c


def _need_depositor(request: Request) -> None:
    if not _depositor(request):
        raise OpenAIError(401, "files and new vector stores need the deposit token or an admin "
                               "(07 §7.4)", kind="authentication_error")


# ---- the sidecar: uploaded files, pairings, metadata ----------------------------

def _dir() -> Path:
    return collections_mod.louise_root() / "state" / "openai"


@contextmanager
def _state(write: bool = False) -> Iterator[dict]:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "stores.json"
    with open(d / ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
        try:
            try:
                st = json.loads(p.read_bytes())
            except (OSError, ValueError):
                st = {}
            st.setdefault("files", {})
            st.setdefault("stores", {})
            st.setdefault("batches", {})
            yield st
            if write:
                tmp = p.with_name(".stores.json.tmp")
                tmp.write_text(json.dumps(st, sort_keys=True), encoding="utf-8")
                os.replace(tmp, p)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _now() -> int:
    return int(time.time())


def _dep_id(key: str) -> str:
    """The id of a deposit no OpenAI upload named (one LocalAGI or the CLI
    added): stable, from its key."""
    return "file-dep-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _keys(name: str) -> list[str]:
    from .hold import deposit
    return deposit.keys(name)


def _file_of_key(st: dict, name: str, key: str) -> str:
    for fid, k in (st["stores"].get(name, {}).get("files") or {}).items():
        if k.get("key") == key:
            return fid
    return _dep_id(key)


def _key_of_file(st: dict, name: str, file_id: str) -> str | None:
    got = (st["stores"].get(name, {}).get("files") or {}).get(file_id)
    if got:
        return got["key"]
    return next((k for k in _keys(name) if _dep_id(k) == file_id), None)


# ---- objects ----------------------------------------------------------------------

def _created_at(c) -> int:
    try:
        return int(Path(c.manifest_path).stat().st_ctime)
    except OSError:
        return 0


def _count(c) -> int:
    if collections_mod.is_private(c):
        return len(_keys(c.name))
    from .hold import collection as col
    from .share import seeder
    snap = seeder.cached_snapshot(col.shard_dir_of(collections_mod.louise_root() / "collections", c.name))
    return int(getattr(snap, "records", 0) or 0) if snap is not None else 0


def store_object(c) -> dict:
    with _state() as st:
        meta = st["stores"].get(c.name, {})
    n = _count(c)
    return {"id": c.name, "object": "vector_store", "created_at": _created_at(c), "name": c.name,
            "usage_bytes": 0, "status": "completed", "last_active_at": None,
            "file_counts": {"in_progress": 0, "completed": n, "failed": 0, "cancelled": 0, "total": n},
            "metadata": meta.get("metadata") or {}, "expires_after": meta.get("expires_after"),
            "expires_at": None}


def _store_file(store: str, file_id: str, *, created: int = 0, attributes: dict | None = None,
                usage: int = 0) -> dict:
    return {"id": file_id, "object": "vector_store.file", "created_at": created, "usage_bytes": usage,
            "vector_store_id": store, "status": "completed", "last_error": None,
            "attributes": attributes or {}, "chunking_strategy": {"type": "other"}}


def _page(data: list[dict], *, has_more: bool = False) -> dict:
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": has_more}


def _window(items: list[dict], limit: int, order: str, after: str | None, before: str | None) -> dict:
    items = sorted(items, key=lambda x: (x.get("created_at") or 0, x["id"]), reverse=(order == "desc"))
    ids = [x["id"] for x in items]
    if after in ids:
        items = items[ids.index(after) + 1:]
    elif before in ids:
        items = items[:ids.index(before)]
    limit = max(1, min(int(limit or 20), 100))
    return _page(items[:limit], has_more=len(items) > limit)


# ---- /v1/vector_stores -------------------------------------------------------------

class StoreBody(BaseModel):
    name: str | None = None
    file_ids: list[str] | None = None
    metadata: dict[str, str] | None = None
    expires_after: dict | None = None
    chunking_strategy: dict | None = None


@router.get("/vector_stores")
@_wrap
async def list_stores(request: Request, limit: int = 20, order: str = "desc",
                      after: str | None = None, before: str | None = None):
    show_private = _depositor(request)
    from . import query_service

    def held():
        out = []
        for c in collections_mod.load_all():
            if collections_mod.is_annotations(c):
                continue
            if collections_mod.is_private(c):
                if show_private:
                    out.append(store_object(c))
            elif query_service.withheld_reason(c) is None:
                out.append(store_object(c))
        return out
    return _window(await run_in_threadpool(held), limit, order, after, before)


@router.post("/vector_stores")
@_wrap
async def create_store(request: Request, body: StoreBody):
    """A new deposit collection (`hold collection create deposit`), by the
    worker; then the files named, attached."""
    from .localagi import _NAME_RE
    _need_depositor(request)
    name = body.name or f"vs-{secrets.token_hex(6)}"
    if not _NAME_RE.match(name):
        raise OpenAIError(400, f"name {name!r}: a collection is named [a-z0-9][a-z0-9_-]{{0,63}} (02 §2.9)",
                          param="name")
    if collections_mod.by_name(name) is not None:
        raise OpenAIError(409, f"'{name}' is held already")
    await _worker("create", name)
    with _state(write=True) as st:
        st["stores"].setdefault(name, {})["metadata"] = body.metadata or {}
        st["stores"][name]["expires_after"] = body.expires_after
    c = collections_mod.by_name(name)
    for fid in body.file_ids or []:
        await _attach(name, fid, None)
    return await run_in_threadpool(store_object, c)


@router.get("/vector_stores/{store_id}")
@_wrap
async def get_store(request: Request, store_id: str):
    return await run_in_threadpool(store_object, _store(request, store_id))


@router.post("/vector_stores/{store_id}")
@_wrap
async def modify_store(request: Request, store_id: str, body: StoreBody):
    c = _store(request, store_id)
    _need_depositor(request)
    if body.name not in (None, c.name):
        raise OpenAIError(409, "a vector store's name is its collection's local name and is not "
                               "renamed here", param="name")
    with _state(write=True) as st:
        s = st["stores"].setdefault(c.name, {})
        if body.metadata is not None:
            s["metadata"] = body.metadata
        if body.expires_after is not None:
            s["expires_after"] = body.expires_after
    return await run_in_threadpool(store_object, c)


@router.delete("/vector_stores/{store_id}")
@_wrap
async def delete_store(request: Request, store_id: str):
    """`hold collection forget` of a deposit collection, by the worker."""
    c = _store(request, store_id, write=True)
    await _worker("forget", c.name)
    with _state(write=True) as st:
        st["stores"].pop(c.name, None)
    return {"id": c.name, "object": "vector_store.deleted", "deleted": True}


class SearchBody(BaseModel):
    query: str | list[str]
    max_num_results: int = 10
    filters: dict | None = None
    ranking_options: dict | None = None
    rewrite_query: bool = False


def facet_filters(f: dict | None) -> dict | None:
    """OpenAI's comparison/compound filters as 05 §5.2's facet filters:
    `eq`, `ne`, `in` and `and`; anything else is refused, never ignored."""
    if not f:
        return None
    if f.get("type") == "and":
        out: dict = {}
        for g in f.get("filters") or []:
            out.update(facet_filters(g) or {})
        return out
    kind, key, value = f.get("type"), f.get("key"), f.get("value")
    if not key:
        raise OpenAIError(400, "a filter names a key", param="filters")
    if kind == "eq":
        return {key: value}
    if kind == "ne":
        return {key: {"not": value}}
    if kind == "in" and isinstance(value, list):
        return {key: value}
    raise OpenAIError(400, f"filter type {kind!r}: eq, ne, in and and are supported here", param="filters")


@router.post("/vector_stores/{store_id}/search")
@_wrap
async def search_store(request: Request, store_id: str, body: SearchBody):
    from fastapi import HTTPException
    from . import query_service
    c = _store(request, store_id)
    query = " ".join(body.query) if isinstance(body.query, list) else body.query
    k = max(1, min(int(body.max_num_results or 10), 50))
    filters = facet_filters(body.filters)
    threshold = (body.ranking_options or {}).get("score_threshold")
    try:
        result = await run_in_threadpool(query_service.run_search, [c.name], query, k, "hybrid", False,
                                         private=collections_mod.is_private(c), filters=filters)
    except HTTPException as e:
        raise OpenAIError(e.status_code, str(e.detail)) from e
    hits = result.get("hits") or []
    if collections_mod.is_private(c):
        keys = await run_in_threadpool(_deposit_keys_of, c, [h.get("uid") or _uid_of(c, h) for h in hits])
        with _state() as st:
            ids = {u: _file_of_key(st, c.name, key) for u, key in keys.items()}
    else:
        ids = {}
    data = []
    for h in hits[:k]:
        score = float(h.get("score") or 0.0)
        if threshold is not None and score < float(threshold):
            continue
        uid = h.get("uid") or _uid_of(c, h)
        data.append({"file_id": ids.get(uid, uid), "filename": str(h.get("title") or uid),
                     "score": score, "attributes": _attributes(c, h, uid),
                     "content": [{"type": "text", "text": str(h.get("text") or "")}]})
    return {"object": "vector_store.search_results.page", "search_query": query, "data": data,
            "has_more": False, "next_page": None}


def _uid_of(c, h: dict) -> str:
    from . import retrieval
    try:
        return retrieval.record_of(h)
    except Exception:   # noqa: BLE001
        return f"{c.plugin}:{h.get('book_id')}"


def _attributes(c, h: dict, uid: str) -> dict[str, Any]:
    """OpenAI's attributes are flat strings, numbers and booleans: where the
    passage is, and where Louise's reader opens it."""
    out: dict[str, Any] = {"collection": c.name, "uid": uid}
    for f in ("chunk_id", "title", "authors", "lang", "chapter", "origin_url"):
        if h.get(f) not in (None, ""):
            out[f] = str(h[f])
    if h.get("book_id") not in (None, ""):
        out["reader"] = f"/read/{c.name}/{h['book_id']}?hl={h.get('chunk_id')}"
    return out


def _deposit_keys_of(c, uids: list[str]) -> dict[str, str]:
    from . import db
    uids = [u for u in dict.fromkeys(uids) if u]
    if not uids:
        return {}
    with closing(db.connect(c)) as con:
        rows = con.execute(f"SELECT uid, txt_name FROM documents WHERE uid IN ({','.join('?' * len(uids))})",
                           uids).fetchall()
    return {r[0]: r[1] for r in rows if r[1]}


# ---- /v1/vector_stores/{id}/files ---------------------------------------------------

class AttachBody(BaseModel):
    file_id: str
    attributes: dict | None = None
    chunking_strategy: dict | None = None


class AttributesBody(BaseModel):
    attributes: dict | None = None


async def _worker(op: str, name: str, **args) -> dict:
    from .localagi import LocalAgiError, _in_worker
    try:
        return await _in_worker(op, name, **args)
    except LocalAgiError as e:
        raise OpenAIError(e.status, e.message) from e


async def _attach(name: str, file_id: str, attributes: dict | None) -> dict:
    """`hold deposit add` of an uploaded file, keyed by its file name."""
    from .hold import deposit
    from .plugins.deposit import source as src
    with _state() as st:
        f = st["files"].get(file_id)
    if f is None:
        raise OpenAIError(404, f"No file found with id '{file_id}'.", param="file_id", code="not_found")
    try:
        src.check_key(f["filename"])
    except src.DepositError as e:
        raise OpenAIError(400, str(e), param="file_id") from e
    data = (_dir() / "files" / file_id).read_bytes()
    await _worker("add", name, key=f["filename"], staged=deposit.stage(name, data))
    now = _now()
    with _state(write=True) as st:
        files = st["stores"].setdefault(name, {}).setdefault("files", {})
        for other, v in list(files.items()):        # a key replaced is the new file's now
            if v.get("key") == f["filename"]:
                files.pop(other)
        files[file_id] = {"key": f["filename"], "attributes": attributes or {}, "created_at": now}
    return _store_file(name, file_id, created=now, attributes=attributes, usage=f["bytes"])


@router.post("/vector_stores/{store_id}/files")
@_wrap
async def attach_file(request: Request, store_id: str, body: AttachBody):
    c = _store(request, store_id, write=True)
    return await _attach(c.name, body.file_id, body.attributes)


def _store_files(c, limit: int, after: str | None) -> tuple[list[dict], bool]:
    """A deposit collection's deposits, or a curated collection's records by
    uid — the manifest's primary key, walked from the cursor (never a scan
    of a 40 M-record manifest)."""
    if collections_mod.is_private(c):
        with _state() as st:
            paired = st["stores"].get(c.name, {}).get("files") or {}
            by_key = {v["key"]: (fid, v) for fid, v in paired.items()}
            out = []
            for key in _keys(c.name):
                fid, v = by_key.get(key, (_dep_id(key), {}))
                out.append(_store_file(c.name, fid, created=v.get("created_at", 0),
                                       attributes=v.get("attributes")))
        return out, False
    from . import db
    with closing(db.connect(c)) as con:
        rows = con.execute("SELECT uid, size FROM documents WHERE uid > ? ORDER BY uid LIMIT ?",
                           (after or "", limit + 1)).fetchall()
    out = [_store_file(c.name, r[0], usage=int(r[1] or 0)) for r in rows[:limit]]
    return out, len(rows) > limit


@router.get("/vector_stores/{store_id}/files")
@_wrap
async def list_files_in_store(request: Request, store_id: str, limit: int = 20, order: str = "desc",
                              after: str | None = None, before: str | None = None, filter: str | None = None):
    c = _store(request, store_id)
    if filter not in (None, "completed"):
        return _page([])                     # nothing here is in progress, failed or cancelled
    limit = max(1, min(int(limit or 20), 100))
    if collections_mod.is_private(c):
        items, _ = await run_in_threadpool(_store_files, c, limit, after)
        return _window(items, limit, order, after, before)
    items, more = await run_in_threadpool(_store_files, c, limit, after)
    return _page(items, has_more=more)


@router.get("/vector_stores/{store_id}/files/{file_id}")
@_wrap
async def get_file_in_store(request: Request, store_id: str, file_id: str):
    c = _store(request, store_id)
    if collections_mod.is_private(c):
        with _state() as st:
            key = _key_of_file(st, c.name, file_id)
            v = (st["stores"].get(c.name, {}).get("files") or {}).get(file_id, {})
        if key is None:
            raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
        return _store_file(c.name, file_id, created=v.get("created_at", 0), attributes=v.get("attributes"))
    if await run_in_threadpool(_record, c, file_id) is None:
        raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
    return _store_file(c.name, file_id)


@router.post("/vector_stores/{store_id}/files/{file_id}")
@_wrap
async def update_file_in_store(request: Request, store_id: str, file_id: str, body: AttributesBody):
    c = _store(request, store_id, write=True)
    with _state(write=True) as st:
        files = st["stores"].setdefault(c.name, {}).setdefault("files", {})
        key = _key_of_file(st, c.name, file_id)
        if key is None:
            raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
        v = files.setdefault(file_id, {"key": key, "created_at": 0})
        v["attributes"] = body.attributes or {}
    return _store_file(c.name, file_id, created=v.get("created_at", 0), attributes=v["attributes"])


@router.delete("/vector_stores/{store_id}/files/{file_id}")
@_wrap
async def detach_file(request: Request, store_id: str, file_id: str):
    """`hold deposit erase` of the deposit the file became (the upload
    itself stays in /v1/files, as OpenAI's does)."""
    c = _store(request, store_id, write=True)
    with _state() as st:
        key = _key_of_file(st, c.name, file_id)
    if key is None:
        raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
    await _worker("erase", c.name, key=key)
    with _state(write=True) as st:
        (st["stores"].get(c.name, {}).get("files") or {}).pop(file_id, None)
    return {"id": file_id, "object": "vector_store.file.deleted", "deleted": True}


def _record(c, uid: str) -> dict | None:
    from .ask import figures
    from .hold import collection as col
    from .share import seeder
    tree = col.shard_dir_of(collections_mod.louise_root() / "collections", c.name)
    got = figures.record_of(tree, seeder.cached_snapshot(tree), uid, figures._order(c.codex))
    return got[0] if got else None


@router.get("/vector_stores/{store_id}/files/{file_id}/content")
@_wrap
async def file_content_in_store(request: Request, store_id: str, file_id: str):
    """The text as Louise holds it: a deposit's, or a record's sections."""
    from .hold import deposit
    c = _store(request, store_id)
    if collections_mod.is_private(c):
        with _state() as st:
            key = _key_of_file(st, c.name, file_id)
        if key is None:
            raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
        text = (await run_in_threadpool(deposit.show, c.name, key))["content"]
    else:
        from . import licensing
        if c.codex is not None and c.codex.license.cls == "red":
            raise OpenAIError(403, f"{c.name}: a red-class collection's text is never served (04 §4.4)")
        if licensing.state_for(c) == licensing.MISSING:
            raise OpenAIError(403, f"{c.name}: its terms are not acknowledged; nothing of it is used (04 §4.4)")
        record = await run_in_threadpool(_record, c, file_id)
        if record is None:
            raise OpenAIError(404, f"No file found with id '{file_id}' in '{c.name}'.", code="not_found")
        text = "\n\n".join(str(b or "") for _, b in record.get("sections") or [])
    return {"object": "vector_store.file_content.page", "data": [{"type": "text", "text": text}],
            "has_more": False, "next_page": None}


# ---- file batches ----------------------------------------------------------------

class BatchBody(BaseModel):
    file_ids: list[str] | None = None
    files: list[dict] | None = None
    attributes: dict | None = None


def _batch_object(store: str, b: dict) -> dict:
    n = len(b["file_ids"])
    done = b["status"] == "completed"
    return {"id": b["id"], "object": "vector_store.files_batch", "created_at": b["created_at"],
            "vector_store_id": store, "status": b["status"],
            "file_counts": {"in_progress": 0, "completed": n if done else len(b.get("done") or []),
                            "failed": len(b.get("failed") or []), "cancelled": 0, "total": n}}


@router.post("/vector_stores/{store_id}/file_batches")
@_wrap
async def create_batch(request: Request, store_id: str, body: BatchBody):
    c = _store(request, store_id, write=True)
    pairs = [(f["file_id"], f.get("attributes")) for f in body.files or []] or \
            [(fid, body.attributes) for fid in body.file_ids or []]
    if not pairs:
        raise OpenAIError(400, "file_ids or files", param="file_ids")
    b = {"id": "vsfb_" + secrets.token_hex(12), "created_at": _now(), "file_ids": [p[0] for p in pairs],
         "done": [], "failed": []}
    for fid, attrs in pairs:
        try:
            await _attach(c.name, fid, attrs)
            b["done"].append(fid)
        except OpenAIError:
            b["failed"].append(fid)
    b["status"] = "completed" if not b["failed"] else "failed"
    with _state(write=True) as st:
        st["batches"][b["id"]] = {**b, "store": c.name}
    return _batch_object(c.name, b)


def _batch(st: dict, store: str, batch_id: str) -> dict:
    b = st["batches"].get(batch_id)
    if b is None or b.get("store") != store:
        raise OpenAIError(404, f"No file batch found with id '{batch_id}'.", code="not_found")
    return b


@router.get("/vector_stores/{store_id}/file_batches/{batch_id}")
@_wrap
async def get_batch(request: Request, store_id: str, batch_id: str):
    c = _store(request, store_id)
    with _state() as st:
        return _batch_object(c.name, _batch(st, c.name, batch_id))


@router.post("/vector_stores/{store_id}/file_batches/{batch_id}/cancel")
@_wrap
async def cancel_batch(request: Request, store_id: str, batch_id: str):
    """A batch is done before its answer: there is nothing left to cancel."""
    c = _store(request, store_id, write=True)
    with _state() as st:
        return _batch_object(c.name, _batch(st, c.name, batch_id))


@router.get("/vector_stores/{store_id}/file_batches/{batch_id}/files")
@_wrap
async def batch_files(request: Request, store_id: str, batch_id: str, limit: int = 20, order: str = "desc",
                      after: str | None = None, before: str | None = None):
    c = _store(request, store_id)
    with _state() as st:
        b = _batch(st, c.name, batch_id)
        paired = st["stores"].get(c.name, {}).get("files") or {}
        items = [_store_file(c.name, fid, created=(paired.get(fid) or {}).get("created_at", 0),
                             attributes=(paired.get(fid) or {}).get("attributes"))
                 for fid in b["done"] if fid in paired]
    return _window(items, limit, order, after, before)


# ---- /v1/files --------------------------------------------------------------------

def _file_object(f: dict) -> dict:
    return {"id": f["id"], "object": "file", "bytes": f["bytes"], "created_at": f["created_at"],
            "filename": f["filename"], "purpose": f["purpose"], "status": "processed",
            "expires_at": None, "status_details": None}


@router.post("/files")
@_wrap
async def upload_file(request: Request):
    """An upload kept until a vector store takes it (`purpose` is recorded,
    any value)."""
    from email import policy
    from email.parser import BytesParser
    _need_depositor(request)
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("multipart/form-data"):
        raise OpenAIError(400, "the upload is multipart/form-data, fields `file` and `purpose`")
    msg = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + ctype.encode("latin-1") + b"\r\n\r\n" + await request.body())
    name, data, purpose = None, None, "assistants"
    for part in msg.iter_parts():
        field = part.get_param("name", header="content-disposition")
        if field == "file":
            raw = part.get_param("filename", header="content-disposition") or "upload"
            raw = raw[2] if isinstance(raw, tuple) else raw
            name, data = str(raw).replace("\\", "/").rsplit("/", 1)[-1], part.get_payload(decode=True) or b""
        elif field == "purpose":
            purpose = (part.get_payload(decode=True) or b"").decode("utf-8", "replace").strip() or purpose
    if data is None:
        raise OpenAIError(400, "no `file` field", param="file")
    fid = "file-" + secrets.token_hex(12)
    d = _dir() / "files"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{fid}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, d / fid)
    f = {"id": fid, "bytes": len(data), "created_at": _now(), "filename": name, "purpose": purpose}
    with _state(write=True) as st:
        st["files"][fid] = f
    return _file_object(f)


@router.get("/files")
@_wrap
async def list_uploaded(request: Request, limit: int = 20, order: str = "desc", after: str | None = None,
                        purpose: str | None = None):
    _need_depositor(request)
    with _state() as st:
        items = [_file_object(f) for f in st["files"].values() if purpose in (None, f["purpose"])]
    return _window(items, limit, order, after, None)


def _uploaded(file_id: str) -> dict:
    with _state() as st:
        f = st["files"].get(file_id)
    if f is None:
        raise OpenAIError(404, f"No such File object: {file_id}", param="id", code="not_found")
    return f


@router.get("/files/{file_id}")
@_wrap
async def get_uploaded(request: Request, file_id: str):
    _need_depositor(request)
    return _file_object(_uploaded(file_id))


@router.get("/files/{file_id}/content")
@_wrap
async def uploaded_content(request: Request, file_id: str):
    _need_depositor(request)
    f = _uploaded(file_id)
    return Response(content=(_dir() / "files" / file_id).read_bytes(), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{f["filename"]}"'})


@router.delete("/files/{file_id}")
@_wrap
async def delete_uploaded(request: Request, file_id: str):
    """The upload goes; a deposit made from it stays, as at OpenAI."""
    _need_depositor(request)
    _uploaded(file_id)
    with _state(write=True) as st:
        st["files"].pop(file_id, None)
    try:
        (_dir() / "files" / file_id).unlink()
    except FileNotFoundError:
        pass
    return {"id": file_id, "object": "file", "deleted": True}
