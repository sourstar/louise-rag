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
    # the question path's models, loaded in the background (app/warmup.py;
    # operator, 2026-10-27): the first question after a restart waited ~65 s
    from . import warmup
    warmup.start()
    yield

app = FastAPI(title="louise-rag", lifespan=lifespan)
# who is asking, on every request; Ask closed to nobody-in-particular once a
# user exists (07 §7.4, amended 2026-10-22; app/users.py)
app.add_middleware(auth.ViewerGate)

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
from . import openai_stores  # noqa: E402
app.include_router(openai_stores.router)   # OpenAI's vector stores and files (07 §7.4, 2026-10-22)


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
    # the recipe TOML's text, or the table itself; absent: the node's own
    # recipe for the plugin (`collections.recipe_path`; RULE 2, 2026-10-25)
    recipe: str | dict | None = None
    space_from: str | None = None
    platform_image: str | None = None
    # this node's endpoint for it; absent: the node decides it from the
    # recipe's weights (`suggest_encoder`; RULE 2, 2026-10-25)
    encoder: str | None = None
    # a bi-encoder's query side (MedCPT): its endpoint. The Codex pins its
    # reference vectors too (09 §9.12), so `create` embeds the set with it
    query_encoder: str | None = None
    # the rest of the file's [encoder] (07 §7.8, amended 2026-10-09): the
    # wire, and each side's model — required where its URL names none
    api: str | None = None
    embed_model: str | None = None
    query_model: str | None = None
    name: str | None = None      # the local name; default: the plugin's (07 §7.8)
    # what the file takes and spends (07 §7.8): absent, the node decides —
    # for a create from the node's own recipe, every slice and "on-demand"
    # (DEFAULT_EMBED_MODE), as the Library's take holds the whole of what it
    # takes; for a create from a recipe given here, what the command writes
    # (no slice, embed off), as before
    slices: str | list[str] | None = None
    embed: str | None = None


def _plain_create_refusal(plugin: str) -> str:
    path = collections_mod.recipe_path(plugin)
    return (f"this node has no recipe for {plugin}: a new {plugin} collection's encoder, work "
            f"budget and description are its creator's choices, made once — on the command line "
            f"(`louise hold collection create {plugin} --recipe FILE`) or by saving the recipe as "
            f"{path} — after which the Library creates one from a name alone. A {plugin} "
            f"collection a peer offers is taken from \"Offered by peers\" with no recipe at all")


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
    # PLUG AND PLAY (RULE 2, amended 2026-10-25): the Library names a plugin
    # and a local name; the recipe is the node's own for that plugin
    recipe_text = body.recipe
    from_node = recipe_text is None
    if from_node:
        path = collections_mod.recipe_path(body.plugin)
        if not path.is_file():
            raise HTTPException(409, _plain_create_refusal(body.plugin))
        recipe_text = path.read_text(encoding="utf-8")
    try:
        recipe = (tomllib.loads(recipe_text) if isinstance(recipe_text, str)
                  else dict(recipe_text))
    except tomllib.TOMLDecodeError as e:
        raise HTTPException(409, f"the recipe is not TOML: {e}") from None
    encoder, query_encoder = body.encoder, body.query_encoder
    api, embed_model, query_model = body.api, body.embed_model, body.query_model
    decided = ""
    try:
        louise.preflight_create(recipe, body.platform_image)
        if not encoder:
            # the node decides the encoder from the recipe's weights — the
            # space of `space_from` where one is named — and refuses, in
            # plain words, where nothing it runs loads them: a Codex holds
            # reference vectors only its own encoder computes (06 §6.6)
            space = recipe.get("space")
            if body.space_from:
                from .hold import collection as col
                space = col.read(louise._default_root(), body.space_from).space
            from . import encoder_resolve
            if not encoder_resolve.space_weights(space)[0]:
                raise CodexError("the recipe names no space.encoder.weights, and a collection's "
                                 "encoder is found by its weights: a recipe carries its [space] "
                                 "(06 §6.6), or --space-from names a held collection's")
            s = suggest_encoder(None, space=space)
            if not s["resolved"]:
                raise CodexError(
                    f"{s['missing'] or 'the node could not decide the encoder'}. A new collection's "
                    f"description holds reference vectors only its own encoder computes (06 §6.6), "
                    f"so nothing was created; once the model is there, create it again — or name "
                    f"one by hand on the command line: --encoder URL --embed-model M")
            encoder, api = s["url"], s["api"]
            embed_model, query_model = s["embed_model"], s["query_model"]
            if encoder_resolve.has_query_encoder(space):
                query_encoder = encoder
            decided = s["note"]
        # the file's api and model keys (07 §7.8, amended 2026-10-09), refused
        # at once: an endpoint naming no model, with no key naming it, is a
        # refusal that needs no work — and never answered with the label
        louise.check_api(api)
        louise.model_keys(encoder, query_encoder, embed_model=embed_model,
                          query_model=query_model)
        slices = body.slices if body.slices is not None else ("all" if from_node else [])
        if isinstance(slices, str) and slices != "all":
            raise CodexError('slices is "all" or a list of slice names')
        embed = louise.check_embed_mode(body.embed or ("on-demand" if from_node else "off"))
    except CodexError as e:
        raise HTTPException(409, str(e)) from None
    cli = (f"louise hold collection create {body.plugin} --recipe "
           f"{collections_mod.recipe_path(body.plugin) if from_node else 'FILE'} --encoder {encoder}")
    if query_encoder:
        cli += f" --query-encoder {query_encoder}"
    for flag, value in (("--api", api), ("--embed-model", embed_model),
                        ("--query-model", query_model)):
        if value:
            cli += f" {flag} {value}"
    if body.space_from:
        cli += f" --space-from {body.space_from}"
    if body.name:
        cli += f" --name {body.name}"
    job_id = jobs.submit(jobs.CREATE, {
        # the text as sent: a TOML date is not JSON, and the job parses it
        "plugin": body.plugin, "recipe": recipe_text, "space_from": body.space_from,
        "platform_image": body.platform_image, "encoder": encoder,
        "query_encoder": query_encoder, "api": api,
        "embed_model": embed_model, "query_model": query_model,
        "name": body.name, "cli": cli, "slices": slices, "embed": embed})
    return JSONResponse(status_code=202, content={**jobs.view(jobs.get(job_id)),
                                                  "decided": decided, "slices": slices,
                                                  "embed": embed})


