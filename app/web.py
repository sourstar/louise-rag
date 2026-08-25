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
import re
import sqlite3
import secrets
import shutil
import tarfile
import tempfile
import threading
import time
import traceback
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import (auth, collections as collections_mod, config, db, discovery, llm,
                platform_config, query_service, spaces)
from .transfer import manager as transfer_manager
from .transfer import schedule as transfer_schedule
from .acquire import claim
from .acquire import engine as acquire_engine
from .acquire import spec as acquire_spec
from .acquire.spec import DiskPreflightError, LicenseAckRequiredError
from .acquire.specs import ACQUIRE_REGISTRY
from .verify import backend_gate
from . import stats as stats_mod

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Dot-grouped digits ("4.020.578"): a comma reads as a decimal separator to
# half the world — an operator read "+377,217/h" as 377.217 docs/h and
# computed an 11-year ETA from a 4-day one. Dots as thousands separators,
# everywhere a count is shown.
TEMPLATES.env.filters["num"] = (
    lambda n: "" if n is None else f"{int(n):,}".replace(",", "."))


def _human_eta(seconds: float | None) -> str | None:
    """Compact ETA for the Library bars — None when it cannot be known
    (no rate, nothing left), so the card shows NOTHING rather than a
    placeholder. Deliberately coarse: a rolling-sample ETA is accurate to
    a factor, not a minute."""
    if not seconds or seconds <= 0:
        return None
    if seconds < 90:
        return f"~{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"~{int(minutes)}min"
    hours = minutes / 60
    if hours < 48:
        return f"~{hours:.0f}h"
    days = hours / 24
    if days < 400:
        return f"~{days:.0f}d"
    return f"~{days / 365:.0f}y"

ask_router = APIRouter()
admin_router = APIRouter(prefix="/admin", dependencies=[Depends(auth.require_admin)])


# UI display names for plugins whose internal id is not the name operators
# know: "embedded" IS the wikivec pre-embedded corpus on every known
# deployment. The internal id stays stable (TOMLs, registries, tests);
# every human-facing surface shows the label.
PLUGIN_LABELS = {"embedded": "wikivec"}


def _plugin_label(plugin: str) -> str:
    return PLUGIN_LABELS.get(plugin, plugin)


def _sample_write(con, key: str, payload: str) -> None:
    """Telemetry-sample writes from PAGE-RENDER code (stage_rate,
    embed_avail_rate, last_embed_rate): never worth waiting for. The
    manifest's writers are the worker's — db.connect sets a 60 s busy
    timeout sized for THEM — and a page that sits in that timeout, or
    500s when it expires, turns a read-only view into a hostage of the
    pipeline (observed live: Library and Stats "look busy" during a
    gutenberg fetch pass beside pubmed fts). Try briefly, skip on
    contention: the next page load samples again, and a skipped sample
    only delays a rate display, never corrupts one."""
    try:
        con.execute("PRAGMA busy_timeout=200")
        db.set_control(con, key, payload)
    except sqlite3.OperationalError:
        pass
    finally:
        try:
            con.execute("PRAGMA busy_timeout=60000")
        except sqlite3.OperationalError:
            pass


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


# "show results in" — the CONTENT languages the Ask page offers (the page
# chrome never changes language). The pre-embedded corpus's eleven plus the
# common European ones; the model translates into any ISO code, so an
# operator can type another into the TOML later without a code change.
RESULT_LANGUAGES = (
    ("en", "English"), ("de", "Deutsch"), ("fr", "Français"), ("it", "Italiano"),
    ("es", "Español"), ("pt", "Português"), ("nl", "Nederlands"), ("pl", "Polski"),
    ("ru", "Русский"), ("ja", "日本語"), ("zh", "中文"), ("tr", "Türkçe"), ("fa", "فارسی"),
)


@ask_router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "ask.html", {
        "active": "ask", "collections": collections_mod.load_all(), "result": None,
        "result_languages": RESULT_LANGUAGES})


class TranslateBody(BaseModel):
    lang: str
    items: list[dict]          # [{"id": ..., "text": ...}]


# (chunk_id, lang) -> translation. Toggling the drop-down back and forth
# must not pay the model twice; bounded so a long session cannot grow it.
_translation_cache: dict = {}
_TRANSLATION_CACHE_MAX = 4000


@ask_router.post("/ask/translate")
def ask_translate(body: TranslateBody):
    """Translate result CONTENT (snippets, drawer text) into `lang` — never
    titles, authors or ids, never the interface. One model call per page
    of results; failure degrades to the originals with `failed` set so the
    page can say so instead of pretending."""
    lang = body.lang.strip().lower()
    if not lang or lang == "original":
        return {"lang": lang, "items": {}, "failed": False}
    out: dict[str, str] = {}
    pending: list[tuple[str, str]] = []
    for item in body.items:
        key = (str(item.get("id", "")), lang)
        text = str(item.get("text", ""))
        if not text.strip():
            continue
        hit = _translation_cache.get(key)
        if hit is not None:
            out[key[0]] = hit
        else:
            pending.append((key[0], text))
    failed = False
    if pending:
        translated = llm.translate_texts([t for _, t in pending], lang)
        if len(translated) != len(pending):
            failed = True
        else:
            for (cid, _), value in zip(pending, translated):
                out[cid] = value
                if len(_translation_cache) >= _TRANSLATION_CACHE_MAX:
                    _translation_cache.clear()
                _translation_cache[(cid, lang)] = value
    return {"lang": lang, "items": out, "failed": failed}


@ask_router.get("/read/{collection}/{book_id}", response_class=HTMLResponse)
def read_document(request: Request, collection: str, book_id: int,
                  hl: str = "", fragment: int = 0):
    """The document behind a hit — whole text from the index stores, the
    highlighted chunk(s) `hl` (comma-separated chunk ids) marked. Open
    like Ask (reader-facing). `fragment=1` returns the inner HTML for the
    in-page reader panel; without it, a standalone page."""
    from . import reader
    c = reader.collection_by_name(collection)
    if c is None:
        raise HTTPException(404, f"no such collection: {collection}")
    doc = reader.document(c, book_id)
    if doc is None:
        raise HTTPException(404, f"{collection} does not hold #{book_id}")
    highlights = [h for h in hl.split(",") if h]
    return TEMPLATES.TemplateResponse(request, "reader.html", {
        "active": "ask", "collection": c.name, "book_id": book_id,
        "doc": doc, "highlights": highlights, "fragment": bool(fragment),
        "plugin_label": _plugin_label(c.plugin)})


@ask_router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    """Platform + UI documentation. Open like Ask: help must be readable
    BEFORE login (it explains how login works)."""
    return TEMPLATES.TemplateResponse(request, "help.html", {"active": "help"})


@ask_router.post("/ask/query", response_class=HTMLResponse)
def ask_query(request: Request, body: AskQueryBody):
    result = query_service.run_search(body.collections, body.query, 12, body.mode, True)
    # every hit names its COLLECTION for the reader panel: federated hits
    # carry it; a single-collection hit is resolved from its source prefix
    # (the plugin id) to the first collection of that plugin
    by_plugin = {}
    for c in collections_mod.load_all():
        by_plugin.setdefault(c.plugin, c.name)
    for h in result.get("hits", []):
        if not h.get("source_collection"):
            h["source_collection"] = by_plugin.get(str(h.get("chunk_id", "")).split(":", 1)[0])
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

def _human_bytes(n: int | None) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.0f} PB"


def _profile_order(plugin: str) -> list[dict]:
    """A plugin's profiles ordered by estimated size — the data-driven
    ordering behind 'profiles are incremental'. Profiles without an estimate
    keep their spec-declared position at the end."""
    spec = ACQUIRE_REGISTRY.get(plugin)
    if spec is None:
        return []
    names = list(spec.profiles)
    names.sort(key=lambda n: (spec.estimated_bytes.get(n) is None,
                               spec.estimated_bytes.get(n, 0)))
    out = []
    for n in names:
        # the gloss goes between name and size: "hg38 — human (GRCh38)
        # (~938 MB)". Sources whose profile names are already words
        # declare none and read exactly as before.
        gloss = spec.profile_labels.get(n)
        label = f"{n} — {gloss}" if gloss else n
        out.append({"name": n,
                    "label": f"{label} (~{_human_bytes(spec.estimated_bytes.get(n))})"})
    return out


def _offered_by_torrent_collection(collection: collections_mod.Collection) -> list[dict] | None:
    """The slices a peer-sourced collection's TORRENT COLLECTION offers
    (STATUS 5l), as grow options — or None when the collection fetches
    from the origin or from plain torrents. Reads the resolved index from
    the control table only; never the network on a page render."""
    from .acquire import torrent_collection
    sources = acquire_engine.peer_sources(collection)
    if not sources:
        return None
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    offered: list[dict] = []
    seen: set[str] = set()
    found = False
    for source in sources:
        index = torrent_collection.cached_index(collection, source)
        if index is None:
            continue
        found = True
        for s in torrent_collection.offered_slices(index):
            if s["name"] in seen:
                continue
            seen.add(s["name"])
            gloss = spec.profile_labels.get(s["name"]) if spec else None
            label = f"{s['name']} — {gloss}" if gloss else s["name"]
            offered.append({"name": s["name"],
                            "label": f"{label} (~{_human_bytes(s['bytes'])}, {s['files']} files)"})
    return offered if found else None


def _grow_options(collection: collections_mod.Collection) -> list[dict]:
    """What "grow" may offer, which depends on how the source's profiles
    relate (spec.profile_mode):

    incremental — the current profile and larger, never smaller:
                  acquisition only adds files, so rolling back would shrink
                  nothing and only misdescribe the corpus. The current one
                  stays offered as a re-sync/top-up.
    exclusive   — nothing: growing would drop a SECOND corpus into the
                  collection (genome, where two assemblies collide on
                  document ids). A new assembly is a new collection.
    additive    — every profile, in size order, as an independent choice:
                  the caller combines them ("tr,it,en"), so a language
                  already held stays checked and a new one is added rather
                  than replacing it.
    """
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        return []
    offered = _offered_by_torrent_collection(collection)
    if offered is not None:
        # a collection fetched from a TORRENT COLLECTION (STATUS 5l) can
        # only grow into what its publisher offers — those slices, with the
        # publisher's sizes, are the options; nothing the origin might have
        return offered
    if spec.profile_mode == "exclusive":
        return []
    if spec.profile_mode == "additive":
        return _profile_order(collection.plugin)
    order = _profile_order(collection.plugin)
    current = collection.profile
    names = [o["name"] for o in order]
    if current in names:
        order = order[names.index(current):]
        order[0] = {**order[0], "label": order[0]["label"] + " — current, re-sync"}
    return order


