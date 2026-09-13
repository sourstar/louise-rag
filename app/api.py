"""Web process — dashboard UI, control plane, stats, and the RAG API.

The /v1/chat/completions endpoint is OpenAI-compatible so Open WebUI can use
this app as a model provider ("louise-rag").

/api/search and /v1/chat/completions federate across collections (P0.5,
app/retrieval.py::federated_search) via an optional `collections` request
field (None = all enabled). Book-management endpoints (books/*, reindex,
summary, /api/stats) stay scoped to collections.default_collection() — not
part of P0.5's deliverables, which are search-time only. Every endpoint here
keeps its pre-refactor int `book_id` request/response shape (native_id),
translating to/from the manifest's internal `uid` inside each handler.

P0.6 adds server-rendered Ask/Library/Network/System pages (app/web.py,
mounted below) over the same manifest tables and, for Library's grow
action, the same app/acquire/engine.py functions app/cli.py calls. Mutating
endpoints here (control/scope/discover/reset/reindex/POST-summary) are
gated by app.auth.require_admin (a single ADMIN_TOKEN, header or cookie);
read/query endpoints (status/stats/events/books/search/chat) stay open —
"Ask is LAN-open read-only" (architecture-v2.md §15).
"""
import json
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import auth, collections as collections_mod, query_service, web
from . import config, db, discovery, retrieval, llm, summarize, embeddings, fts, spaces
from . import node_state

STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # PER-COLLECTION (invariant 1): one collection whose space does not
        # match is not served, and the node keeps the rest. The web process
        # exiting over it took every other collection offline too.
        for collection, reason in spaces.refuse_boot(collections_mod.load_all()):
            print(f"NOT SERVED: {collection.name}: {reason}", file=sys.stderr)
    except spaces.BootValidationError as e:
        # chunking config — node-wide, no per-collection answer exists
        print(f"BOOT VALIDATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    yield

app = FastAPI(title="louise-rag", lifespan=lifespan)

def _con():
    # one connection per request keeps things simple; SQLite WAL handles it
    return db.connect()

def _collection():
    return collections_mod.default_collection()

def _uid(book_id: int) -> str:
    return f"{_collection().plugin}:{book_id}"

def _display_id(doc_uid: str | None):
    if not doc_uid:
        return None
    _, _, native = doc_uid.partition(":")
    try:
        return int(native)
    except ValueError:
        return native

# collection-resolution + search-running helpers live in query_service.py so
# app/web.py (the server-rendered Ask/Library/Network/System pages) can share
# them without importing this module (this module mounts web.py's routers,
# so the reverse import would be circular).
_resolve_collections = query_service.resolve_collections
_run_search = query_service.run_search
_source_qualified_ids = query_service.source_qualified_ids

app.include_router(web.ask_router)
app.include_router(web.admin_router)
# index.html was fully self-contained (inline CSS/JS, no asset requests) so
# no static mount existed before P0.6 — the new pages need admin.css and the
# vendored htmx.min.js served for real.
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---------------- UI ----------------

@app.get("/")
def index():
    """The legacy self-contained dashboard used to live here; it's retired —
    the stopped gutenberg-rag app keeps its own copy. Ask is the platform's
    front door."""
    return RedirectResponse("/ask")


@app.exception_handler(StarletteHTTPException)
async def _unauthenticated_page_to_login(request: Request, exc: StarletteHTTPException):
    """A browser hitting an admin PAGE without a token gets the login page
    (with a return path), not a bare 401. Only plain page navigation is
    redirected: JSON/API clients (no text/html Accept) and htmx fragment
    polling (HX-Request) still get the honest 401, so nothing programmatic
    ever sees a 303 masquerading as auth failure."""
    if (exc.status_code == 401 and request.method == "GET"
            and "text/html" in request.headers.get("accept", "")
            and "hx-request" not in request.headers):
        return RedirectResponse(
            f"/admin/login?next={quote(request.url.path)}", status_code=303)
    return await http_exception_handler(request, exc)

@app.get("/healthz")
def healthz():
    return {"ok": True}

# ---------------- status & stats ----------------

@app.get("/api/status")
def status():
    con = _con()
    scope = db.get_scope(con)
    counts = con.execute("""
        SELECT COUNT(*) total,
          SUM(in_scope) in_scope,
          SUM(CASE WHEN in_scope=1 AND rdf_done=1 THEN 1 ELSE 0 END) rdf_done,
          SUM(CASE WHEN in_scope=1 AND fts_done=1 THEN 1 ELSE 0 END) fts_done,
          SUM(CASE WHEN in_scope=1 AND embed_done=1 THEN 1 ELSE 0 END) embed_done,
          SUM(CASE WHEN in_scope=1 AND summary_done=1 THEN 1 ELSE 0 END) summaries,
          SUM(CASE WHEN in_scope=1 AND error IS NOT NULL THEN 1 ELSE 0 END) errors,
          SUM(CASE WHEN in_scope=1 THEN chunks ELSE 0 END) chunks,
          SUM(CASE WHEN in_scope=1 THEN tokens ELSE 0 END) tokens
        FROM documents""").fetchone()
    queue = con.execute("SELECT COUNT(*) c FROM embed_queue").fetchone()["c"]
    return {
        "worker_state": node_state.worker_state(),
        "scope": scope,
        "counts": {k: counts[k] or 0 for k in counts.keys()},
        "embed_queue": queue,
        "vectors": _safe_vectors(),
        "config": {
            "embed_model": config.EMBED_MODEL, "chat_model": config.CHAT_MODEL,
            "chunk_tokens": config.CHUNK_TOKENS,
            "versions": {"chunk": config.CHUNK_VERSION, "fts": config.FTS_VERSION,
                         "embed": config.EMBED_VERSION},
        },
    }

def _safe_vectors():
    try:
        return embeddings.stats()["vectors"]
    except Exception:
        return 0

@app.get("/api/stats")
def stats():
    con = _con()
    collection = _collection()
    langs = [dict(r) for r in con.execute("""
        SELECT COALESCE(lang,'?') lang, COUNT(*) books,
          SUM(fts_done) fts_done, SUM(embed_done) embed_done,
          SUM(tokens) tokens
        FROM documents WHERE in_scope=1 GROUP BY lang ORDER BY books DESC LIMIT 30""")]
    usage = con.execute("""
        SELECT COUNT(*) queries, AVG(total_ms) avg_ms,
          SUM(CASE WHEN ts > strftime('%s','now')-86400 THEN 1 ELSE 0 END) last24h
        FROM query_log""").fetchone()
    recent = [dict(r) for r in con.execute(
        "SELECT ts,endpoint,query,mode,total_ms,n_results FROM query_log "
        "ORDER BY id DESC LIMIT 15")]
    def du(path):
        try:
            return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
        except Exception:
            return 0
    return {"languages": langs,
            "usage": {k: usage[k] or 0 for k in usage.keys()},
            "recent_queries": recent,
            "disk": {"fts": du(collection.fts_dir), "vectors": du(collection.lance_dir),
                     "manifest": du(collection.manifest_path) or
                                 (collection.manifest_path.stat().st_size
                                  if collection.manifest_path.exists() else 0)}}

@app.get("/api/events")
def events(after: int = 0, limit: int = 60):
    con = _con()
    rows = []
    for r in con.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT ?", (after, limit)):
        d = dict(r)
        d["book_id"] = _display_id(d.pop("doc_uid", None))
        rows.append(d)
    return {"events": rows}

# ---------------- control ----------------

class ControlBody(BaseModel):
    state: str

@app.post("/api/control", dependencies=[Depends(auth.require_admin)])
def control(body: ControlBody):
    if body.state not in db.WORKER_STATES:
        raise HTTPException(400, f"state must be {'|'.join(db.WORKER_STATES)}")
    # node.sqlite, not the first collection's manifest: the ONE place every
    # process reads the node state from (app/node_state.py)
    node_state.set_worker_state(body.state)
    try:
        db.log_event(_con(), "info", "control", f"worker set to {body.state}")
    except Exception:   # noqa: BLE001 — the state is set; the event is a courtesy
        print(f"[info:control] worker set to {body.state}", flush=True)
    return {"worker_state": body.state}

class ScopeBody(BaseModel):
    languages: list[str] = []
    book_ids: list[int] = []
    limit: int = 0
    sample_per_lang: int = 0
    embed_mode: str = "lazy"

@app.post("/api/scope", dependencies=[Depends(auth.require_admin)])
def set_scope(body: ScopeBody):
    con = _con()
    scope = db.set_scope(con, body.model_dump())
    discovery.apply_scope(con)
    db.log_event(con, "info", "control", f"scope updated: {json.dumps(scope)}")
    return {"scope": scope}

@app.post("/api/discover", dependencies=[Depends(auth.require_admin)])
def discover():
    con = _con()
    return discovery.sync_collection(con, _collection())

class ResetBody(BaseModel):
    stage: str  # rdf | fts | embed | errors
    book_ids: list[int] = []

@app.post("/api/reset", dependencies=[Depends(auth.require_admin)])
def reset(body: ResetBody):
    con = _con()
    try:
        db.apply_reset(con, body.stage, body.book_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log_event(con, "info", "control", f"reset {body.stage} ({len(body.book_ids) or 'all'})")
    return {"ok": True}

# ---------------- books ----------------

@app.get("/api/books")
def books(status: str = "", lang: str = "", q: str = "", page: int = 0, size: int = 50):
    con = _con()
    where, params = ["1=1"], []
    if status == "errors":
        where.append("error IS NOT NULL")
    elif status == "pending":
        where.append("in_scope=1 AND (rdf_done=0 OR fts_done=0)")
    elif status == "done":
        where.append("in_scope=1 AND fts_done=1")
    if lang:
        where.append("lang=?"); params.append(lang)
    if q:
        where.append("(title LIKE ? OR authors LIKE ? OR native_id=?)")
        params += [f"%{q}%", f"%{q}%", q]
    rows = [dict(r) for r in con.execute(
        f"SELECT CAST(native_id AS INTEGER) AS id,title,authors,lang,in_scope,rdf_done,"
        f"fts_done,embed_done,summary_done,chunks,tokens,error FROM documents "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY CAST(native_id AS INTEGER) LIMIT ? OFFSET ?", params + [size, page * size])]
    return {"books": rows}

@app.get("/api/books/{book_id}")
def book_detail(book_id: int):
    con = _con()
    row = con.execute("SELECT * FROM documents WHERE uid=?", (_uid(book_id),)).fetchone()
    if not row:
        raise HTTPException(404, "not in manifest")
    d = dict(row)
    d["id"] = book_id
    d["summary"] = summarize.get_cached(con, book_id)
    return d

@app.post("/api/books/{book_id}/reindex", dependencies=[Depends(auth.require_admin)])
def reindex(book_id: int):
    con = _con()
    uid = _uid(book_id)
    con.execute(
        "UPDATE documents SET rdf_done=0,fts_done=0,embed_done=0,error=NULL,retries=0 "
        "WHERE uid=?", (uid,))
    db.log_event(con, "info", "control", "manual reindex", uid)
    return {"ok": True}

_summary_jobs: dict[int, str] = {}   # book_id -> "running" | error string
_summary_lock = threading.Lock()

def _run_summary(book_id: int, force: bool):
    con = db.connect()   # sqlite connections are per-thread
    try:
        summarize.summarize_book(con, book_id, force=force)
        with _summary_lock:
            _summary_jobs.pop(book_id, None)
    except Exception as e:
        with _summary_lock:
            _summary_jobs[book_id] = f"{type(e).__name__}: {e}"[:300]
        db.log_event(con, "error", "summary", str(e)[:300], _uid(book_id))

@app.post("/api/books/{book_id}/summary", dependencies=[Depends(auth.require_admin)])
def make_summary(book_id: int, force: bool = False):
    con = _con()
    if not force:
        cached = summarize.get_cached(con, book_id)
        if cached:
            return {"status": "done", **cached}
    with _summary_lock:
        state = _summary_jobs.get(book_id)
        if state == "running":
            return {"status": "running"}
        _summary_jobs[book_id] = "running"
    threading.Thread(target=_run_summary, args=(book_id, force), daemon=True).start()
    return {"status": "started"}

@app.get("/api/books/{book_id}/summary")
def summary_status(book_id: int):
    con = _con()
    cached = summarize.get_cached(con, book_id)
    with _summary_lock:
        state = _summary_jobs.get(book_id)
    if state == "running":
        return {"status": "running"}
    if state:
        with _summary_lock:
            _summary_jobs.pop(book_id, None)
        return {"status": "error", "error": state}
    if cached:
        return {"status": "done", **cached}
    return {"status": "none"}

# ---------------- search (debug playground) ----------------

class SearchBody(BaseModel):
    query: str
    k: int = 12
    mode: str = "hybrid"
    translate: bool = True
    collections: list[str] | None = None   # None = all enabled
    verify: bool = False                   # blocking verify-on-use (§6 T4)

@app.post("/api/search")
def api_search(body: SearchBody):
    result = _run_search(body.collections, body.query, body.k, body.mode, body.translate,
                          verify=body.verify)
    con = _con()   # query_log lives in the default collection's manifest regardless
    db.log_query(con, "search", body.query, body.mode, body.k, result["timings"],
                 len(result["hits"]), _source_qualified_ids(result["hits"]))
    return result

# ---------------- OpenAI-compatible RAG endpoint ----------------

@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": "gutenberg-rag", "object": "model", "owned_by": "homelab"}]}

class VerifyBody(BaseModel):
    chunk_ids: list[str]
    collections: list[str] | None = None

@app.post("/api/verify")
def api_verify(body: VerifyBody):
    """Verify-on-use after the fact: ask a question, then decide the answer is
    worth checking (architecture-v2.md §6 T4). Recomputes the named imported
    chunks and updates their badges permanently; locally embedded chunks are
    skipped, since re-embedding them only confirms our own arithmetic."""
    from .bundles import verify_on_use
    if len(body.chunk_ids) > 200:
        raise HTTPException(400, "at most 200 chunk ids per request")
    merged = {"checked": 0, "verified": 0, "failed": 0, "quarantined": []}
    for participant in _resolve_collections(body.collections):
        con = db.connect(participant)
        summary = verify_on_use.verify_chunks(con, participant, body.chunk_ids)
        for key in ("checked", "verified", "failed"):
            merged[key] += summary.get(key, 0)
        merged["quarantined"].extend(summary.get("quarantined", []))
    return merged

class ChatBody(BaseModel):
    model: str = "gutenberg-rag"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.3
    collections: list[str] | None = None   # None = all enabled
    verify: bool = False                   # blocking verify-on-use (§6 T4)
    answer_lang: str | None = None         # ISO code: answer in this language (Ask's "show results in")

@app.post("/v1/chat/completions")
def chat_completions(body: ChatBody):
    con = _con()   # query_log lives in the default collection's manifest regardless
    user_msgs = [m for m in body.messages if m.get("role") == "user"]
    if not user_msgs:
        raise HTTPException(400, "no user message")
    query = str(user_msgs[-1].get("content", ""))
    t0 = time.time()
    result = _run_search(body.collections, query, 10, "hybrid", True, verify=body.verify)
    messages = retrieval.build_answer_messages(query, result, answer_lang=body.answer_lang)
    # keep prior turns for conversational context (before the sources turn)
    history = [m for m in body.messages[:-1] if m.get("role") in ("user", "assistant")]
    messages = [messages[0]] + history[-6:] + [messages[1]]
    rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not body.stream:
        answer = llm.chat(messages, temperature=body.temperature)
        result["timings"]["llm"] = round((time.time() - t0) * 1000 - result["timings"]["total"], 1)
        result["timings"]["total"] = round((time.time() - t0) * 1000, 1)
        db.log_query(con, "chat", query, "hybrid", 10, result["timings"],
                     len(result["hits"]), _source_qualified_ids(result["hits"]))
        return {
            "id": rid, "object": "chat.completion", "created": int(time.time()),
            "model": "gutenberg-rag",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
        }

    def sse():
        try:
            for delta in llm.chat_stream(messages, temperature=body.temperature):
                chunk = {"id": rid, "object": "chat.completion.chunk",
                         "created": int(time.time()), "model": "gutenberg-rag",
                         "choices": [{"index": 0, "delta": {"content": delta},
                                      "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            err = {"id": rid, "object": "chat.completion.chunk",
                   "choices": [{"index": 0, "delta": {"content": f"\n[error: {e}]"},
                                "finish_reason": None}]}
            yield f"data: {json.dumps(err)}\n\n"
        done = {"id": rid, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "gutenberg-rag",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"
        timings = dict(result["timings"])
        timings["total"] = round((time.time() - t0) * 1000, 1)
        db.log_query(con, "chat", query, "hybrid", 10, timings,
                     len(result["hits"]), _source_qualified_ids(result["hits"]))

    return StreamingResponse(sse(), media_type="text/event-stream")
