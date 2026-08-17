"""Server-rendered Ask/Library/Network/System pages (architecture-v2.md
§15) over the same manifest tables and code paths the JSON API and CLI
already use — FastAPI + Jinja + htmx + SSE, no SPA, no build step, no
client state store.

`ask_router` is LAN-open, read-only (§15: "Ask is LAN-open read-only").
`admin_router` (prefix /admin) is gated by app.auth.require_admin on every
route (§14: "authenticated /admin API, strictly separate from the
OpenAI-compatible query endpoint"). /admin/login lives outside both routers
(it must be reachable unauthenticated) and sets the admin_token cookie
require_admin also accepts, so plain page navigation and htmx GET polling —
which can't attach a custom header per request — stay authenticated after
one login.

Library's grow action calls app.acquire.engine.plan_acquisition/
run_acquisition directly — the exact same functions app.cli's `corpus
add|grow` call — so the CLI and the web UI are provably the same code path,
not two implementations that happen to agree today (see
tests/test_web_shared_cli_path.py). Actions with no real CLI verb behind
them (pause/resume, per-collection reset, rescan) stay frictionless
one-click calls with no CLI-equivalent string in their modal, rather than
fabricating one.
"""
import json
import secrets
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import (auth, collections as collections_mod, config, db, discovery,
                platform_config, query_service, spaces)
from .transfer import manager as transfer_manager
from .transfer import schedule as transfer_schedule
from .acquire import engine as acquire_engine
from .acquire.spec import DiskPreflightError, LicenseAckRequiredError
from .acquire.specs import ACQUIRE_REGISTRY

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

ask_router = APIRouter()
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(auth.require_admin)])


def _collection_or_404(name: str) -> collections_mod.Collection:
    c = collections_mod.by_name(name)
    if c is None:
        raise HTTPException(404, f"no such collection: {name!r}")
    return c


def _spec_or_400(collection: collections_mod.Collection):
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        raise HTTPException(400, f"no AcquisitionSpec registered for plugin {collection.plugin!r}")
    return spec


# ---------------- Ask (open, read-only) ----------------

class AskQueryBody(BaseModel):
    query: str
    collections: list[str] | None = None
    mode: str = "hybrid"


@ask_router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "ask.html", {
        "active": "ask", "collections": collections_mod.load_all(), "result": None})


@ask_router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """Platform + UI documentation. Open like Ask: help must be readable
    BEFORE login (it explains how login works)."""
    return TEMPLATES.TemplateResponse(request, "help.html", {"active": "help"})


@ask_router.post("/ask/query", response_class=HTMLResponse)
def ask_query(request: Request, body: AskQueryBody):
    result = query_service.run_search(body.collections, body.query, 12, body.mode, True)
    con = db.connect()   # query_log lives in the default collection's manifest regardless
    db.log_query(con, "ask", body.query, body.mode, 12, result["timings"],
                 len(result["hits"]), query_service.source_qualified_ids(result["hits"]))
    return TEMPLATES.TemplateResponse(request, "_ask_results.html", {"result": result})


# ---------------- admin login (outside admin_router — must be reachable
# unauthenticated) ----------------

class LoginBody(BaseModel):
    token: str


@ask_router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "login.html", {
        "active": "", "admin_token_configured": bool(config.ADMIN_TOKEN)})


@ask_router.post("/admin/login")
def login(body: LoginBody, response: Response):
    if not config.ADMIN_TOKEN or not secrets.compare_digest(body.token, config.ADMIN_TOKEN):
        raise HTTPException(401, "invalid token")
    response.set_cookie("admin_token", body.token, httponly=True, samesite="lax",
                         max_age=60 * 60 * 24 * 30)
    return {"ok": True}


# ---------------- Library ----------------

def _collection_shelf(collection: collections_mod.Collection) -> dict:
    con = db.connect(collection)
    counts = con.execute("""
        SELECT COUNT(*) total, SUM(in_scope) in_scope,
          SUM(CASE WHEN in_scope=1 AND rdf_done=1 THEN 1 ELSE 0 END) rdf_done,
          SUM(CASE WHEN in_scope=1 AND fts_done=1 THEN 1 ELSE 0 END) fts_done,
          SUM(CASE WHEN in_scope=1 AND embed_done=1 THEN 1 ELSE 0 END) embed_done
        FROM documents""").fetchone()
    queue = con.execute("SELECT COUNT(*) c FROM embed_queue").fetchone()["c"]
    space_row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    last_discover = con.execute(
        "SELECT ts FROM events WHERE stage='discover' ORDER BY id DESC LIMIT 1").fetchone()

    fetched = None
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if collection.acquire_enabled and spec is not None:
        fc = con.execute(
            "SELECT COUNT(*) total, SUM(fetch_done) done FROM fetch_files WHERE source_id=?",
            (spec.source_id,)).fetchone()
        fetched = {"done": fc["done"] or 0, "total": fc["total"] or 0}

    if last_discover:
        freshness = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_discover["ts"]))
    else:
        freshness = "never scanned"

    return {
        "name": collection.name, "plugin": collection.plugin,
        "profile": collection.profile or "(hand-populated)",
        "space_id": ((space_row["space_id"][:10] + "…") if space_row and space_row["space_id"]
                      else "unpinned"),
        "fetched": fetched,
        "total": counts["total"] or 0, "in_scope": counts["in_scope"] or 0,
        "rdf_done": counts["rdf_done"] or 0, "fts_done": counts["fts_done"] or 0,
        "embed_done": counts["embed_done"] or 0,
        "queue": queue, "last_discover_text": freshness,
    }