# ---------------- document counts: one scan, shared and cached ----------------
#
# Every number on the Library card and the Stats page is a SUM over the whole
# `documents` table. No index can answer that — the predicates span error,
# retries, chunks and embed_version — so each one is a full scan, and it is
# not close to free: measured on this node, pubmed (24.4M rows) takes 12.0s
# and wikipedia (19.4M rows) 10.5s. Seven collections made the Library page a
# 24-second wait, and the Stats page scanned the same two tables all over
# again for 15 more.
#
# Two fixes, both here. The queries MERGE: one aggregate now serves both
# pages, so opening Stats after Library costs nothing rather than repeating
# the work. And the result is cached stale-while-revalidate: a page is served
# from cache instantly and a refresh runs behind it when the entry is older
# than the TTL. Nobody waits except the very first viewer after a restart,
# and a collection nobody looks at is never scanned at all — which is why
# this is a cache and not a background job on a timer.
#
# Staleness is shown, never hidden: these counts describe a backfill that
# moves over days, so a two-minute-old number is not misleading, but a
# number of unknown age would be.

_COUNTS_TTL_S = 120.0

_counts_cache: dict[str, tuple[float, dict]] = {}
_counts_lock = threading.Lock()
# Stage-rate samples (see _collection_shelf): memory-first, keyed on the
# manifest path. GIL-atomic dict ops; a rare lost update costs one sample.
_stage_rate_memo: dict[str, dict] = {}
# in-flight refresh per collection, WITH its start time: a refresh thread
# that hangs (or dies un-noticed) must not hold the guard forever, or the
# counters freeze at an ever-growing age with no refresh ever starting
_counts_refreshing: dict[str, float] = {}
_COUNTS_REFRESH_ABANDON_S = 600.0
# a cache entry that cannot be refreshed (every attempt failing) is dropped
# once it is this stale, so the next viewer pays the cold scan and sees the
# truth instead of a frozen number of unbounded age
_COUNTS_MAX_AGE_S = 1800.0
# how long a COLD page view waits for the first scan before rendering
# "counting…" instead — small manifests answer well inside this
_COUNTS_COLD_WAIT_S = 2.0


def _document_counts_query(collection) -> dict:
    """THE scan. One pass for every count either page needs."""
    con = db.connect(collection)
    # blocked vs unindexable: stage failures are prefixed with their stage
    # (worker._execute_step), so an UNPREFIXED error came from discovery and
    # describes the CORPUS — no text in the mirror, retracted upstream.
    # "Clear errors & retry" cannot fix those (the next scan re-applies
    # them), so reporting them as failures promised an impossible remedy.
    stage_error = ("(" + " OR ".join(
        f"error LIKE '{st}: %'" for st in ("rdf", "fts", "embed")) + ")")
    pending = (f"in_scope=1 AND fts_done=1 AND error IS NULL AND retries<:max "
               f"AND (embed_done=0 OR embed_version<:ver)")
    row = con.execute(f"""
        SELECT COUNT(*) total, SUM(in_scope) in_scope,
          SUM(CASE WHEN in_scope=1 AND rdf_done=1 THEN 1 ELSE 0 END) rdf_done,
          SUM(CASE WHEN in_scope=1 AND fts_done=1 THEN 1 ELSE 0 END) fts_done,
          SUM(CASE WHEN in_scope=1 AND embed_done=1 THEN 1 ELSE 0 END) embed_done,
          SUM(CASE WHEN in_scope=1 AND ({stage_error} OR retries>=:max)
                   THEN 1 ELSE 0 END) blocked,
          SUM(CASE WHEN in_scope=1 AND error IS NOT NULL
                        AND NOT {stage_error} AND retries<:max
                   THEN 1 ELSE 0 END) unindexable,
          SUM(CASE WHEN {pending} THEN 1 ELSE 0 END) embed_ready,
          SUM(CASE WHEN {pending} THEN chunks ELSE 0 END) chunks_pending,
          -- Documents that still have to pass rdf/fts before they can be
          -- embedded. `chunks` is written BY the fts stage, so for these the
          -- chunk count is not zero — it is unknown, and a page that renders
          -- it as 0 says "nothing to do" about a collection with thousands of
          -- contigs still to index.
          SUM(CASE WHEN in_scope=1 AND fts_done=0 AND error IS NULL
                        AND retries<:max THEN 1 ELSE 0 END) docs_unindexed
        FROM documents""",
        {"max": config.MAX_RETRIES, "ver": config.EMBED_VERSION}).fetchone()
    return {k: (row[k] or 0) for k in row.keys()}


def _counts_key(collection) -> str:
    """Keyed on the physical manifest path, not collection.name — the same
    lesson fts.get_index() records: two Collection instances can share a
    name while pointing at different files, and a name-keyed cache then
    serves one collection's numbers for another."""
    return str(collection.manifest_path)


def _document_counts(collection) -> tuple[dict | None, float]:
    """(counts, age_seconds). Serves a cached value immediately and refreshes
    behind it. A COLD cache no longer blocks for long: seven collections at
    ~25 s of full scan each made the first Library view after a restart a
    minute-plus hang — indistinguishable from a stuck page, on the page
    whose job is to show that things move. Cold starts the refresh and
    waits at most _COUNTS_COLD_WAIT_S for it — a small manifest answers
    within that and behaves as always; a 40M-row one returns (None, 0) and
    the card renders an honest "counting…" until the scan lands."""
    name = _counts_key(collection)
    now = time.time()
    with _counts_lock:
        entry = _counts_cache.get(name)
    if entry is not None:
        at, counts = entry
        if now - at > _COUNTS_TTL_S:
            _refresh_counts_async(collection)
        return counts, now - at
    _refresh_counts_async(collection)
    deadline = time.time() + _COUNTS_COLD_WAIT_S
    while time.time() < deadline:
        with _counts_lock:
            entry = _counts_cache.get(name)
        if entry is not None:
            return entry[1], 0.0
        time.sleep(0.05)
    return None, 0.0


def _refresh_counts_async(collection) -> None:
    """At most one refresh in flight per collection — without the guard, a
    page that is slow to load invites a reload, and every reload would start
    another 12-second scan of the same table. The guard expires
    (_COUNTS_REFRESH_ABANDON_S): a refresh thread that hangs must not stop
    every future refresh, which would freeze the counters for good."""
    name = _counts_key(collection)
    now = time.time()
    with _counts_lock:
        started = _counts_refreshing.get(name)
        if started is not None and now - started < _COUNTS_REFRESH_ABANDON_S:
            return
        _counts_refreshing[name] = now

    def _run():
        try:
            counts = _document_counts_query(collection)
            with _counts_lock:
                _counts_cache[name] = (time.time(), counts)
        except Exception:   # noqa: BLE001 — a failed refresh keeps the old value
            # ...but never silently: a refresh that fails EVERY time froze
            # the Library's counters at an ever-growing age with nothing in
            # any log to say why. stdout reaches `docker logs`; and once the
            # entry is too stale to be worth anything, drop it so the next
            # viewer pays the cold scan and sees the truth.
            print(f"counts refresh failed for {name}:\n"
                  f"{traceback.format_exc()[:1200]}", flush=True)
            with _counts_lock:
                entry = _counts_cache.get(name)
                if entry is not None and time.time() - entry[0] > _COUNTS_MAX_AGE_S:
                    del _counts_cache[name]
        finally:
            with _counts_lock:
                _counts_refreshing.pop(name, None)

    threading.Thread(target=_run, name=f"counts:{name}", daemon=True).start()


def invalidate_counts(collection=None) -> None:
    """Drop cached counts after an action that changes them (reset, clear
    errors, rescan), so the operator sees the effect of what they just did
    instead of a stale number. No argument clears everything."""
    with _counts_lock:
        if collection is None:
            _counts_cache.clear()
        else:
            _counts_cache.pop(_counts_key(collection), None)