#: an uploaded claims file is held here until the worker's job reads it
CLAIMS_UPLOADS = "claims-uploads"
#: 09 §9.5 claims are one JSON object a line; a file beyond this is not a
#: claims file but a mistake, and the web process does not buffer it
CLAIMS_MAX_BYTES = 256 * 1024 * 1024


@app.post("/api/hold/collection/create-annotations", dependencies=[Depends(auth.require_admin)])
async def hold_collection_create_annotations(
        request: Request, name: str = "", annotates: str = "", from_snapshot: str = "",
        title: str = "", summary: str = "", annotator: str = "", snapshot: str = "",
        platform_image: str = "", dry_run: bool = False, container: str = "jsonl"):
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
    # PLUG AND PLAY (RULE 2, amended 2026-10-25): the name and the claims
    # file are the user's; the targets are the collections the claims' uids
    # name, each from its newest held snapshot, the title and summary are
    # derived and the annotator is the claims' contributor — all decided by
    # the node (`annotations.resolve_targets`), the flags still taken
    if not dry_run and not name:
        raise HTTPException(409, "create annotations takes a name: the new collection's local name "
                                 "(02 §2.10)")
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
        # several targets: comma-separated, one snapshot per target (02 §2.10, 2026-10-20);
        # none named: decided from the claims (RULE 2)
        targets = await run_in_threadpool(ann.resolve_targets, root, data, annotates or None,
                                          from_snapshot or None)
        annotates = ",".join(t for t, _ in targets)
        from_snapshot = ",".join(s for _, s in targets)
        if not dry_run:
            louise.preflight_create({}, platform_image or None)
            from .hold import collection as hold_col
            hold_col.check_name(name)
            if collections_mod.config_toml_path(name).exists() or hold_col.dir_of(root, name).exists():
                raise CodexError(f"a collection named {name!r} is already held: a name holds one "
                                 f"collection for its life (07 §7.8)")
        preview = await run_in_threadpool(
            ann.preview, root, targets=targets, container=container.strip() or "jsonl",
            claims=data, annotator=annotator or None)
    except (ann.AnnotationsError, CodexError, ValueError, FileNotFoundError) as e:
        raise HTTPException(409, str(e)) from None
    cli = (f"louise hold collection create annotations --annotates {annotates} "
           f"--from-snapshot {from_snapshot} --claims FILE --name {name or '<name>'}")
    for flag, value in (("--title", title), ("--summary", summary), ("--annotator", annotator),
                        ("--snapshot", snapshot), ("--container", container if container != "jsonl" else "")):
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
        "container": container.strip() or "jsonl",
        "title": title, "summary": summary, "annotator": annotator,
        "snapshot": snapshot, "platform_image": platform_image or None,
        "claims_path": str(saved), "cli": cli})
    return JSONResponse(status_code=202, content={**jobs.view(jobs.get(job_id)), "preview": preview})