@admin_router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    collections = collections_mod.load_all()
    con = db.connect()   # worker_state is one global control-table toggle (see worker.py)
    return TEMPLATES.TemplateResponse(request, "library.html", {
        "active": "library",
        "shelves": [_collection_shelf(c) for c in collections],
        "plugins": sorted(collections_mod.PLUGIN_REGISTRY),
        "worker_state": db.worker_state(con),
    })


class GrowBody(BaseModel):
    profile: str


@admin_router.post("/library/{name}/grow/plan")
def grow_plan(name: str, body: GrowBody):
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    con = db.connect(collection)
    plan = acquire_engine.plan_acquisition(con, collection, spec, body.profile)
    cli = f"python -m app.cli corpus grow {collection.name} --profile {body.profile} --dry-run"
    return {"plan": asdict(plan), "cli": cli}


@admin_router.post("/library/{name}/grow/run")
def grow_run(name: str, body: GrowBody):
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    con = db.connect(collection)
    try:
        result = acquire_engine.run_acquisition(con, collection, spec, body.profile)
    except LicenseAckRequiredError as e:
        raise HTTPException(409, str(e))
    except DiskPreflightError as e:
        raise HTTPException(507, str(e))
    db.log_event(con, "info", "control", f"grow to profile {body.profile!r} via admin UI")
    return result


class LibraryResetBody(BaseModel):
    stage: str
    book_ids: list[int] = []


@admin_router.post("/library/{name}/reset")
def library_reset(name: str, body: LibraryResetBody):
    collection = _collection_or_404(name)
    con = db.connect(collection)
    try:
        db.apply_reset(con, body.stage, body.book_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log_event(con, "info", "control", f"reset {body.stage} ({len(body.book_ids) or 'all'})")
    return {"ok": True}


@admin_router.post("/library/{name}/discover")
def library_discover(name: str):
    collection = _collection_or_404(name)
    con = db.connect(collection)
    return discovery.sync_collection(con, collection)


class NewCollectionBody(BaseModel):
    name: str
    plugin: str
    profile: str = "demo"


@admin_router.post("/library/new/preview")
def new_collection_preview(body: NewCollectionBody):
    if body.plugin not in collections_mod.PLUGIN_REGISTRY:
        raise HTTPException(400, f"unknown plugin: {body.plugin!r}")
    spec = ACQUIRE_REGISTRY.get(body.plugin)
    terms_text = None
    ack = None
    license_class = spec.license_class.value if spec else "unknown"
    if spec and spec.terms_text_path and spec.terms_text_path.is_file():
        terms_text = spec.terms_text_path.read_text()[:2000]
        ack = acquire_engine.make_ack(spec.terms_text_path)
    toml_lines = [f'plugin = "{body.plugin}"', f'profile = "{body.profile}"', "acquire_enabled = true"]
    if ack:
        toml_lines.append(f'license_ack = "{ack}"')
    return {
        "license_class": license_class, "terms_text": terms_text, "ack": ack,
        "toml": "\n".join(toml_lines), "toml_path": f"collections/{body.name}.toml",
        "instructions": "Save this as the path above, then commit it — versioned Tier-2 config, "
                         "not a DB write (CLAUDE.md invariant 10). Restart the app to pick it up, "
                         "then run the CLI command below to see real size/ETA estimates.",
        "cli": f"python -m app.cli corpus add {body.name} --profile {body.profile} --dry-run",
    }


# ---------------- Network ----------------

@admin_router.get("/network", response_class=HTMLResponse)
def network_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "network.html", {"active": "network"})


def _bandwidth_payload() -> dict:
    """Live per-class rates from the transfer manager (P0.7). Split out from
    bandwidth_stream()'s infinite generator so it's testable without consuming
    a live, never-ending SSE stream (a sync generator blocked in time.sleep()
    inside a worker thread can't be reliably cancelled mid-sleep by anyio, so
    driving the real endpoint to completion in-process is a hang risk).

    Note this reports the WEB process's own buckets. Acquisition runs in the
    worker process, so its bytes appear here only for transfers the web
    process itself performs; a cross-process rates feed is a later task and is
    labelled as such in the UI rather than silently implied.
    """
    return transfer_manager.get_manager().rates()


@admin_router.get("/network/bandwidth/stream")
def bandwidth_stream():
    def gen():
        while True:
            yield f"data: {json.dumps(_bandwidth_payload())}\n\n"
            time.sleep(2)
    return StreamingResponse(gen(), media_type="text/event-stream")


class BandwidthOverrideBody(BaseModel):
    rate: str | None = None          # e.g. "10Mbit"; None clears the override
    hours: int = 12