def _worker_stats() -> dict:
    """The worker's published per-collection snapshot, or {} when it is
    silent or stale. Read once per page: it is one control-table row."""
    try:
        con = db.read_only_connect()
        if con is None:
            return {}
        try:
            payload, _ = db.read_worker_stats(con)
            return payload or {}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _collection_shelf(collection: collections_mod.Collection,
                       measured: dict | None = None) -> dict:
    con = db.connect(collection)
    # Counts come from the shared cache (see _document_counts): one scan per
    # collection, reused by the Stats page, refreshed behind the request
    # rather than in front of it.
    counts, counts_age = _document_counts(collection)
    counts_pending = counts is None
    if counts_pending:
        counts = {k: 0 for k in ("total", "in_scope", "rdf_done", "fts_done",
                                 "embed_done", "blocked", "unindexable",
                                 "embed_ready", "chunks_pending", "docs_unindexed")}
    # How many of the "awaiting" documents are in fact inside the encoder
    # right now. A document commits only when all of its chunks are done, so
    # on a slow CPU encoder several sit in flight for many minutes — and
    # counting them as waiting reads as "nothing is happening" while the
    # encoder runs at 300% CPU.
    in_flight = int((measured or {}).get(collection.name, {}).get("in_flight", 0) or 0)
    queue = con.execute("SELECT COUNT(*) c FROM embed_queue").fetchone()["c"]
    blocked = counts["blocked"] or 0
    unindexable = counts["unindexable"] or 0
    embed_ready = counts["embed_ready"] or 0
    space_row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    last_discover = con.execute(
        "SELECT ts FROM events WHERE stage='discover' ORDER BY id DESC LIMIT 1").fetchone()

    fetched = None
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if collection.acquire_enabled and spec is not None:
        # extra=0: opted-in EXTRA formats (spec.extras — local-only, never
        # p2p) are counted apart, never inflating the Louise numbers.
        fc = con.execute(
            "SELECT COUNT(*) total, SUM(fetch_done) done FROM fetch_files "
            "WHERE source_id=? AND extra=0",
            (spec.source_id,)).fetchone()
        xc = con.execute(
            "SELECT COUNT(*) total, SUM(fetch_done) done FROM fetch_files "
            "WHERE source_id=? AND extra=1",
            (spec.source_id,)).fetchone()
        # A fetch pass streams the remote listing lazily — claim a file,
        # download it, move on — so fetch_files GROWS while the pass runs
        # and done==total mid-pass, which read as "complete" while a third
        # of a dataset was still to come (observed live on wikivec:
        # "1414/1414" with 33 files unlisted). The acquisition claim
        # (refreshed every 60 s by the running pass) says whether one is
        # active, so the card can say "in progress" instead.
        acquire_claim = claim.read_claim(con)
        active = bool(acquire_claim
                      and time.time() - float(acquire_claim.get("ts", 0))
                      <= claim.STALE_AFTER_SECONDS)
        # the SOURCE's own file count, recorded by the engine at pass start
        # (acquire_engine.LISTING_KEY): the honest denominator while the
        # pass streams — without it, done==total at every instant mid-pass
        remote_total = 0
        docs_total = None
        extra_remote_total = 0
        raw_listing = db.get_control(con, acquire_engine.LISTING_KEY)
        if raw_listing:
            try:
                listing = json.loads(raw_listing)
                if listing.get("profile") == (collection.profile or listing.get("profile")):
                    remote_total = int(listing.get("files_total") or 0)
                    docs_total = listing.get("docs_total")
                    extra_remote_total = int(listing.get("extra_files_total") or 0)
            except (ValueError, TypeError):
                pass
        # fetched in the same UNIT parsed/indexed use, where the layout
        # allows it: one dir = one document, so distinct fetched top dirs =
        # documents on disk. Container sources instead say so on the card.
        docs_done = None
        if spec.layout == "one-dir-per-doc":
            docs_done = con.execute(
                "SELECT COUNT(DISTINCT substr(rel_path, 1, instr(rel_path, '/') - 1)) "
                "FROM fetch_files WHERE source_id=? AND fetch_done=1 AND extra=0 "
                "AND instr(rel_path, '/') > 0", (spec.source_id,)).fetchone()[0]
            # SEEDED from discovery: what is already on disk IS fetched in
            # the operator's sense, and discovery counted it at startup —
            # the fetch ledger only catches up as the first full pass
            # ADOPTS (hash-verifies) those files. Without the seed the
            # counter started at 0 beside a 45k-book corpus. Documents the
            # corpus holds without usable text are excluded: an empty or
            # text-less dir is not a fetched document.
            if not counts_pending:
                on_disk = max(0, (counts["total"] or 0) - (counts["unindexable"] or 0))
                docs_done = max(docs_done, on_disk)
        # extras (spec.extras): "+ e/E" beside the Louise counter, shown
        # only when the collection opted in or extra rows exist. Format
        # explained in Help, never inline — the GUI stays clean.
        has_extras = bool(getattr(collection, "extras", ())) or (xc["total"] or 0) > 0
        louise_total = max(remote_total, fc["total"] or 0)
        extra_total = max(extra_remote_total, xc["total"] or 0)
        # The label's third state: no pass running AND every listed file
        # (Louise and, when opted in, extras) has landed. `complete` is
        # judged against the honest denominators — the origin's recorded
        # listing — never against the streaming ledger alone, and never
        # while a pass is running (mid-pass done==total at every instant).
        complete = (not active and louise_total > 0
                    and (fc["done"] or 0) >= louise_total
                    and (xc["done"] or 0) >= extra_total)
        fetched = {"done": fc["done"] or 0, "total": fc["total"] or 0,
                   "remote_total": louise_total,
                   "docs_done": docs_done, "docs_total": docs_total,
                   "containers": spec.layout != "one-dir-per-doc",
                   "active": active, "complete": complete,
                   "has_extras": has_extras,
                   "extra_done": xc["done"] or 0,
                   "extra_total": extra_total}

    if last_discover:
        freshness = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(last_discover["ts"]))
    else:
        freshness = "never scanned"

    # PROOF OF MOTION and synthetic ETAs, one mechanism for EVERY bar:
    # each stage's advance is measured across page loads (persisted per
    # collection) and turned into a compact ETA shown after the counter. A
    # 40M-document collection advancing at a healthy rate moves its bar
    # fractions of a percent per hour, which reads as STUCK (operator
    # report, verified live against the events table). A rate that
    # measures zero over the window yields NOTHING — no placeholder, and
    # never a stale number beside a genuinely stalled stage.
    # Samples live in PROCESS MEMORY first (all page loads share the web
    # process); the control-table write is best-effort persistence for the
    # next restart only. Memory is not optional: on a manifest under
    # continuous pipeline writes the 200 ms _sample_write loses the lock
    # race nearly every time (observed live on pubmed after the throughput
    # fix — the baseline could not land for an hour), and a sampler that
    # needs a WRITE to function turns a reader into a lock contender.
    _RATE_FIELDS = ("fetch", "rdf", "fts", "embed")
    stage_rates = {k: None for k in _RATE_FIELDS}
    stage_etas = {k: None for k in _RATE_FIELDS}
    if not counts_pending:
        now_i = int(time.time())
        cur = {
            "rdf": counts["rdf_done"] or 0,
            "fts": counts["fts_done"] or 0,
            "embed": counts["embed_done"] or 0,
            # documents where the layout counts them, files otherwise —
            # the same unit the fetched bar displays
            "fetch": ((fetched.get("docs_done") if fetched
                       and fetched.get("docs_done") is not None
                       else (fetched or {}).get("done")) or 0),
        }
        remaining = {
            "rdf": max(0, (counts["in_scope"] or 0) - cur["rdf"]),
            "fts": max(0, (counts["in_scope"] or 0) - cur["fts"]),
            "embed": max(0, (counts["in_scope"] or 0) - cur["embed"]),
            "fetch": (max(0, ((fetched.get("docs_total")
                               if fetched.get("docs_total") is not None
                               else fetched.get("remote_total")) or 0)
                          - cur["fetch"]) if fetched else 0),
        }
        memo_key = str(collection.manifest_path)
        rec_sr = _stage_rate_memo.get(memo_key)
        if rec_sr is None:
            # warm start after a web restart: the last persisted sample
            raw_sr = db.get_control(con, "stage_rate")
            if raw_sr:
                try:
                    rec_sr = json.loads(raw_sr)
                except (ValueError, TypeError):
                    rec_sr = None
        if rec_sr is None or any(k not in rec_sr for k in _RATE_FIELDS) \
                or any(cur[k] < int(rec_sr.get(k) or 0) for k in _RATE_FIELDS):
            # first sight, a pre-upgrade record, or a reset shrank a
            # count: fresh baseline
            baseline = {**cur, "ts": now_i}
            _stage_rate_memo[memo_key] = baseline
            _sample_write(con, "stage_rate", json.dumps(baseline))
            for k in _RATE_FIELDS:
                if remaining[k] > 0:
                    stage_etas[k] = "TBD"        # work remains, no history yet
        else:
            # LOW-PASS FILTERED rate: an exponential moving average over the
            # per-window rates (alpha 0.3 -> a new window carries 30 %), and
            # an ETA is shown only once TWO windows have contributed.
            # Operator report: raw window-to-window rates swung the ETA from
            # wildly optimistic (a 1000-document batch landing just inside
            # the window) to wildly pessimistic (the window catching the
            # quiet tail of a batch). A zero-progress window drops the rate
            # entirely — TBD until motion resumes — rather than decaying a
            # stale number into an ever-growing ETA.
            _EMA_ALPHA = 0.3
            _MIN_WINDOWS = 2
            # A stage that commits in BURSTS (a 1,000-chunk book every six
            # minutes) shows no progress in many 5-minute windows. Such a
            # window is not evidence of a zero rate: the measurement
            # window STRETCHES until the stage moves again — the rate is
            # progress over the time since the LAST progress — and only a
            # stall longer than _STALL_S drops the rate to TBD.
            _STALL_S = 1800
            samples = {k: int(rec_sr.get(k + "_n") or 0) for k in _RATE_FIELDS}
            for k in _RATE_FIELDS:
                stage_rates[k] = rec_sr.get(k + "_ph")
            elapsed = now_i - int(rec_sr.get("ts") or now_i)
            if elapsed >= 300:
                progress_ts, progress_count = {}, {}
                for k in _RATE_FIELDS:
                    # since the last PROGRESS-bearing sample (older records:
                    # the last sample)
                    p_ts = int(rec_sr.get(k + "_pts") or rec_sr.get("ts") or now_i)
                    p_count = int(rec_sr.get(k + "_pc") if rec_sr.get(k + "_pc") is not None
                                  else (rec_sr.get(k) or 0))
                    delta = cur[k] - p_count
                    span = max(1, now_i - p_ts)
                    prev = rec_sr.get(k + "_ph")
                    if delta > 0:
                        raw = delta * 3600.0 / span
                        ema = raw if not prev else _EMA_ALPHA * raw + (1 - _EMA_ALPHA) * prev
                        stage_rates[k] = round(ema)
                        samples[k] += 1
                        p_ts, p_count = now_i, cur[k]
                    elif span >= _STALL_S:
                        stage_rates[k] = None       # stalled: no number to stand behind
                        samples[k] = 0
                    progress_ts[k], progress_count[k] = p_ts, p_count
                updated = {**cur, "ts": now_i,
                           **{k + "_ph": stage_rates[k] for k in _RATE_FIELDS},
                           **{k + "_n": samples[k] for k in _RATE_FIELDS},
                           **{k + "_pts": progress_ts[k] for k in _RATE_FIELDS},
                           **{k + "_pc": progress_count[k] for k in _RATE_FIELDS}}
                _stage_rate_memo[memo_key] = updated
                _sample_write(con, "stage_rate", json.dumps(updated))
            for k in _RATE_FIELDS:
                if remaining[k] <= 0:
                    continue
                if stage_rates[k] and samples[k] >= _MIN_WINDOWS:
                    stage_etas[k] = _human_eta(remaining[k] * 3600.0 / stage_rates[k])
                else:
                    # work remains and the rate is not yet trustworthy:
                    # say so, never a number that will be revised 10x
                    stage_etas[k] = "TBD"

    return {
        "embed_mode": db.get_scope(con)["embed_mode"],
        "name": collection.name, "plugin": collection.plugin,
        "plugin_label": _plugin_label(collection.plugin),
        "stage_rates": stage_rates,
        "stage_etas": stage_etas,
        "profile": collection.profile or "(hand-populated)",
        # the FULL id, never truncated: it names an embedding space uniquely,
        # and a prefix invites comparing prefixes — two spaces can share ten
        # hex chars and a truncated display would call them equal
        "space_id": (space_row["space_id"] if space_row and space_row["space_id"]
                      else "unpinned"),
        "fetched": fetched,
        "total": counts["total"] or 0, "in_scope": counts["in_scope"] or 0,
        "rdf_done": counts["rdf_done"] or 0, "fts_done": counts["fts_done"] or 0,
        "embed_done": counts["embed_done"] or 0,
        "queue": queue, "last_discover_text": freshness,
        # how old the cached counts are, so a stale progress bar is
        # visibly stale rather than quietly wrong; counts_pending = the
        # first scan after a restart is still running (cold cache)
        "counts_age": counts_age,
        "counts_pending": counts_pending,
        # a torrent-collection peer combines slices like an additive source
        "profile_mode": ("additive" if _offered_by_torrent_collection(collection) is not None
                         else (spec.profile_mode if spec else "incremental")),
        "profile_parts": acquire_spec.profile_parts(collection.profile or ""),
        "blocked": blocked, "unindexable": unindexable,
        "embed_ready": embed_ready,
        "embed_in_flight": min(in_flight, embed_ready),
        "embed_waiting": max(0, embed_ready - in_flight),
        "gate": backend_gate.gate_state(con),
        # which processor this collection's encoder spends — the GPU is the
        # scarce one, so "turn embedding on" means something different here
        "embed_device": collections_mod.recipe_for(collection).spends,
        "grow_options": _grow_options(collection),
        # EXTRA formats: what the source offers, what this collection holds
        "extras_offered": sorted(spec.extras) if spec is not None else [],
        "extras_held": list(getattr(collection, "extras", ())),
        "license": _license_state(collection),
        "genesis": _genesis_state(con, collection,
                                  None if counts_pending else counts,
                                  fetched=fetched),
    }


