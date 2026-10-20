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
action, the same app/acquire/engine.py functions `louise hold records fetch` calls. Mutating
endpoints here (control/scope/discover/reset/reindex/POST-summary) are
gated by app.auth.require_admin (a single ADMIN_TOKEN, header or cookie);
read/query endpoints (status/stats/events/books/search/chat) stay open —
"Ask is LAN-open read-only" (architecture-v2.md §15).
"""
import contextvars
import json
import queue
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (HTMLResponse, RedirectResponse, Response, StreamingResponse,
                               JSONResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from . import auth, cancel, collections as collections_mod, query_service, web
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
# 07 §7.4: LocalAGI's knowledge-base routes, the second protocol alias
from . import localagi  # noqa: E402
app.include_router(localagi.router)
from .share import api as share_api  # noqa: E402
app.include_router(share_api.router)      # the peer messages of 09 §9.9
from .ask import api as ask_api  # noqa: E402
app.include_router(ask_api.router)        # `ask` over HTTP and the MCP server (07 §7.4; 05 §5.4)


@app.get("/.well-known/louise-certifier.json")
def certifier_anchor():
    """09 §9.11's anchor document for THIS node as a certifier: its key,
    valid from `[certifier] valid_from` (07 §7.8), never revoked here —
    a revocation is the operator's act, written into the platform file —
    and the collections held whose Codex names this key as certifier.
    Served where a domain the operator controls fronts the node with TLS
    (the domain's chain is the anchor; 03 §3.7); open, nothing secret."""
    from . import identity, platform_config
    try:
        own = identity.own()
    except Exception:   # noqa: BLE001 — a node without a key certifies nothing
        own = None
    if not own:
        raise HTTPException(404, "this node has no key, so it is nobody's certifier")
    cfg = getattr(platform_config.load(), "certifier", None)
    valid_from = getattr(cfg, "valid_from", None) or "1970-01-01T00:00:00Z"
    revoked_at = getattr(cfg, "revoked_at", None)
    certified = []
    # `skip_unreadable` answers `(served, [(name, reason)…])` — the worker's
    # shape; iterating the pair listed nothing (found live 2026-10-07)
    served, _refused = collections_mod.load_all(skip_unreadable=True)
    for c in served:
        codex = getattr(c, "codex", None)
        cert = getattr(codex, "certifier", None) if codex is not None else None
        if cert is not None and getattr(codex, "publication", "open") == "certified" and cert.key == own:
            certified.append(codex.collection_id)
    doc = {"kind": "louise-certifier", "version": 1,
           "keys": [{"key": own, "valid_from": valid_from, "revoked_at": revoked_at}],
           "collections": sorted(certified)}
    return Response(json.dumps(doc, sort_keys=True, separators=(",", ":")), media_type="application/json")


@app.get("/.well-known/louise-pointer/{collection_hex}.json")
def certifier_pointer(collection_hex: str):
    """09 §9.11: the certifier's signed pointer to its current certified
    snapshot, served where a certifier's domain publishes it — this node
    is that domain's origin when the operator fronts it with one. Open,
    like every `ask` route: it carries nothing secret (07 §7.4)."""
    from .hold import certify, shard as shard_mod
    if not collection_hex.isalnum() or len(collection_hex) != 64:
        raise HTTPException(404, "no pointer")
    root = collections_mod.louise_root() / "collections"
    served, _refused = collections_mod.load_all(skip_unreadable=True)
    for c in served:
        codex = getattr(c, "codex", None)
        if codex is None or shard_mod._hex(codex.collection_id) != collection_hex:
            continue
        p = certify.pointer_path(root, c.name, codex)
        if p.is_file():
            return Response(p.read_bytes(), media_type="application/json")
    raise HTTPException(404, "no pointer")
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
            "chat_model": config.CHAT_MODEL,
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

class HoldCreateBody(BaseModel):
    plugin: str
    recipe: str | dict           # the recipe TOML's text, or the table itself
    space_from: str | None = None
    platform_image: str | None = None
    encoder: str | None = None   # this node's endpoint for it — required, no default
    # a bi-encoder's query side (MedCPT): its endpoint. The Codex pins its
    # reference vectors too (09 §9.12), so `create` embeds the set with it
    query_encoder: str | None = None
    # the rest of the file's [encoder] (07 §7.8, amended 2026-10-09): the
    # wire, and each side's model — required where its URL names none
    api: str | None = None
    embed_model: str | None = None
    query_model: str | None = None
    name: str | None = None      # the local name; default: the plugin's (07 §7.8)


@app.post("/api/hold/collection/create", dependencies=[Depends(auth.require_admin)])
def hold_collection_create(body: HoldCreateBody):
    """`louise hold collection create` as one route (07 §7.4): the body's
    keys are the command's flags, and a refusal (exit 2) answers 409. It
    writes the Codex and the collection file (07 §7.8) — what the Library's
    *new collection* is now (07 §7.6), in place of the form that wrote a
    file naming no Codex.

    QUEUED FOR THE WORKER, 202 (operator, 2026-09-29). Creating signs the
    calibration member with the node's private key, and this process holds
    only the public half (app/identity.py). The refusals that need no work
    are answered here, at once; the rest is the job's, read at
    `GET /api/jobs/<id>` (app/jobs.py)."""
    import tomllib
    from . import jobs, louise
    from .hold.codex import CodexError
    if body.plugin not in collections_mod.PLUGIN_REGISTRY:
        raise HTTPException(409, f"unknown plugin: {body.plugin!r}")
    try:
        recipe = (tomllib.loads(body.recipe) if isinstance(body.recipe, str)
                  else dict(body.recipe))
    except tomllib.TOMLDecodeError as e:
        raise HTTPException(409, f"the recipe is not TOML: {e}") from None
    try:
        louise.preflight_create(recipe, body.platform_image)
        if not body.encoder:
            raise CodexError(louise.NO_ENCODER)
        # the file's api and model keys (07 §7.8, amended 2026-10-09), refused
        # at once: an endpoint naming no model, with no key naming it, is a
        # refusal that needs no work — and never answered with the label
        louise.check_api(body.api)
        louise.model_keys(body.encoder, body.query_encoder, embed_model=body.embed_model,
                          query_model=body.query_model)
    except CodexError as e:
        raise HTTPException(409, str(e)) from None
    cli = f"louise hold collection create {body.plugin} --recipe FILE --encoder {body.encoder}"
    if body.query_encoder:
        cli += f" --query-encoder {body.query_encoder}"
    for flag, value in (("--api", body.api), ("--embed-model", body.embed_model),
                        ("--query-model", body.query_model)):
        if value:
            cli += f" {flag} {value}"
    if body.space_from:
        cli += f" --space-from {body.space_from}"
    if body.name:
        cli += f" --name {body.name}"
    job_id = jobs.submit(jobs.CREATE, {
        # the text as sent: a TOML date is not JSON, and the job parses it
        "plugin": body.plugin, "recipe": body.recipe, "space_from": body.space_from,
        "platform_image": body.platform_image, "encoder": body.encoder,
        "query_encoder": body.query_encoder, "api": body.api,
        "embed_model": body.embed_model, "query_model": body.query_model,
        "name": body.name, "cli": cli})
    return JSONResponse(status_code=202, content=jobs.view(jobs.get(job_id)))


#: an uploaded claims file is held here until the worker's job reads it
CLAIMS_UPLOADS = "claims-uploads"
#: 09 §9.5 claims are one JSON object a line; a file beyond this is not a
#: claims file but a mistake, and the web process does not buffer it
CLAIMS_MAX_BYTES = 256 * 1024 * 1024


@app.post("/api/hold/collection/create-annotations", dependencies=[Depends(auth.require_admin)])
async def hold_collection_create_annotations(
        request: Request, name: str = "", annotates: str = "", from_snapshot: str = "",
        title: str = "", summary: str = "", annotator: str = "", snapshot: str = "",
        platform_image: str = "", dry_run: bool = False):
    """`louise hold collection create annotations` as one route (07 §7.4;
    02 §2.10), the Library's *new collection* of the annotations type
    (2026-10-19). The flags are QUERY parameters and the claims file is the
    RAW request body (`application/x-ndjson`) — no multipart, which would
    need python-multipart, a dependency the platform does not carry (the
    bundle import route is built the same way). `dry_run` is `--dry-run`:
    the claims checked against the target snapshot and counted, nothing
    written, answered at once. Otherwise the same check runs first — a
    refusal is a 409 that names its rule and queues nothing — and the
    create is QUEUED for the worker, 202, which holds the key that signs
    the first snapshot (app/jobs.py); the uploaded file waits under the
    node's state until the job has read it."""
    import secrets
    from . import jobs, louise
    from .hold import annotations as ann
    from .hold.codex import CodexError
    name, annotates, from_snapshot = name.strip(), annotates.strip(), from_snapshot.strip()
    title, summary, annotator, snapshot = title.strip(), summary.strip(), annotator.strip(), snapshot.strip()
    missing = [f for f, v in (("annotates", annotates), ("from_snapshot", from_snapshot)) if not v]
    if not dry_run and not name:
        missing.append("name")
    if missing:
        raise HTTPException(409, f"create annotations takes {', '.join(missing)}: the collection it "
                                 f"annotates, the target snapshot the claims hold from and the new "
                                 f"collection's name (02 §2.10)")
    # the body, bounded as it streams: a claims file is one JSON object a
    # line, and one beyond this is a mistake the web process does not buffer
    parts, received = [], 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > CLAIMS_MAX_BYTES:
            raise HTTPException(413, f"the claims file is over {CLAIMS_MAX_BYTES // (1024 * 1024)} MB")
        parts.append(chunk)
    data = b"".join(parts)
    if not data.strip():
        raise HTTPException(409, "the claims file is the request body, and it is empty (09 §9.5: one "
                                 "JSON claim per line)")
    root = louise._default_root()
    try:
        if not dry_run:
            louise.preflight_create({}, platform_image or None)
            from .hold import collection as hold_col
            hold_col.check_name(name)
            if collections_mod.config_toml_path(name).exists() or hold_col.dir_of(root, name).exists():
                raise CodexError(f"a collection named {name!r} is already held: a name holds one "
                                 f"collection for its life (07 §7.8)")
        preview = await run_in_threadpool(
            ann.preview, root, target_ref=annotates, from_snapshot=from_snapshot,
            claims=data, annotator=annotator or None)
    except (ann.AnnotationsError, CodexError, ValueError, FileNotFoundError) as e:
        raise HTTPException(409, str(e)) from None
    cli = (f"louise hold collection create annotations --annotates {annotates} "
           f"--from-snapshot {from_snapshot} --claims FILE --name {name or '<name>'}")
    for flag, value in (("--title", title), ("--summary", summary), ("--annotator", annotator),
                        ("--snapshot", snapshot)):
        if value:
            cli += f" {flag} {value!r}"
    if dry_run:
        return {**preview, "cli": cli + " --dry-run"}
    uploads = config.louise_root() / "state" / CLAIMS_UPLOADS
    uploads.mkdir(parents=True, exist_ok=True)
    saved = uploads / f"{name}-{secrets.token_hex(6)}.jsonl"
    saved.write_bytes(data)
    job_id = jobs.submit(jobs.CREATE_ANNOTATIONS, {
        "name": name, "annotates": annotates, "from_snapshot": from_snapshot,
        "title": title, "summary": summary, "annotator": annotator,
        "snapshot": snapshot, "platform_image": platform_image or None,
        "claims_path": str(saved), "cli": cli})
    return JSONResponse(status_code=202, content={**jobs.view(jobs.get(job_id)), "preview": preview})


@app.get("/api/node/show", dependencies=[Depends(auth.require_admin)])
def node_show_route():
    """07 §7.3's `node show` over HTTP (07 §7.4): the mode and overrides in
    force, each collection's state and stage, the last round, the jobs."""
    from . import node_show
    return node_show.show()


@app.get("/api/share/offer/show", dependencies=[Depends(auth.require_admin)])
def share_offer_show():
    """`louise share offer` (07 §7.3; 04 §4.2): the collections neighbours
    announce that this node does not hold, with who serves each."""
    from .share import offer
    return {"offered": offer.offered_for_node()}


class ShareFetchBody(BaseModel):
    collection_id: str
    name: str = ""
    slices: list[str] | str = "all"
    kinds: list[str] = Field(default_factory=lambda: ["records"])
    encoder: str = ""
    api: str = ""
    embed_model: str = ""
    query_model: str = ""
    snapshot: str = ""


@app.post("/api/share/fetch", dependencies=[Depends(auth.require_admin)])
def share_fetch(body: ShareFetchBody):
    """`louise share fetch <collection>` as the Library queues it from the
    offer (07 §7.4; 04 §4.2, operator 2026-10-20): the flags are the body's
    keys. Refused at once (409) when nobody announced the collection — the
    command's `--from ADDRESS` takes one from a peer given by hand — when the
    name is a collection this node already holds, or when the encoder's
    model keys do not read (07 §7.8). Otherwise QUEUED for the worker (202),
    which holds the node key the pulls are signed with (app/jobs.py), and
    `GET /api/jobs/<id>` follows it."""
    from . import jobs, louise
    from .hold.codex import CodexError
    from .share import offer
    cid = body.collection_id.strip()
    if not cid.startswith("sha256:"):
        raise HTTPException(409, "a collection is taken by its id (sha256:…), as the offer lists it")
    if not offer.announced(cid):
        raise HTTPException(409, f"no live neighbour's digest lists {cid[:23]}… and no Codex of it is in the "
                                 f"catalogue: it is not in the offer; `louise share fetch {cid} --from ADDRESS` "
                                 f"takes it from a peer given by hand")
    name = body.name.strip()
    if name and collections_mod.by_name(name) is not None:
        raise HTTPException(409, f"{name!r} is a collection this node already holds; choose another local name")
    slices = body.slices
    if isinstance(slices, str):
        slices = slices.strip()
        if slices and slices != "all":
            raise HTTPException(409, "slices is \"all\" or a list of slice names as the snapshot lists them")
        slices = "all"
    else:
        slices = [s.strip() for s in slices if s and s.strip()] or "all"
    kinds = [k.strip() for k in body.kinds if k and k.strip()]
    if not kinds:
        raise HTTPException(409, "kinds names at least one member kind (records, vectors)")
    encoder = body.encoder.strip()
    try:
        louise.check_api(body.api.strip() or None)
        if encoder:
            louise.model_keys(encoder, embed_model=body.embed_model.strip() or None,
                              query_model=body.query_model.strip() or None)
    except CodexError as e:
        raise HTTPException(409, str(e)) from None
    args = {"collection_id": cid, "name": name, "slices": slices, "kinds": kinds,
            "encoder": encoder, "api": body.api.strip(), "embed_model": body.embed_model.strip(),
            "query_model": body.query_model.strip(), "snapshot": body.snapshot.strip()}
    job_id = jobs.submit(jobs.SHARE_FETCH, args)
    return JSONResponse(status_code=202, content={
        "id": job_id, "collection_id": cid, "args": args,
        "cli": (f"louise share fetch {cid}" + ("" if slices == "all" else " --slice " + " ".join(slices))
                + f" --kinds {','.join(kinds)}" + (f" --encoder {encoder}" if encoder else "")
                + (f" --name {name}" if name else ""))})


@app.get("/api/jobs/{job_id}", dependencies=[Depends(auth.require_admin)])
def job_status(job_id: int):
    """A job the web queued for the worker (app/jobs.py): its state, its
    result or its failure, and why it waits if it does."""
    from . import jobs
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job {job_id}")
    return jobs.view(job)


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
    collection = _collection()
    # invariant 9, "not discovered" (verification pass, 2026-09-13)
    web._refuse_withheld(collection, "its corpus is discovered")
    con = _con()
    return discovery.sync_collection(con, collection)

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
    # invariant 9: the catalogue is the collection's data (audit, 2026-09-13)
    web._refuse_withheld(_collection(), "its catalogue is served")
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
    # invariant 9: a document's record is the collection's data (audit)
    web._refuse_withheld(_collection(), "its catalogue is served")
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
    # invariant 9: a summary sends the book's text to the LLM — use
    web._refuse_withheld(_collection(), "its text is sent to the LLM for a summary")
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
    # invariant 9: a cached summary is text derived from the corpus (audit)
    web._refuse_withheld(_collection(), "its summaries are served")
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
    # 05 §5.6: how many of each list the reranker orders — 50 by default
    rerank_depth: int = Field(retrieval.RERANK_TOP_N, ge=1, le=retrieval.MAX_RERANK_DEPTH)
    all_languages: bool = False            # every language held, not only the main ones
    # the annotations collections consulted (05 §5.3, amended 2026-10-19):
    # None = every one held, [] = none, names = those
    annotations: list[str] | None = None

#: what a caller that disconnected mid-search is answered: nothing it will
#: read. nginx's "client closed request"; the search behind it has stopped.
CLIENT_GONE = 499


def _search_and_log(body: SearchBody) -> dict:
    result = _run_search(body.collections, body.query, body.k, body.mode, body.translate,
                         **({"annotations": body.annotations} if body.annotations is not None else {}),
                          verify=body.verify, rerank_depth=body.rerank_depth,
                          all_languages=body.all_languages)
    con = _con()   # query_log lives in the default collection's manifest regardless
    db.log_query(con, "search", body.query, body.mode, body.k, result["timings"],
                 len(result["hits"]), _source_qualified_ids(result["hits"]))
    return result


@app.post("/api/search")
async def api_search(body: SearchBody, request: Request):
    """The search runs in a worker thread for as long as its caller is there;
    a caller that disconnects stops it (app/cancel.py) — and is not logged,
    since it was never answered."""
    try:
        return await cancel.while_connected(request, _search_and_log, body)
    except cancel.Cancelled:
        return Response(status_code=CLIENT_GONE)

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

#: an SSE comment every so often while an answer has nothing to show yet
#: (`_chat_answer`): the stream is never silent for longer than this
KEEPALIVE_S = 5.0


class ChatBody(BaseModel):
    model: str = "gutenberg-rag"
    messages: list[dict]
    stream: bool = False
    temperature: float = 0.3
    collections: list[str] | None = None   # None = all enabled
    verify: bool = False                   # blocking verify-on-use (§6 T4)
    answer_lang: str | None = None         # ISO code: answer in this language (Ask's "show results in")
    # the sources' search: results after the reranker and its depth — the
    # Ask page sends its own two settings, so the answer's [n] and the
    # page's hits come from the same search
    limit: int = Field(retrieval.DEFAULT_RESULTS, ge=1, le=retrieval.MAX_RESULTS)
    rerank_depth: int = Field(retrieval.RERANK_TOP_N, ge=1, le=retrieval.MAX_RERANK_DEPTH)
    all_languages: bool = False            # the page's own choice, so both searches match
    # the page's choice of annotations collections, so the answer's [n] are
    # the page's hits (05 §5.3, amended 2026-10-19); None = every one held
    annotations: list[str] | None = None

@app.post("/v1/chat/completions")
async def chat_completions(body: ChatBody, request: Request):
    user_msgs = [m for m in body.messages if m.get("role") == "user"]
    if not user_msgs:
        raise HTTPException(400, "no user message")
    query = str(user_msgs[-1].get("content", ""))
    t0 = time.time()
    # The SEARCH stops if the caller goes (app/cancel.py). The answer after
    # it is one call to the model, which nothing can interrupt midway; it
    # runs in a worker thread so this endpoint never blocks the loop.
    try:
        result = await cancel.while_connected(
            request, _run_search, body.collections, query, body.limit, "hybrid", True,
            verify=body.verify, rerank_depth=body.rerank_depth,
            all_languages=body.all_languages,
            # the page's own "show results in", so a work's one hit is the
            # same edition in the answer's [n] as on the page
            **({"show_in": body.answer_lang} if body.answer_lang else {}),
            **({"annotations": body.annotations} if body.annotations is not None else {}))
    except cancel.Cancelled:
        return Response(status_code=CLIENT_GONE)
    return await run_in_threadpool(_chat_answer, body, query, result, t0)


def _chat_answer(body: ChatBody, query: str, result: dict, t0: float):
    con = _con()   # query_log lives in the default collection's manifest regardless
    messages = retrieval.build_answer_messages(query, result, answer_lang=body.answer_lang)
    # keep prior turns for conversational context (before the sources turn)
    history = [m for m in body.messages[:-1] if m.get("role") in ("user", "assistant")]
    messages = [messages[0]] + history[-6:] + [messages[1]]
    rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if not body.stream:
        answer = llm.chat(messages, temperature=body.temperature)
        result["timings"]["llm"] = round((time.time() - t0) * 1000 - result["timings"]["total"], 1)
        result["timings"]["total"] = round((time.time() - t0) * 1000, 1)
        db.log_query(con, "chat", query, "hybrid", body.limit, result["timings"],
                     len(result["hits"]), _source_qualified_ids(result["hits"]))
        return {
            "id": rid, "object": "chat.completion", "created": int(time.time()),
            "model": "gutenberg-rag",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
        }

    def sse():
        # THE ANSWER'S STREAM, RELAYED FROM A THREAD (2026-10-13: "the answer
        # was cut", and nothing had recorded how it ended). `pump` reads the
        # endpoint, so this generator never blocks longer than KEEPALIVE_S:
        # while nothing has arrived — the cold start, the prompt, a model
        # that thinks before it writes — an SSE comment goes out instead,
        # which the page ignores and anything on the way counts as traffic.
        # And the stream's END is said: an answer the endpoint capped, failed
        # or closed early gets a visible note and a warn line, where before
        # it ended exactly like a whole one. Every answer gets one log line
        # with its size and timing, so the next report has a measurement.
        q: queue.Queue = queue.Queue()
        stop = threading.Event()

        def pump() -> None:
            try:
                it = llm.chat_stream(messages, temperature=body.temperature)
                while not stop.is_set():
                    try:
                        q.put(("delta", next(it)))
                    except StopIteration as s:
                        q.put(("end", s.value))
                        return
                it.close()          # the reader left: the endpoint stops writing
            except BaseException as e:      # noqa: BLE001 — said to the reader below
                q.put(("error", e))
        threading.Thread(target=contextvars.copy_context().run, args=(pump,),
                         name="chat-stream", daemon=True).start()

        def chunk(delta: str) -> str:
            return "data: " + json.dumps({
                "id": rid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": "gutenberg-rag",
                "choices": [{"index": 0, "delta": {"content": delta},
                             "finish_reason": None}]}) + "\n\n"

        chars = 0
        first: float | None = None
        why: str | None = None
        try:
            while True:
                try:
                    kind, value = q.get(timeout=KEEPALIVE_S)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if kind == "delta":
                    if first is None:
                        first = time.time() - t0
                    chars += len(value)
                    yield chunk(value)
                    continue
                if kind == "error":
                    why = f"the endpoint failed ({type(value).__name__}: {value})"
                elif value is None or not value.whole:
                    why = value.why() if value is not None else "the stream said no end"
                break
        except GeneratorExit:
            stop.set()
            raise
        secs = time.time() - t0
        if why is not None:
            yield chunk(f"\n[answer cut: {why}]")
            db.log_event(con, "warn", "chat",
                         f"answer cut after {chars} chars, {secs:.1f} s: {why}")
        elif first is None:
            db.log_event(con, "info", "chat", f"answer: empty, {secs:.1f} s")
        else:
            db.log_event(con, "info", "chat",
                         f"answer: {chars} chars in {secs:.1f} s, first word at {first:.1f} s")
        done = {"id": rid, "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "gutenberg-rag",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"
        timings = dict(result["timings"])
        timings["total"] = round((time.time() - t0) * 1000, 1)
        db.log_query(con, "chat", query, "hybrid", body.limit, timings,
                     len(result["hits"]), _source_qualified_ids(result["hits"]))

    return StreamingResponse(sse(), media_type="text/event-stream")