@admin_router.post("/network/bandwidth/override")
def set_bandwidth_override(body: BandwidthOverrideBody):
    """architecture-v2.md §14's "boost bandwidth tonight" — a Tier-3 runtime
    verb, NOT desired state: it carries an expiry and is never written to
    platform.toml (CLAUDE.md invariant 10)."""
    try:
        rate_bps = transfer_schedule.parse_rate(body.rate)
    except transfer_schedule.ScheduleError as e:
        raise HTTPException(400, str(e))
    con = db.connect()
    db.set_bandwidth_override(con, rate_bps, ttl_seconds=max(1, body.hours) * 3600)
    # apply to this process too, so its own SSE feed reflects the change at once
    transfer_manager.get_manager().set_override(rate_bps)
    db.log_event(con, "info", "control",
                 f"bandwidth override {'cleared' if rate_bps is None else body.rate} "
                 f"({body.hours}h)")
    return {"ok": True, "rate_bps": rate_bps, "hours": body.hours}


# ---------------- System ----------------

def _effective_config() -> dict:
    tier1 = {
        "OLLAMA_URL": config.OLLAMA_URL, "EMBED_URL": config.EMBED_URL,
        "EMBED_MODEL": config.EMBED_MODEL, "CHAT_MODEL": config.CHAT_MODEL,
        "RERANK_URL": config.RERANK_URL or "(disabled)",
        "CHUNK_TOKENS": config.CHUNK_TOKENS, "CHUNK_OVERLAP": config.CHUNK_OVERLAP,
        "CHUNK_VERSION": config.CHUNK_VERSION, "FTS_VERSION": config.FTS_VERSION,
        "EMBED_VERSION": config.EMBED_VERSION, "CLEANING_VERSION": config.CLEANING_VERSION,
        "ENTITY_VERSION": config.ENTITY_VERSION, "FETCH_VERSION": config.FETCH_VERSION,
        "DISCOVER_INTERVAL_MIN": config.DISCOVER_INTERVAL_MIN,
        "ADMIN_TOKEN": "(set)" if config.ADMIN_TOKEN else "(unset — admin routes locked)",
    }
    tier2 = [{
        "collection": c.name, "plugin": c.plugin, "profile": c.profile,
        "embed_mode": c.embed_mode_default, "acquire_enabled": c.acquire_enabled,
        "license_ack": "(recorded)" if c.license_ack else "(none)",
    } for c in collections_mod.load_all()]

    # platform.toml is the OTHER Tier-2 file (architecture-v2.md §14). Shown
    # separately from collections/*.toml because it is node-level, not
    # per-collection.
    pc = platform_config.load()
    def _rate(value):
        return f"{value / 1_000_000:.1f} MB/s" if value else "unlimited"
    tier2_platform = {
        "source": str(pc.source_path) if pc.source_path else "(no platform.toml — defaults)",
        "bandwidth.down_max": _rate(pc.bandwidth.down_max_bps),
        "bandwidth.up_max": _rate(pc.bandwidth.up_max_bps),
        "bandwidth.reserve_interactive": f"{pc.bandwidth.reserve_interactive:.0%}",
        "bandwidth.schedule": pc.bandwidth.schedule_text or "always full",
        "transfer.workers": pc.transfer.workers,
        "transfer.per_endpoint": pc.transfer.per_endpoint,
        "transfer.segments_per_file": pc.transfer.segments_per_file,
    }

    con = db.connect()
    override = db.get_bandwidth_override(con)
    tier3 = {"worker_state (control table)": db.worker_state(con),
             "scope (control table)": db.get_scope(con),
             "bandwidth override (control table, expires)":
                 _rate(override) if override else "(none)"}
    return {"tier1": tier1, "tier2": tier2, "tier2_platform": tier2_platform, "tier3": tier3}


def _ping(url: str) -> bool:
    """Split out from _health_pings() so tests can stub network access
    without touching httpx.Client globally (which would also break
    TestClient itself, since it's httpx-based)."""
    try:
        with httpx.Client(timeout=1.5) as client:
            return client.get(url).status_code < 500
    except Exception:
        return False


def _health_pings() -> list[dict]:
    endpoints: dict[str, list[str]] = {}
    endpoints.setdefault(config.OLLAMA_URL, []).append("OLLAMA_URL")
    endpoints.setdefault(config.EMBED_URL, []).append("EMBED_URL")
    if config.RERANK_URL:
        endpoints.setdefault(config.RERANK_URL, []).append("RERANK_URL")
    return [{"names": names, "url": url, "ok": _ping(url)} for url, names in endpoints.items()]


def _recent_events(con, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


@admin_router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    collections = collections_mod.load_all()
    con = db.connect()
    return TEMPLATES.TemplateResponse(request, "system.html", {
        "active": "system",
        "effective": _effective_config(),
        "boot_report": spaces.report_boot_status(collections),
        "health": _health_pings(),
        "worker_state": db.worker_state(con),
    })


@admin_router.get("/system/log", response_class=HTMLResponse)
def system_log_fragment(request: Request):
    con = db.connect()
    return TEMPLATES.TemplateResponse(request, "_fragments/log_rows.html",
                                       {"events": _recent_events(con)})