def _genesis_state(con, collection, counts=None, fetched=None) -> dict | None:
    """Provenance visibility for pre-embedded data: is a genesis archive
    declared, how much of the collection did it cover — and, for a REMOTE
    archive, whether it is still WAITING. Remote shards apply once,
    post-settle (drain_next_shard's backlog gate), so pubmed's card read
    "0/37 shards ... the worker drains the rest automatically" for the
    ~8 weeks its 33M documents take to parse and index, promising activity
    the gate deliberately withholds. Say so instead: `waiting_docs` (from
    the cached counts — never a fresh scan here) is how many in-scope
    documents still have to pass fts before the first shard can apply."""
    if not collection.genesis_archive:
        return None
    from .bundles.genesis import ARCHIVES, _local_shards
    archive = ARCHIVES.get(collection.genesis_archive)
    totals = con.execute(
        "SELECT COUNT(*) shards, SUM(state IN ('applied','final')) applied, "
        "SUM(state='final') final, SUM(docs) docs, "
        "SUM(state='rejected') rejected, "
        # amendment 42: the archive is REJECTED only while nothing has proven
        # it; a rejected shard beside a proven one is that shard's problem
        "SUM(proven_at IS NOT NULL OR (state IN ('applied','final') AND COALESCE(docs,0)>0)) proven, "
        # amendment 38: what the fetcher has landed (on disk or already
        # consumed), what still sits on disk, and how many held documents
        # the resident shards are waiting on
        "SUM(state IN ('fetched','applying','applied','final')) fetched, "
        "SUM(state='fetching') fetching, "
        "SUM(CASE WHEN state IN ('fetched','applying','applied') THEN COALESCE(bytes,0) ELSE 0 END) resident_bytes, "
        "SUM(CASE WHEN state='applied' THEN COALESCE(pending,0) ELSE 0 END) pending "
        "FROM genesis_imports WHERE archive=?",
        (collection.genesis_archive,)).fetchone()
    if archive is not None and archive.local:
        shard_total = len(_local_shards(collection))
    else:
        shard_total = len(archive.shards) if archive else 0
    # Computed for BOTH kinds: a remote archive WAITS on it (shards apply
    # once, post-settle); a local one is PACED by it (a shard is read when
    # an indexed document's locator names it, and revisited as more of its
    # documents clear fts) — either way it is the number that explains a
    # shard counter standing still.
    if counts is None:          # cold cache: unknown, but almost surely waiting
        waiting_docs = None
        index_done = index_total = index_pct = None
    else:
        waiting_docs = max(0, (counts["in_scope"] or 0) - (counts["fts_done"] or 0))
        # progress TOWARD the import, for the card to show as a number —
        # "0/37, waits by design" is a fact but not a meter; what actually
        # moves toward the import starting is indexing
        index_done = counts["fts_done"] or 0
        index_total = counts["in_scope"] or 0
        index_pct = (100.0 * index_done / index_total) if index_total else 0.0
    # The badge's color speaks the ARCHIVE DATA's fetch state (operator
    # scheme: green done, orange fetching in progress, grey fetching off).
    # For a local archive the data IS the corpus, so its state is the
    # card's own fetched state; for a remote one it is the shard download:
    # all applied = done, a shard mid-apply = active, otherwise idle.
    applied = totals["applied"] or 0
    if archive is not None and archive.local:
        if fetched and fetched.get("complete"):
            fetch_state = "done"
        elif fetched and fetched.get("active"):
            fetch_state = "active"
        else:
            fetch_state = "idle"
    else:
        # amendment 38: the archive is a second acquisition stream — its
        # fetch meter is shards LANDED (fetched, applying, applied, final)
        # over declared; active while a shard is downloading or applying
        applying = con.execute(
            "SELECT 1 FROM genesis_imports WHERE archive=? AND state='applying' "
            "LIMIT 1", (collection.genesis_archive,)).fetchone()
        if shard_total and (totals["fetched"] or 0) >= shard_total:
            fetch_state = "done"
        elif applying or totals["fetching"]:
            fetch_state = "active"
        else:
            fetch_state = "idle"
    return {
        "archive": collection.genesis_archive,
        "known": archive is not None,
        "local": bool(archive and archive.local),
        "shard_total": shard_total,
        "applied": applied,
        "final": totals["final"] or 0,
        "fetched": totals["fetched"] or 0,
        "resident_bytes": totals["resident_bytes"] or 0,
        "pending": totals["pending"] or 0,
        "docs": totals["docs"] or 0,
        "rejected": bool(totals["rejected"]) and not bool(totals["proven"]),
        "rejected_shards": int(totals["rejected"] or 0),
        "waiting_docs": waiting_docs,
        "fetch_state": fetch_state,
        "index_done": index_done,
        "index_total": index_total,
        "index_pct": index_pct,
        # declared coverage: how many in-scope documents the archive CAN
        # cover is unknown without the ids; what it has covered is `docs`
        "declares_coverage": bool(archive and archive.ids_per_shard),
    }


def _license_state(collection: collections_mod.Collection) -> dict:
    """One resolution for every plugin, packs included (app/licensing.py):
    the class comes from the spec or the pack's manifest, the terms from
    the spec's file or the pack's TERMS.txt, and the ack must match them."""
    from . import licensing
    cls = licensing.license_class_for(collection)
    state = licensing.state_for(collection)
    return {
        "class": None if cls == "unknown" else cls,
        "state": state,
        "ack_recorded": bool(collection.license_ack),
        "acquire_enabled": collection.acquire_enabled,
        # the ack button applies when use is gated: a yellow/red class, no
        # VALID ack (missing, or for other terms), and a TOML to write it into
        "needs_ack": bool(
            cls in ("yellow", "red") and state == licensing.MISSING
            and collections_mod.config_toml_path(collection.name).exists()),
    }


@admin_router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    collections = collections_mod.load_all()
    con = db.connect()   # worker_state is one global control-table toggle (see worker.py)
    measured = _worker_stats()
    shelves = [_collection_shelf(c, measured) for c in collections]
    return TEMPLATES.TemplateResponse(request, "library.html", {
        "active": "library",
        "shelves": shelves,
        # oldest cached count on the page — a stale bar should look stale
        "counts_age": max((s["counts_age"] for s in shelves), default=0),
        "plugins": sorted(collections_mod.PLUGIN_REGISTRY),
        "plugin_profiles": {p: _profile_order(p)
                             for p in collections_mod.PLUGIN_REGISTRY},
        # EXTRA formats each source offers (spec.extras) — local-only
        # downloads, never p2p; the create/grow modals render checkboxes
        "plugin_extras": {p: (sorted(ACQUIRE_REGISTRY[p].extras)
                              if p in ACQUIRE_REGISTRY else [])
                          for p in collections_mod.PLUGIN_REGISTRY},
        "corpus_mounts": _corpus_mounts(),
        "corpus_mounts_dir": str(config.CORPUS_MOUNTS_DIR),
        "worker_state": db.worker_state(con),
    })