@app.post("/api/hold/annotations/{name}/update", dependencies=[Depends(auth.require_admin)])
async def hold_annotations_update(name: str, request: Request, dry_run: bool = False):
    """`louise hold annotations update <name>` as one route (07 §7.4; 02
    §2.10; operator, 2026-10-23: annotations are updated, not replaced). The
    body is the new claims file (`application/x-ndjson`), the whole claim
    set from now on; an EMPTY body re-checks the claims held against the
    targets' newest snapshots. The same check runs first and answers at
    once — `dry_run` stops there; a refusal is a 409 naming its rule and
    queues nothing. Otherwise the update is QUEUED for the worker, 202,
    which holds the key that signs the next snapshot (app/jobs.py)."""
    import secrets
    from . import jobs, louise
    from .hold import annotations as ann
    from .hold.codex import CodexError
    parts, received = [], 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > CLAIMS_MAX_BYTES:
            raise HTTPException(413, f"the claims file is over {CLAIMS_MAX_BYTES // (1024 * 1024)} MB")
        parts.append(chunk)
    data = b"".join(parts)
    claims = data if data.strip() else None
    root = louise._default_root()
    try:
        preview = await run_in_threadpool(ann.update, root, name, claims=claims, dry_run=True, log=lambda m: None)
    except (ann.AnnotationsError, CodexError, ValueError, FileNotFoundError) as e:
        raise HTTPException(409, str(e)) from None
    cli = f"louise hold annotations update {name}" + (" --claims FILE" if claims is not None else "")
    view = {**preview.to_dict(), "cli": cli + (" --dry-run" if dry_run else "")}
    if dry_run:
        return view
    if not (preview.added or preview.changed or preview.tombstoned):
        return JSONResponse(status_code=200, content={**view, "why": f"nothing to update: the claims and the "
                                                                    f"records they name are as {preview.previous_id} "
                                                                    f"holds them"})
    saved = None
    if claims is not None:
        uploads = config.louise_root() / "state" / CLAIMS_UPLOADS
        uploads.mkdir(parents=True, exist_ok=True)
        saved = uploads / f"{name}-update-{secrets.token_hex(6)}.jsonl"
        saved.write_bytes(claims)
    job_id = jobs.submit(jobs.UPDATE_ANNOTATIONS, {"name": name, "claims_path": str(saved) if saved else None,
                                                   "cli": cli})
    return JSONResponse(status_code=202, content={**jobs.view(jobs.get(job_id)), "preview": view})


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