class GrowBody(BaseModel):
    profile: str
    # opted-in EXTRA formats (spec.extras): None = keep the collection's
    # current opt-ins; a list = the new selection (may be empty to drop
    # them). Local-only downloads — never part of the p2p exchange.
    extras: list[str] | None = None


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
    """Growing = declaring the new profile (Tier-2 TOML). The WORKER then
    acquires: its hot-reload treats a profile change as re-discover+acquire
    within one round, with the download budgeted, resumable, and in the
    byte-moving process. Running the acquisition inside this web request —
    the previous implementation — blocked the response for the duration of
    a multi-GB download (observed live: the grow modal never closed) and
    moved corpus bytes in the wrong process."""
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    try:
        # accepts a COMBINED profile ("tr,it,en") on an additive source,
        # where growing means adding a language rather than replacing one
        acquire_spec.resolve_profile(spec, body.profile)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if body.extras is None:
        extras = collection.extras
    else:
        unknown = [e for e in body.extras if e not in spec.extras]
        if unknown:
            raise HTTPException(400, f"unknown extra format(s) "
                                      f"{', '.join(unknown)} — this source "
                                      f"offers: {', '.join(sorted(spec.extras)) or 'none'}")
        extras = tuple(body.extras)
    toml_path = collections_mod.config_toml_path(name)
    if not toml_path.exists():
        raise HTTPException(400, f"{name!r} has no TOML file (synthesized "
                                  f"default) — declare it before growing")
    _write_collection_toml(
        toml_path, plugin=collection.plugin, profile=body.profile,
        acquire_enabled=True, corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=collection.license_ack,
        genesis_archive=collection.genesis_archive,
        # Tier-2 TOML is DESIRED STATE (invariant 10): every rewrite
        # materializes the EFFECTIVE recipe, so an env-fallback collection
        # becomes explicit on its first touch. Byte-equal recipe → same
        # space id → the pin never notices. embed_mode carried for the same
        # reason the [embed] block is: a rewrite that drops it would reset
        # the collection to 'lazy' at the next restart.
        embed=collections_mod.recipe_for(collection),
        embed_mode=collection.embed_mode_default,
        snapshot=collection.snapshot, extras=extras,
        peer_source=collection.peer_source)
    con = db.connect(collection)
    db.log_event(con, "info", "control",
                 f"profile -> {body.profile!r} via admin UI; the worker "
                 f"acquires within a round")
    return {"ok": True, "profile": body.profile,
            "note": "Profile declared; the worker starts fetching within one "
                     "round (watch the System log / the fetched bar)."}


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
    # a reset is exactly the moment an operator wants to SEE change; serving
    # them a cached count from before their own click reads as a no-op
    invalidate_counts(collection)
    return {"ok": True}


@admin_router.post("/library/{name}/discover")
def library_discover(name: str):
    """"Rescan" means FULL rescan: the scan cursor is cleared first.

    Every plugin skips containers/book directories whose (mtime, size) is
    unchanged, which is what makes repeat scans cheap — but it cannot see a
    file rewritten in place by something outside the platform. Without this
    the button would re-run the same cheap stat pass and appear to do
    nothing, which is exactly when an operator reaches for it."""
    collection = _collection_or_404(name)
    con = db.connect(collection)
    from .plugins import cursor as scan_cursor
    cleared = scan_cursor.clear(collection.manifest_path.parent, collection.plugin)
    if cleared:
        db.log_event(con, "info", "discover",
                     "scan cursor cleared — this rescan re-examines everything")
    return discovery.sync_collection(con, collection)


class NewCollectionBody(BaseModel):
    name: str
    plugin: str
    profile: str = "demo"
    corpus_dir: str = ""      # a pack folder: its own terms text (packed plugin)


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
    if body.plugin == "packed" and body.corpus_dir:
        # a pack's terms are its own file — the origin's, byte-exact
        from .pack import manifest as pack_manifest
        from .plugins.packed import for_pack
        if pack_manifest.is_pack_dir(body.corpus_dir):
            src = for_pack(body.corpus_dir)
            license_class = src.license_class.value
            if src.terms_path is not None:
                terms_text = src.terms_path.read_text(encoding="utf-8")[:2000]
                ack = acquire_engine.make_ack(src.terms_path)
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


# ---------------- collection management (create / remove / bundle I/O) ----

_COLLECTION_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _write_collection_toml(path: Path, *, plugin: str, profile: str | None,
                            acquire_enabled: bool, corpus_dir: Path,
                            manifest: Path, fts: Path, lancedb: Path,
                            license_ack: str | None = None,
                            genesis_archive: str | None = None,
                            embed=None, embed_mode: str | None = None,
                            snapshot: str | None = None,
                            extras: tuple[str, ...] = (),
                            peer_source: str | None = None) -> None:
    lines = [f'plugin = "{plugin}"']
    if peer_source:
        # peer plane: fetch from this magnet/.torrent instead of the origin
        # (engine.effective_spec). Carried on every rewrite like the rest.
        # Several (one per language of a pre-embedded corpus, STATUS 5h)
        # are written as a TOML array, in fetch order.
        from .acquire.engine import peer_sources as _peer_sources
        sources = _peer_sources(type("_P", (), {"peer_source": peer_source})())
        if len(sources) > 1:
            lines.append("peer_source = [")
            lines.extend(f'  "{s}",' for s in sources)
            lines.append("]")
        else:
            lines.append(f'peer_source = "{sources[0] if sources else peer_source}"')
    if embed_mode:
        lines.append(f'embed_mode = "{embed_mode}"')
    if snapshot:
        # carried like the [embed] block and embed_mode: dropping a pinned
        # snapshot on a rewrite would silently move the collection back to
        # "whatever the origin calls latest"
        lines.append(f'snapshot = "{snapshot}"')
    if profile:
        lines.append(f'profile = "{profile}"')
    if acquire_enabled:
        lines.append("acquire_enabled = true")
    if license_ack:
        lines.append(f'license_ack = "{license_ack}"')
    if genesis_archive:
        lines.append(f'genesis_archive = "{genesis_archive}"')
    if extras:
        # opted-in EXTRA formats (spec.extras) — local-only downloads,
        # never p2p. Carried on every rewrite like embed_mode: dropping it
        # would silently stop the extras at the next profile touch.
        quoted = ", ".join(f'"{e}"' for e in extras)
        lines.append(f"extras = [{quoted}]")
    lines.append(f'corpus_dir = "{corpus_dir.as_posix()}"')
    if embed is not None:
        # any rewrite must carry the [embed] block forward — dropping it
        # would silently flip the collection back to the env recipe, which
        # the space pin would then refuse at the next boot
        lines += ["", "[embed]", f'url = "{embed.url}"',
                  f'api = "{embed.api}"', f'model = "{embed.model}"',
                  f'dtype = "{embed.dtype}"', f"ctx = {embed.ctx}"]
        if embed.dim is not None:
            lines.append(f"dim = {embed.dim}")
        if embed.query_url:
            lines.append(f'query_url = "{embed.query_url}"')
        if getattr(embed, "concurrency", 1) > 1:
            # not space identity, but VERY MUCH desired state: dropping it
            # on a rewrite silently serialized a CPU collection's embeds
            lines.append(f"concurrency = {embed.concurrency}")
        if getattr(embed, "device", None):
            lines.append(f'device = "{embed.device}"')
    lines += [
        "",
        "[paths]",
        f'manifest = "{manifest.as_posix()}"',
        f'fts = "{fts.as_posix()}"',
        f'lancedb = "{lancedb.as_posix()}"',
        "",
    ]
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def _corpus_mounts() -> list[dict]:
    """The candidate corpus folders: whatever the deployment mounted under
    CORPUS_MOUNTS_DIR, each marked with the collection already using it (a
    corpus can belong to at most one collection). Operators pick from real
    mounts — arbitrary container paths are not accepted."""
    used = {c.corpus_dir.as_posix(): c.name for c in collections_mod.load_all()}
    mounts = []
    root = config.CORPUS_MOUNTS_DIR
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.is_dir():
                mounts.append({"path": entry.as_posix(),
                                "used_by": used.get(entry.as_posix()), "kind": "corpus"})
    # PACKS (docs/pack-format.md): a directory holding a manifest is a
    # corpus for the `packed` plugin — offered beside the origin corpora
    from .pack import manifest as pack_manifest
    packs = config.PACK_MOUNTS_DIR
    if packs.is_dir() and packs != root:
        for entry in sorted(packs.iterdir()):
            if entry.is_dir() and pack_manifest.is_pack_dir(entry):
                mounts.append({"path": entry.as_posix(),
                                "used_by": used.get(entry.as_posix()), "kind": "pack"})
    return mounts


class CreateCollectionBody(BaseModel):
    name: str
    plugin: str
    profile: str = "demo"
    acquire_enabled: bool = False
    corpus_dir: str = ""     # a CORPUS_MOUNTS_DIR entry; empty = CORPUS_DIR/<name>
    acknowledge_terms: bool = False   # yellow-class sources: explicit consent
    # Optional pre-embedded COMPANION at install time (wikipedia only): a
    # second collection <name>-vec on the `embedded` plugin, acquiring the
    # published frozen snapshot whose vectors travel with it — dense search
    # works from day one while the live collection's own embeddings grow
    # lazily. Needs its own corpus mount and its own terms acknowledgement.
    companion_corpus_dir: str = ""
    acknowledge_companion_terms: bool = False
    # opted-in EXTRA formats (spec.extras) — local-only downloads beside
    # the Louise data, never part of the p2p exchange
    extras: list[str] = []
    # WHERE the corpus comes from: "source" (the origin, today's default),
    # "peers" (a peer's torrent — peer_source holds the magnet/.torrent),
    # or "off" (hand-populated: no acquisition). acquire_enabled stays for
    # older clients; the dropdown value wins when given.
    acquire_source: str = ""
    peer_source: str = ""


def _resolve_corpus_mount(raw: str, mounts, *, what: str = "collection") -> Path:
    """The chosen corpus folder must be a real, unused mounted dataset —
    shared by the main collection and the companion (each needs its own)."""
    if not raw.strip():
        free = [m["path"] for m in mounts if not m["used_by"]]
        raise HTTPException(
            400, f"choose a corpus folder for the {what}: it must point at a "
                 f"real mounted dataset. Free folders under "
                 f"{config.CORPUS_MOUNTS_DIR}: {', '.join(free) or '(none — map one first)'}. "
                 f"Map a dataset by adding '- /mnt/<pool>/<dataset>:"
                 f"{config.CORPUS_MOUNTS_DIR}/<name>' to BOTH services in the "
                 f"app YAML, then re-open this page")
    chosen = next((m for m in mounts if m["path"] == raw.strip()), None)
    if chosen is None:
        available = ", ".join(m["path"] for m in mounts) or "(none)"
        raise HTTPException(
            400, f"the {what}'s corpus folder must be one of the mounted "
                 f"entries under {config.CORPUS_MOUNTS_DIR} — available: "
                 f"{available}. Map your dataset there first (TrueNAS: add "
                 f"'- /mnt/<pool>/<dataset>:{config.CORPUS_MOUNTS_DIR}/"
                 f"<name>' to both services in the app YAML)")
    if chosen["used_by"]:
        raise HTTPException(
            409, f"{chosen['path']} already belongs to collection "
                 f"{chosen['used_by']!r} — a corpus folder can serve only "
                 f"one collection")
    return Path(chosen["path"])


def _ack_for_acquisition(plugin: str, acquire_enabled: bool,
                          acknowledged: bool) -> str | None:
    """Returns the license_ack to record, None when none is needed, or
    raises 409 when acquisition of a yellow-class source was requested
    without explicit acknowledgement — the client then shows the terms and
    retries with acknowledge_terms=true. The ack itself is computed
    server-side from the shipped terms text, identical to
    `corpus add --ack-terms`."""
    spec = ACQUIRE_REGISTRY.get(plugin)
    if not acquire_enabled or spec is None:
        return None
    if spec.license_class.value != "yellow":
        return None
    if spec.terms_text_path is None or not spec.terms_text_path.is_file():
        raise HTTPException(500, f"{plugin}: yellow-class source ships no terms text")
    if not acknowledged:
        raise HTTPException(
            409, "terms-acknowledgement-required: this source's license "
                 "requires explicit acknowledgement before acquisition "
                 "(license gate, never bypassed)")
    return acquire_engine.make_ack(spec.terms_text_path)


def _ack_for_pack(corpus_dir, acknowledged: bool) -> str | None:
    """The same gate for a PACKED collection: the pack's manifest names the
    class, its TERMS.txt is the byte-exact terms text (the origin's), and
    a yellow/red pack needs the acknowledgement before the collection
    exists at all — a folder is not a bypass (invariant 9)."""
    from .pack import manifest as pack_manifest
    from .plugins.packed import for_pack
    if not pack_manifest.is_pack_dir(corpus_dir):
        raise HTTPException(400, f"{corpus_dir}: not a pack (no manifest.json) — choose a "
                                  f"folder listed as (pack)")
    src = for_pack(corpus_dir)
    if src.license_class.value not in ("yellow", "red"):
        return None
    terms = src.terms_path
    if terms is None:
        raise HTTPException(400, f"{corpus_dir}: the pack declares license class "
                                  f"{src.license_class.value} but ships no TERMS.txt")
    if not acknowledged:
        raise HTTPException(
            409, "terms-acknowledgement-required: this pack's license requires "
                 "explicit acknowledgement before its records are used "
                 "(license gate, never bypassed)")
    return acquire_engine.make_ack(terms)


@admin_router.post("/library/new/create")
def new_collection_create(body: CreateCollectionBody):
    """Writes the collection's Tier-2 TOML — the same file an operator would
    save by hand from the preview. Paths are auto-isolated per collection
    (DATA_DIR/<name>/...) because the TOML defaults deliberately point at the
    FIRST collection's paths, and two collections must never share a
    manifest or vector table (one embedding space per table, invariant 1)."""
    if not _COLLECTION_NAME.match(body.name):
        raise HTTPException(400, "collection name must be [a-z0-9][a-z0-9_-]{0,63}")
    if body.plugin not in collections_mod.PLUGIN_REGISTRY:
        raise HTTPException(400, f"unknown plugin: {body.plugin!r}")
    if collections_mod.by_name(body.name) is not None:
        raise HTTPException(409, f"collection {body.name!r} already exists")
    # "(hand-populated)": no profile, no acquisition — the operator fills
    # the corpus folder themselves (rsync, zfs send, files staged by hand)
    # and discovery simply indexes what appears. Selecting it forces
    # acquisition off: a profile is what acquisition fetches BY.
    hand_populated = body.profile in ("", "hand-populated")
    if hand_populated:
        body.profile = ""
        body.acquire_enabled = False
    create_spec = ACQUIRE_REGISTRY.get(body.plugin)
    if body.extras:
        offered = create_spec.extras if create_spec is not None else {}
        unknown = [e for e in body.extras if e not in offered]
        if unknown:
            raise HTTPException(400, f"unknown extra format(s) "
                                      f"{', '.join(unknown)} — this source "
                                      f"offers: {', '.join(sorted(offered)) or 'none'}")
        if hand_populated:
            raise HTTPException(400, "extras need acquisition — a "
                                      "hand-populated collection fetches nothing")
    # acquisition SOURCE (the create form's drop-down): the origin, a
    # peer's torrent, or nothing (hand-populated)
    if body.acquire_source:
        if body.acquire_source not in ("source", "peers", "off"):
            raise HTTPException(400, "acquire_source must be source | peers | off")
        if hand_populated and body.acquire_source != "off":
            raise HTTPException(400, "a hand-populated collection acquires nothing")
        body.acquire_enabled = body.acquire_source != "off"
        if body.acquire_source != "peers":
            body.peer_source = ""
    peer = body.peer_source.strip()
    if body.acquire_source == "peers" and not peer:
        raise HTTPException(400, "fetching from peers needs a magnet URI or a "
                                  ".torrent path (peer_source)")
    # one source, or several one per line / comma-separated (the per-
    # language torrents of a pre-embedded corpus, fetched in this order)
    from .acquire.engine import peer_sources as _peer_sources
    for src in _peer_sources(type("_P", (), {"peer_source": peer})()):
        if not (src.startswith("magnet:") or src.endswith(".torrent")):
            raise HTTPException(400, f"peer_source entry is neither a magnet: URI nor a "
                                      f".torrent path: {src[:60]}")
    toml_path = collections_mod.config_toml_path(body.name)
    if toml_path.exists():
        raise HTTPException(409, f"{toml_path} already exists")

    # The collections/ directory EXISTING is what flips load_all() from
    # synthesized-default mode to TOML mode — without this, creating an
    # empty new collection would silently disown the default collection
    # and everything in it (observed live: 79k books vanished behind a
    # test 'pubmed' TOML). Capture the synthesized default BEFORE the
    # directory is created and materialize it, so what exists keeps
    # existing. (An operator who removed the default on purpose has an
    # existing, empty directory: nothing is synthesized, nothing comes back.)
    legacy = [c for c in collections_mod.load_all() if collections_mod.is_synthesized(c)]
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    if legacy:
        for existing in legacy:
            _write_collection_toml(
                collections_mod.config_toml_path(existing.name),
                plugin=existing.plugin, profile=existing.profile,
                acquire_enabled=existing.acquire_enabled,
                corpus_dir=existing.corpus_dir,
                manifest=existing.manifest_path, fts=existing.fts_dir,
                lancedb=existing.lance_dir, license_ack=existing.license_ack,
                genesis_archive=existing.genesis_archive,
                embed=collections_mod.recipe_for(existing),
                embed_mode=existing.embed_mode_default,
                snapshot=existing.snapshot, extras=existing.extras,
                peer_source=existing.peer_source)

    # A corpus folder is CHOSEN, never defaulted. The old fallback
    # (CORPUS_DIR/<name>) silently invented a path that existed in no
    # container, so the collection was born pointing at nothing and only
    # said so as "corpus dir not found" on the next discovery round. An
    # unmounted corpus is not a state worth being able to create.
    #
    # EVERYTHING is validated before ANYTHING is written: a companion that
    # fails validation must not leave the main collection half-created.
    mounts = _corpus_mounts()
    corpus_dir = _resolve_corpus_mount(body.corpus_dir, mounts)

    ack = _ack_for_acquisition(body.plugin, body.acquire_enabled,
                                body.acknowledge_terms)
    if body.plugin == "packed":
        ack = _ack_for_pack(corpus_dir, body.acknowledge_terms)

    companion = None
    if body.companion_corpus_dir.strip():
        if body.plugin != "wikipedia":
            raise HTTPException(400, "the pre-embedded companion is offered "
                                     "for the wikipedia plugin only")
        companion_name = f"{body.name}-vec"
        if collections_mod.by_name(companion_name) is not None or \
                collections_mod.config_toml_path(companion_name).exists():
            raise HTTPException(409, f"companion collection {companion_name!r} "
                                     f"already exists")
        companion_dir = _resolve_corpus_mount(body.companion_corpus_dir, mounts,
                                              what="companion")
        if companion_dir == corpus_dir:
            raise HTTPException(409, "the companion needs its OWN corpus "
                                     "folder — the frozen parquet snapshot and "
                                     "the live dumps cannot share one")
        # the embedded plugin is yellow-class with its own terms text: its
        # acknowledgement is separate from the main plugin's, never implied
        try:
            companion_ack = _ack_for_acquisition("embedded", True,
                                                 body.acknowledge_companion_terms)
        except HTTPException as e:
            if "terms-acknowledgement-required" in str(e.detail):
                raise HTTPException(
                    409, "companion-terms-acknowledgement-required: the "
                         "pre-embedded companion carries its own license "
                         "terms (embedded plugin)") from None
            raise
        companion = (companion_name, companion_dir, companion_ack)

    # default ON: a plugin with a known published-embeddings archive gets it
    # declared, so the worker reuses pre-embedded data before spending GPU.
    # Operators opt out by deleting the line from the TOML.
    from .bundles.genesis import DEFAULT_ARCHIVE_BY_PLUGIN
    genesis_archive = DEFAULT_ARCHIVE_BY_PLUGIN.get(body.plugin)

    base = config.DATA_DIR / body.name
    _write_collection_toml(
        toml_path, plugin=body.plugin, profile=body.profile,
        acquire_enabled=body.acquire_enabled,
        corpus_dir=corpus_dir,
        manifest=base / "manifest.db", fts=base / "fts",
        lancedb=base / "lancedb", license_ack=ack,
        genesis_archive=genesis_archive,
        # Explicit from birth: the space this collection will pin at its
        # first validation is written down, not implied by env. Switching
        # encoders before anything embeds stays a pin reset + TOML edit,
        # exactly as before — just visible now.
        #
        # The node's per-plugin default wins over the env recipe, because
        # env is the TEXT encoder and some plugins are not text. Two genome
        # collections were created here against the node default and pinned
        # a Qwen3 text space over DNA before anyone noticed; a genome
        # collection embedded with a language model is not a worse choice,
        # it is a wrong one.
        embed=(collections_mod.default_recipe_for_plugin(body.plugin)
               or collections_mod.env_recipe()),
        extras=tuple(body.extras),
        peer_source=(body.peer_source.strip() or None))

    companion_note = ""
    if companion is not None:
        companion_name, companion_dir, companion_ack = companion
        companion_base = config.DATA_DIR / companion_name
        _write_collection_toml(
            collections_mod.config_toml_path(companion_name),
            plugin="embedded", profile="all", acquire_enabled=True,
            corpus_dir=companion_dir,
            manifest=companion_base / "manifest.db",
            fts=companion_base / "fts", lancedb=companion_base / "lancedb",
            license_ack=companion_ack,
            genesis_archive=DEFAULT_ARCHIVE_BY_PLUGIN.get("embedded"),
            embed=(collections_mod.default_recipe_for_plugin("embedded")
                   or collections_mod.env_recipe()),
            # 'fix' from birth: everything arrives pre-embedded via genesis;
            # the local encoder only mops up what the archive cannot cover
            # (shard-boundary straddlers), which is minutes of CPU, once
            embed_mode="all")
        companion_note = (
            f" Companion {companion_name!r} created beside it: it acquires "
            f"the published pre-embedded snapshot and imports its vectors "
            f"(no GPU work), so dense search answers from day one while "
            f"{body.name!r} embeds lazily where queries lead.")

    con = db.connect()
    db.log_event(con, "info", "control",
                 f"collection {body.name!r} created via admin UI ({toml_path})"
                 + (f"; companion {companion[0]!r} created" if companion else ""))
    note = ("Live now: the web process sees it immediately and the worker "
            "picks it up within one round (no restart). If your config dir "
            "is versioned, commit the new file. A yellow-class source still "
            "needs its license ack before acquisition (corpus add --ack-terms)."
            + companion_note)
    # Always true now: the corpus is picked from _corpus_mounts(), which
    # lists directories that exist. Kept in the response because callers
    # (and the tests) read it as the "this collection can actually see its
    # data" flag, and a mount can still vanish between listing and create.
    corpus_mounted = corpus_dir.is_dir()
    if not corpus_mounted:
        note = (f"CORPUS PATH DISAPPEARED BETWEEN LISTING AND CREATE: "
                 f"{corpus_dir} is no longer present in the container. Check "
                 f"the mount is still declared for BOTH services in the app "
                 f"YAML. Until then discovery reports 'corpus dir not "
                 f"found'. ") + note
    return {"ok": True, "toml_path": str(toml_path),
            "corpus_dir": corpus_dir.as_posix(), "corpus_mounted": corpus_mounted,
            "companion": (companion[0] if companion else None),
            "note": note}