def _models_at(url: str, timeout: float = 3.0) -> list[str]:
    """The model names the node's model container serves (`GET /v1/models`,
    the OpenAI shape LocalAI and VeGa answer); [] when it does not answer —
    then the name is typed, as before."""
    import httpx
    try:
        r = httpx.get(f"{url.rstrip('/')}/v1/models", timeout=timeout)
        r.raise_for_status()
        data = r.json().get("data") or []
        return sorted({str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")})
    except Exception:   # noqa: BLE001 — a suggestion, never a verdict
        return []


def node_model_server(held=None) -> tuple[str, str]:
    """(url, api) of THIS node's model server, from what the node already
    knows (operator, 2026-10-20: "something already set at a system
    level"): the address the held collections' `[encoder]` blocks share —
    on the node LocalAI at :30286, five files, one address — else the
    platform's `[deposit] encoder`. NOT `VEGA_URL`: that is the receipts
    service beside the model server (:30439; München, 2026-10-20), which
    answers no model. ("", "") when the node knows none."""
    from collections import Counter
    held = collections_mod.load_all() if held is None else held
    seen = Counter((c.url.rstrip("/"), c.api) for c in held
                   if getattr(c, "url", None) and getattr(c, "api", None))
    if seen:
        return seen.most_common(1)[0][0]
    try:
        from . import platform_config
        dep = platform_config.load().deposit
        if dep.encoder:
            return dep.encoder.rstrip("/"), dep.api or "openai"
    except Exception:   # noqa: BLE001 — no platform.toml: the node knows no server
        pass
    return "", ""


def suggest_encoder(codex, *, space=None) -> dict:
    """What a collection's `[encoder]` says on THIS node, decided by the node
    (RULE 2, amended 2026-10-25): the model server is the node's
    (`node_model_server`); the model NAMES are, in order, a held
    collection's in the same space (the same `space.encoder.weights` and
    query weights: its file already says), else the models on that server
    whose stated runs load exactly those weights (app/encoder_resolve.py —
    a model counts only if its weights digest matches; a name never
    decides). `resolved` is whether the node can embed and search this
    collection as it stands; when it cannot, `missing` says in plain words
    what would supply it. `models` is the server's list, for the command
    line. Nothing here is a default encoder (operator, 2026-09-18): the file
    still names its own, and the create holds the server's stated run to
    the Codex's weights, so a wrong name is refused, never written.

    `space`: a recipe's `[space]` table, for a create, where no Codex exists
    yet; otherwise the Codex's."""
    from . import encoder_resolve
    held = collections_mod.load_all()
    url, api = node_model_server(held)
    out = {"url": url, "api": api, "embed_model": "", "query_model": "",
           "known_from": None, "models": [], "resolved": False, "missing": "", "note": ""}
    space = space if space is not None else getattr(codex, "space", None)
    weights, qweights, label = encoder_resolve.space_weights(space)
    if weights:
        for c in held:
            cw, cq, _ = encoder_resolve.space_weights(getattr(c.codex, "space", None))
            # both sides: a held file in another query space names the wrong query model
            if cw == weights and (cq or None) == qweights and getattr(c, "url", None):
                out.update(url=c.url.rstrip("/"), api=c.api, embed_model=c.model,
                           query_model=(c.query_model or c.model), known_from=c.name, resolved=True)
                break
    if out["url"]:
        out["models"] = _models_at(out["url"])
    if weights and out["url"] and not out["resolved"]:
        found = encoder_resolve.on_server(out["url"], out["api"], out["models"], space)
        if found is not None:
            out.update(embed_model=found[0], query_model=found[1], known_from="server", resolved=True)
    what = label or f"weights {str(weights)[:23]}…"
    if codex is None and space is None:
        out["note"] = "the collection's description is pulled when it is taken; the encoder is decided then"
    elif out["resolved"] and out["known_from"] == "server":
        out["note"] = (f"this node's model server runs this collection's encoder ({what}) as "
                       f"{out['embed_model']!r}" + (f" and {out['query_model']!r} for questions"
                                                    if out["query_model"] != out["embed_model"] else "")
                       + ": its stated run loads the very weights the collection names")
    elif out["resolved"]:
        out["note"] = (f"the same encoder as your collection {out['known_from']!r}: this node embeds "
                       f"and searches it as it does that one")
    elif not out["url"]:
        out["missing"] = ("this node knows no model server yet (no collection names one, and the "
                          "platform's [deposit] names no encoder): name one in platform.toml's "
                          "[deposit] encoder, and this node embeds and searches the collection")
        out["note"] = "held, not searched here: " + out["missing"]
    else:
        out["missing"] = (f"no model on this node's server ({out['url']}) serves this collection's "
                          f"encoder ({what}; weights {str(weights)[:23]}…): install it there — add "
                          f"the model to the server, behind VeGa so its runs state their weights — "
                          f"and this node embeds and searches the collection; until then it is "
                          f"held — its texts and the peers' vectors arrive — and not searched here")
        out["note"] = "held, not searched here: " + out["missing"]
    out["weights"] = {"document": weights, "query": qweights}
    return out


@app.get("/api/encoders/suggest", dependencies=[Depends(auth.require_admin)])
async def encoders_suggest(collection_id: str = ""):
    """The Library's take and create dialogs: the encoder block the node
    decides by itself (`suggest_encoder`, RULE 2). With a collection id the
    Codex is the catalogue's, where the offer has pulled it; without one,
    the server and its models alone."""
    codex = None
    cid = collection_id.strip()
    if cid:
        codex = await run_in_threadpool(_catalogue_codex, cid)
    return await run_in_threadpool(suggest_encoder, codex)


def _catalogue_codex(cid: str):
    from contextlib import closing
    from .share import catalogue, peers
    with closing(peers.connect()) as con:
        return catalogue.codex_of(con, cid)


class ShareFetchBody(BaseModel):
    collection_id: str
    name: str = ""
    slices: list[str] | str = "all"
    kinds: list[str] = Field(default_factory=lambda: ["records", "vectors"])
    encoder: str = ""
    api: str = ""
    embed_model: str = ""
    query_model: str = ""
    snapshot: str = ""
    #: 07 §7.8's embed mode for the file: what this node's encoder spends;
    #: "" = decided by the node (p2p where it can embed, off where it cannot)
    embed: str = ""


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
    ok, why = offer.can_take(cid)
    if not ok:
        # said here, before anything is queued, in the words the card shows —
        # not after a worker's fetch fails with "holds no snapshot" (Perugia,
        # 2026-10-20, the node's wikidata while its scan runs)
        raise HTTPException(409, f"this collection cannot be taken yet: {why}")
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
    encoder, api = body.encoder.strip(), body.api.strip()
    embed_model, query_model = body.embed_model.strip(), body.query_model.strip()
    missing = ""
    if not encoder:
        # PLUG AND PLAY (RULE 2): the node decides its encoder block — its
        # model server and the model names a held collection in the same
        # space already uses. Where it cannot, the collection is held without
        # one (the worker then says so and serves nothing of it) and the
        # answer says what would supply it; the take never asks a form.
        s = suggest_encoder(_catalogue_codex(cid))
        if s["resolved"]:
            encoder, api, embed_model, query_model = s["url"], s["api"], s["embed_model"], s["query_model"]
        else:
            missing = s["missing"] or "the node could not decide the encoder"
    try:
        louise.check_api(api or None)
        if encoder:
            louise.model_keys(encoder, embed_model=embed_model or None, query_model=query_model or None)
        embed = louise.check_embed_mode(body.embed.strip() or ("p2p" if encoder else "off"))
    except CodexError as e:
        raise HTTPException(409, str(e)) from None
    args = {"collection_id": cid, "name": name, "slices": slices, "kinds": kinds,
            "encoder": encoder, "api": api, "embed_model": embed_model,
            "query_model": query_model, "snapshot": body.snapshot.strip(), "embed": embed}
    job_id = jobs.submit(jobs.SHARE_FETCH, args)
    return JSONResponse(status_code=202, content={
        "id": job_id, "collection_id": cid, "args": args, "missing": missing,
        "cli": (f"louise share fetch {cid}" + ("" if slices == "all" else " --slice " + " ".join(slices))
                + f" --kinds {','.join(kinds)}" + (f" --encoder {encoder}" if encoder else "")
                + (f" --embed {embed}" if embed != "off" else "")
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
def control(body: ControlBody, request: Request):
    if body.state not in db.WORKER_STATES:
        raise HTTPException(400, f"state must be {'|'.join(db.WORKER_STATES)}")
    # node.sqlite, not the first collection's manifest: the ONE place every
    # process reads the node state from (app/node_state.py); the write
    # recorded with who made it (the caller's address and agent)
    client = request.client.host if request.client else "?"
    agent = (request.headers.get("user-agent") or "")[:80]
    node_state.set_worker_state(body.state, by=f"POST /api/control from {client} ({agent})")
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

#: the name the OpenAI-compatible endpoint answered to before `louise` and
#: `louise/<collection>` (07 §7.4, amended 2026-10-22): still taken, as every
#: collection, so a client configured with it keeps working
LEGACY_MODEL = "gutenberg-rag"


@app.get("/v1/models")
def models():
    """`louise` (every collection) and `louise/<name>` per collection a
    search may use: in a client that sends nothing but a model name (Open
    WebUI), choosing the model chooses what is searched."""
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "louise"}
                                       for m in web.model_ids()]}


def collections_for_model(model: str, collections: list[str] | None) -> list[str] | None:
    """The collections a chat searches: the body's own list when it sends
    one, else the one `louise/<name>` names, else every one."""
    if collections is not None:
        return collections
    if model.startswith("louise/") and model[len("louise/"):]:
        return [model[len("louise/"):]]
    if model in ("louise", LEGACY_MODEL, ""):
        return None
    raise HTTPException(404, f"model {model!r}: `louise` or `louise/<collection>` (GET /v1/models lists them)")

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
    model: str = "louise"
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
    # the hits the answer's [n] number, beside the answer (`louise_sources`):
    # one search for both, where a client would otherwise search again for
    # them (the Open WebUI pipe, deploy/openwebui/; 07 §7.4, 2026-10-22)
    sources: bool = False
    # the Ask page's own search (`X-Louise-Search` of /ask/query): the answer
    # is written from those hits instead of searching again; unknown or
    # expired, the answer searches as before
    search_id: str | None = None

def _text_of(content) -> str:
    """A message's text: a string, or OpenAI's list of parts (Open WebUI
    sends `[{"type": "text", "text": …}]` with an attachment)."""
    if isinstance(content, list):
        return " ".join(str(p.get("text") or "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text").strip()
    return str(content or "")


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatBody, request: Request):
    user_msgs = [m for m in body.messages if m.get("role") == "user"]
    if not user_msgs:
        raise HTTPException(400, "no user message")
    query = _text_of(user_msgs[-1].get("content", ""))
    searched = collections_for_model(body.model, body.collections)
    t0 = time.time()
    kept = web.kept_search(body.search_id, query)
    if kept is not None:
        # the page's hits, untouched; this request's own timings start at 0
        result = {**kept, "timings": {"total": 0.0, "search": "reused"}}
        return await run_in_threadpool(_chat_answer, body, query, result, t0)
    # The SEARCH stops if the caller goes (app/cancel.py). The answer after
    # it is one call to the model, which nothing can interrupt midway; it
    # runs in a worker thread so this endpoint never blocks the loop.
    try:
        result = await cancel.while_connected(
            request, _run_search, searched, query, body.limit, "hybrid", True,
            verify=body.verify, rerank_depth=body.rerank_depth,
            all_languages=body.all_languages,
            # the page's own "show results in", so a work's one hit is the
            # same edition in the answer's [n] as on the page
            **({"show_in": body.answer_lang} if body.answer_lang else {}),
            **({"annotations": body.annotations} if body.annotations is not None else {}))
    except cancel.Cancelled:
        return Response(status_code=CLIENT_GONE)
    return await run_in_threadpool(_chat_answer, body, query, result, t0)


def source_entries(hits: list[dict]) -> list[dict]:
    """The answer's [n], in order: what a client shows as its sources —
    the passage, where it is (a path of this node's reader), the work's
    other editions and the claims beside it (05 §5.3). Never a vector."""
    out = []
    for n, h in enumerate(hits, 1):
        coll = h.get("source_collection") or str(h.get("chunk_id", "")).split(":", 1)[0]
        bid = h.get("book_id")
        reader = f"/read/{coll}/{bid}?hl={h.get('chunk_id')}" if bid not in (None, "") else None
        out.append({
            "n": n, "title": h.get("title"), "authors": h.get("authors"), "lang": h.get("lang"),
            "collection": coll, "uid": h.get("uid"), "chunk_id": h.get("chunk_id"), "book_id": bid,
            "chapter": h.get("chapter"), "text": h.get("text"), "origin_url": h.get("origin_url"),
            "reader": reader,
            "editions": [{"uid": e.get("uid"), "title": e.get("title"), "lang": e.get("lang"),
                          "held": bool(e.get("held"))} for e in h.get("editions") or []],
            "annotations": [{"type": a.get("type"), "uid": (a.get("record") or {}).get("uid"),
                             "title": (a.get("record") or {}).get("title"),
                             "lang": (a.get("record") or {}).get("lang"), "state": a.get("state")}
                            for a in h.get("annotations") or []],
        })
    return out


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
        out = {
            "id": rid, "object": "chat.completion", "created": int(time.time()),
            "model": body.model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
        }
        if body.sources:
            out["louise_sources"] = source_entries(result["hits"])
        return out

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
                "model": body.model,
                "choices": [{"index": 0, "delta": {"content": delta},
                             "finish_reason": None}]}) + "\n\n"

        chars = 0
        first: float | None = None
        why: str | None = None
        if body.sources:
            # before the first word: an OpenAI client ignores the extra key
            yield "data: " + json.dumps({
                "id": rid, "object": "chat.completion.chunk", "created": int(time.time()),
                "model": body.model, "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                "louise_sources": source_entries(result["hits"])}, default=str) + "\n\n"
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
                "created": int(time.time()), "model": body.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"
        timings = dict(result["timings"])
        timings["total"] = round((time.time() - t0) * 1000, 1)
        db.log_query(con, "chat", query, "hybrid", body.limit, timings,
                     len(result["hits"]), _source_qualified_ids(result["hits"]))

    return StreamingResponse(sse(), media_type="text/event-stream")


# LAST: every command of the tree as one route (07 §7.4, app/api_commands.py).
# Registered after every explicit route, which therefore answers first.
from . import api_commands  # noqa: E402
app.include_router(api_commands.router)