@admin_router.post("/library/{name}/remove")
def collection_remove(name: str):
    """Removing = retiring the TOML (renamed aside with a timestamp, never
    deleted). Data on disk is NEVER touched — the manifest, FTS index and
    vector table stay where they are and are reported back, so removal is
    reversible by renaming the file back."""
    collection = _collection_or_404(name)
    toml_path = collections_mod.config_toml_path(name)
    if not toml_path.exists():
        if not collections_mod.is_synthesized(collection):
            raise HTTPException(400, f"{name!r} has no TOML file to retire")
        # The synthesized default (legacy mode: no collections/ directory).
        # Creating the EMPTY directory switches the node to TOML mode with
        # zero collections — the default is gone, its data stays on disk,
        # and the create flow declares the next collection explicitly.
        toml_path.parent.mkdir(parents=True, exist_ok=True)
        con = db.connect()
        db.log_event(con, "info", "control",
                     f"synthesized default collection {name!r} removed via admin UI "
                     f"({toml_path.parent} created empty: TOML mode, zero collections; "
                     f"data kept on disk)")
        return {"ok": True, "retired_toml": None,
                "data_kept": {"manifest": str(collection.manifest_path),
                               "fts": str(collection.fts_dir),
                               "lancedb": str(collection.lance_dir)},
                "note": "This was the synthesized default (no TOML). The node now has "
                         "zero collections; create one from the Library. Its data "
                         "stays on disk."}
    retired = toml_path.with_name(f"{name}.toml.removed-{int(time.time())}")
    toml_path.rename(retired)
    con = db.connect()
    db.log_event(con, "info", "control",
                 f"collection {name!r} removed via admin UI (TOML retired to "
                 f"{retired.name}; data kept on disk)")
    return {"ok": True, "retired_toml": str(retired),
            "data_kept": {"manifest": str(collection.manifest_path),
                           "fts": str(collection.fts_dir),
                           "lancedb": str(collection.lance_dir)},
            "note": "The worker stops ingesting it within one round (no "
                     "restart). To restore, rename the retired file back to "
                     + toml_path.name + "."}


class EmbedModeBody(BaseModel):
    mode: str


@admin_router.post("/library/{name}/embed-mode")
def collection_embed_mode(name: str, body: EmbedModeBody):
    """Per-collection GPU-spending control (off | lazy | all) — each
    collection's manifest carries its own scope, so pausing gutenberg's
    backfill never touches pubmed's, and vice versa. The worker re-reads
    scope every round.

    PERSISTENT: this writes BOTH the runtime scope (immediate effect) and
    `embed_mode` in the collection's TOML — a Tier-2 edit through the UI,
    exactly like collection-create and the license ack. The first cut made
    this a runtime-only override that a restart reverted to the TOML, which
    was invariant-10-correct and operationally wrong: the operator set a
    mode seven restarts in a day set back seven times. Invariant 10 holds
    because the UI now edits desired state rather than the DB pretending to
    be it; POST /api/scope remains the runtime-only override for anyone who
    wants one."""
    if body.mode not in ("off", "lazy", "all"):
        raise HTTPException(400, "mode must be off | lazy | all")
    collection = _collection_or_404(name)
    # TOML FIRST — a file write never loses a lock race. The old order
    # wrote the runtime scope first, and on a manifest under continuous
    # pipeline writes that raised "database is locked" BEFORE the TOML was
    # ever written: the 500 reloaded the page and the mode reverted
    # (operator report: wikipedia off -> refresh -> on-demand again).
    toml_path = collections_mod.config_toml_path(name)
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    if not toml_path.exists():
        # a synthesized collection (no TOMLs at all yet): materialize what
        # exists first, or writing this one file would disown the rest —
        # the same guard collection-create applies
        for existing in collections_mod.load_all():
            _write_collection_toml(
                collections_mod.config_toml_path(existing.name),
                plugin=existing.plugin, profile=existing.profile,
                acquire_enabled=existing.acquire_enabled,
                corpus_dir=existing.corpus_dir,
                manifest=existing.manifest_path, fts=existing.fts_dir,
                lancedb=existing.lance_dir, license_ack=existing.license_ack,
                genesis_archive=existing.genesis_archive,
                embed=collections_mod.recipe_for(existing),
                embed_mode=existing.embed_mode_default,
                snapshot=existing.snapshot, extras=existing.extras,
                peer_source=existing.peer_source)
    _write_collection_toml(
        toml_path, plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=collection.acquire_enabled,
        corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=collection.license_ack,
        genesis_archive=collection.genesis_archive,
        embed=collections_mod.recipe_for(collection),
        embed_mode=body.mode, snapshot=collection.snapshot,
        extras=collection.extras, peer_source=collection.peer_source)
    # Runtime scope + event log are BEST-EFFORT immediacy on top of the
    # persisted desired state: if the manifest is busy right now, the
    # worker still applies the TOML within a round — the setting is never
    # hostage to the write lock, and the page never 500s over it.
    applied_now = True
    try:
        con = db.connect(collection)
        con.execute("PRAGMA busy_timeout=2000")
        scope = db.get_scope(con)
        scope["embed_mode"] = body.mode
        db.set_scope(con, scope)
        db.log_event(con, "info", "control",
                     f"embed mode -> {body.mode!r} for {name!r} via admin UI "
                     f"(written to {toml_path.name}: permanent)")
    except sqlite3.OperationalError:
        applied_now = False
    return {"ok": True, "embed_mode": body.mode, "persisted": True,
            "applied_now": applied_now,
            "note": None if applied_now else
            "saved to the TOML; the manifest is busy, so the worker applies "
            "it within a round"}


@admin_router.get("/library/{name}/terms")
def collection_terms(name: str):
    """The source's terms text, for the acknowledgement modal."""
    from . import licensing
    collection = _collection_or_404(name)
    path = licensing.terms_path_for(collection)
    cls = licensing.license_class_for(collection)
    if path is None:
        return {"license_class": cls, "terms_text": None}
    return {"license_class": cls, "terms_text": path.read_text(encoding="utf-8")[:8000]}


@admin_router.post("/library/{name}/ack")
def collection_ack(name: str):
    """Record the license acknowledgement for an EXISTING collection and
    enable acquisition — the UI path to what `corpus add --ack-terms` +
    hand-editing the TOML does. Rewrites the collection's TOML with
    license_ack and acquire_enabled=true; the worker hot-reloads it."""
    from . import licensing
    collection = _collection_or_404(name)
    toml_path = collections_mod.config_toml_path(name)
    if not toml_path.exists():
        raise HTTPException(400, f"{name!r} has no TOML file to record the ack in")
    if licensing.terms_path_for(collection) is None:
        raise HTTPException(400, f"{collection.plugin}: no terms text shipped — "
                                  f"nothing to acknowledge")
    ack = licensing.make_ack(collection)
    # acquisition follows the ack only where there is an origin to acquire
    # from; a packed collection is fed by a folder or a torrent, not a spec
    acquire = True if ACQUIRE_REGISTRY.get(collection.plugin) else collection.acquire_enabled
    _write_collection_toml(
        toml_path, plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=acquire, corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=ack,
        genesis_archive=collection.genesis_archive,
        embed=collections_mod.recipe_for(collection),
        embed_mode=collection.embed_mode_default,
        snapshot=collection.snapshot, extras=collection.extras)
    con = db.connect(collection)
    db.log_event(con, "info", "control",
                 f"license terms acknowledged for {name!r} via admin UI "
                 f"({ack.split('/')[0]}); acquisition enabled")
    return {"ok": True, "license_ack": ack,
            "note": "Recorded in the TOML; acquisition is enabled and the "
                     "worker picks it up within one round."}


class BundleExportBody(BaseModel):
    uids: list[str] = []
    limit: int = 100


@admin_router.post("/library/{name}/bundle/export")
def collection_bundle_export(name: str, body: BundleExportBody):
    """Same code path as `python -m app.cli bundle export`, delivered as a
    tar.gz download. The bundle is built in a scratch dir beside the
    collection's data (same filesystem, no /tmp assumptions) and cleaned up
    after the response streams out."""
    from .bundles import export as bundle_export
    collection = _collection_or_404(name)
    con = db.connect(collection)
    uids = body.uids or [r["uid"] for r in con.execute(
        "SELECT uid FROM documents WHERE in_scope=1 AND embed_done=1 "
        "ORDER BY CAST(native_id AS INTEGER) LIMIT ?", (max(1, body.limit),))]
    if not uids:
        raise HTTPException(400, "no embedded documents to export")

    scratch = Path(tempfile.mkdtemp(
        prefix="bundle-export-", dir=collection.manifest_path.parent))
    try:
        result = bundle_export.export_bundle(con, collection, uids,
                                              scratch / "bundle")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tar_path = scratch / f"bundle-{name}-{stamp}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(scratch / "bundle", arcname=f"bundle-{name}-{stamp}")
    except Exception as e:
        shutil.rmtree(scratch, ignore_errors=True)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(400, f"{type(e).__name__}: {e}")
    db.log_event(con, "info", "bundle",
                 f"exported {result['chunks']} chunks / {result['uids']} docs "
                 f"via admin UI ({result['bundle_id'][:16]}…)")
    return FileResponse(
        tar_path, media_type="application/gzip", filename=tar_path.name,
        background=BackgroundTask(shutil.rmtree, scratch, ignore_errors=True))


@admin_router.post("/library/{name}/bundle/import")
async def collection_bundle_import(name: str, request: Request,
                                    filename: str = "upload.tar.gz"):
    """Accepts a bundle tar.gz as the raw request body (no multipart — no new
    dependency) and drops it into the collection's inbox, where the worker's
    existing drain imports it with the full verification ladder next round.
    The UI upload is just a delivery mechanism: admission stays the
    importer's job, identical to a bundle arriving by hand or coordinator."""
    from .bundles import importer as bundle_importer
    collection = _collection_or_404(name)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(filename).stem) or "upload"
    inbox = bundle_importer.inbox_dir(collection)
    entry = inbox / f"{safe_stem}-{int(time.time())}"

    limit = 4 << 30
    received = 0
    with tempfile.NamedTemporaryFile(dir=str(inbox.parent) if inbox.parent.is_dir()
                                      else None, delete=False, suffix=".tar.gz") as spool:
        spool_path = Path(spool.name)
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                spool.close()
                spool_path.unlink(missing_ok=True)
                raise HTTPException(413, "bundle upload exceeds 4 GiB")
            spool.write(chunk)
    if received == 0:
        spool_path.unlink(missing_ok=True)
        raise HTTPException(400, "empty upload")

    try:
        with tarfile.open(spool_path, "r:gz") as tar:
            entry.mkdir(parents=True, exist_ok=True)
            # filter="data" (3.12) refuses path traversal, links, devices
            tar.extractall(entry, filter="data")
    except tarfile.TarError as e:
        shutil.rmtree(entry, ignore_errors=True)
        raise HTTPException(400, f"not a valid bundle tar.gz: {e}")
    finally:
        spool_path.unlink(missing_ok=True)

    # a tar of the bundle DIRECTORY has one wrapping folder — unwrap it so
    # the entry itself holds header.json etc., as the importer expects
    children = list(entry.iterdir())
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for item in inner.iterdir():
            item.rename(entry / item.name)
        inner.rmdir()

    con = db.connect(collection)
    db.log_event(con, "info", "bundle",
                 f"bundle uploaded via admin UI -> inbox/{entry.name} "
                 f"({received} bytes); worker imports it next round")
    return {"queued": True, "entry": entry.name,
            "note": "The worker verifies and imports it within one round; "
                     "watch the System log for the verdict."}



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

    Reports BOTH processes. A token bucket is per-process state, and the
    bytes an operator cares about — acquisition, seeding — move in the
    worker, so this used to be a flat zero line next to a saturated link.
    The worker publishes its own rates to the control table each loop
    (db.publish_worker_rates) and they are summed in here per class.

    The worker's contribution is dropped, not carried forward, once its
    sample goes stale: a frozen number on a live graph reads as traffic that
    is not happening. `worker` in the payload says which of those two
    situations the reader is looking at, so the UI never has to guess.
    """
    payload = transfer_manager.get_manager().rates()
    worker_rates = age = None
    seed_rows, seed_age = [], None
    worker_stats: dict = {}
    con = None
    try:
        # read_only_connect, not connect: this runs every couple of seconds
        # per subscriber, and connect() re-runs the schema script and the
        # ALTER TABLE migrations each call — write-path work on a manifest
        # the pipeline threads are already contending for.
        con = db.read_only_connect()
        if con is not None:
            worker_rates, age = db.read_worker_rates(con)
            # the seeding table rides the SAME cheap reader (one read-only
            # connection per poll — the test pins that): what this node
            # serves to peers, published by the worker's seeder each beat
            from .acquire import seeding
            seed_rows, seed_age = seeding.read_table(con)
            # ...and so does the per-collection stats snapshot the
            # download split below is derived from (one more row)
            worker_stats = (db.read_worker_stats(con)[0] or {})
    except sqlite3.Error:
        # the manifest being briefly unavailable must not break the graph
        worker_rates, age = None, None
    finally:
        if con is not None:
            con.close()
    if worker_rates:
        for direction in ("down", "up"):
            mine = payload.get(direction, {})
            theirs = worker_rates.get(direction, {}) or {}
            for cls, value in theirs.items():
                try:
                    mine[cls] = round(mine.get(cls, 0.0) + float(value), 1)
                except (TypeError, ValueError):
                    continue
            payload[direction] = mine
    payload["worker"] = {
        "reporting": bool(worker_rates),
        "age_seconds": round(age, 1) if age is not None else None,
        "max_age_seconds": db.WORKER_RATES_MAX_AGE_S,
    }
    payload["seeding"] = {"rows": seed_rows,
                          "age_seconds": round(seed_age, 1) if seed_age is not None else None}
    # WHAT A PERSON CAN ACT ON: one download total, one upload total, and
    # the download split BY COLLECTION (measured by the worker's fetch
    # recorder: bytes over wall time). The priority classes above stay in
    # the payload for tests and the API, but they answer a scheduler's
    # question, not an operator's — "interactive 0, delta 0, backfill 0,
    # seeding 0" told nobody which corpus was downloading how fast.
    payload["totals"] = {
        "down": round(sum(float(v or 0) for v in payload.get("down", {}).values()), 1),
        "up": round(sum(float(v or 0) for v in payload.get("up", {}).values()), 1),
    }
    downloads = []
    for name, entry in worker_stats.items():
        try:
            fetch = stats_mod.rates(entry).get("fetch") or {}
        except Exception:   # noqa: BLE001 — a malformed sample must not break the page
            continue
        bps = fetch.get("bytes_per_s_wall")
        if bps:
            downloads.append({"collection": name, "bytes_per_s": round(float(bps), 1)})
    downloads.sort(key=lambda d: -d["bytes_per_s"])
    payload["downloads"] = downloads
    return payload


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
        "collection": c.name, "plugin": _plugin_label(c.plugin), "profile": c.profile,
        "embed_mode": c.embed_mode_default, "acquire_enabled": c.acquire_enabled,
        "license_ack": "(recorded)" if c.license_ack else "(none)",
        "embed_recipe": (f"{c.embed.model} @ {c.embed.url}"
                          + (" (+query encoder)" if c.embed.query_url else "")
                          if c.embed else "(node default)"),
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
    # per-collection [embed] recipes (doc amendment 9): each declared
    # endpoint — including a bi-encoder's query side — is pinged too
    for c in collections_mod.load_all():
        if c.embed is not None:
            endpoints.setdefault(c.embed.url, []).append(f"{c.name}.embed")
            if c.embed.query_url:
                endpoints.setdefault(c.embed.query_url, []).append(f"{c.name}.query")
    return [{"names": names, "url": url, "ok": _ping(url)} for url, names in endpoints.items()]


def _recent_events(con, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def _recent_events_all(limit: int = 60) -> list[dict]:
    """Events from EVERY collection, newest first, tagged with their
    collection.

    Events live in per-collection manifests, so reading only the default
    collection's table hid every genesis verdict, gate pause, acquisition
    failure and import error belonging to the other collections — the
    System page looked calm while a collection was stuck. One connection
    per collection, `limit` rows each, merged and re-sorted here: the row
    counts are small and the alternative (a UNION over attached databases)
    would couple the page to every manifest being attachable at once."""
    rows: list[dict] = []
    for collection in collections_mod.load_all():
        try:
            con = db.connect(collection)
        except Exception:   # noqa: BLE001 — a broken collection must not
            continue        # blank the whole log
        try:
            for r in con.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)):
                event = dict(r)
                event["collection"] = collection.name
                rows.append(event)
        except Exception:   # noqa: BLE001 — same reasoning per collection
            continue
    rows.sort(key=lambda e: (e.get("ts") or 0, e.get("id") or 0), reverse=True)
    return rows[:limit]


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
    return TEMPLATES.TemplateResponse(request, "_fragments/log_rows.html",
                                       {"events": _recent_events_all()})
