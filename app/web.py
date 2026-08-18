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
import secrets
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

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
    return [{"name": n,
             "label": f"{n} (~{_human_bytes(spec.estimated_bytes.get(n))})"}
            for n in names]


def _grow_options(collection: collections_mod.Collection) -> list[dict]:
    """Current profile and larger — never smaller: acquisition only adds
    files, so 'rolling back' a profile wouldn't shrink anything and would
    only misdescribe the corpus. The current profile stays offered as a
    re-sync/top-up."""
    order = _profile_order(collection.plugin)
    current = collection.profile
    names = [o["name"] for o in order]
    if current in names:
        order = order[names.index(current):]
        order[0] = {**order[0], "label": order[0]["label"] + " — current, re-sync"}
    return order


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
        "embed_mode": db.get_scope(con)["embed_mode"],
        "name": collection.name, "plugin": collection.plugin,
        "profile": collection.profile or "(hand-populated)",
        "space_id": ((space_row["space_id"][:10] + "…") if space_row and space_row["space_id"]
                      else "unpinned"),
        "fetched": fetched,
        "total": counts["total"] or 0, "in_scope": counts["in_scope"] or 0,
        "rdf_done": counts["rdf_done"] or 0, "fts_done": counts["fts_done"] or 0,
        "embed_done": counts["embed_done"] or 0,
        "queue": queue, "last_discover_text": freshness,
        "grow_options": _grow_options(collection),
        "license": _license_state(collection),
        "genesis": _genesis_state(con, collection),
    }


def _genesis_state(con, collection) -> dict | None:
    """Provenance visibility for pre-embedded data: is a genesis archive
    declared, and how much of the collection did it cover?"""
    if not collection.genesis_archive:
        return None
    from .bundles.genesis import ARCHIVES, _local_shards
    archive = ARCHIVES.get(collection.genesis_archive)
    totals = con.execute(
        "SELECT COUNT(*) shards, SUM(state='applied') applied, SUM(docs) docs, "
        "SUM(state='rejected') rejected FROM genesis_imports WHERE archive=?",
        (collection.genesis_archive,)).fetchone()
    if archive is not None and archive.local:
        shard_total = len(_local_shards(collection))
    else:
        shard_total = len(archive.shards) if archive else 0
    return {
        "archive": collection.genesis_archive,
        "known": archive is not None,
        "shard_total": shard_total,
        "applied": totals["applied"] or 0,
        "docs": totals["docs"] or 0,
        "rejected": bool(totals["rejected"]),
    }


def _license_state(collection: collections_mod.Collection) -> dict:
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    return {
        "class": spec.license_class.value if spec else None,
        "ack_recorded": bool(collection.license_ack),
        "acquire_enabled": collection.acquire_enabled,
        # the ack button applies when acquisition is gated: yellow class,
        # no recorded ack, and a TOML to write it into
        "needs_ack": bool(
            spec and spec.license_class.value == "yellow"
            and not collection.license_ack
            and collections_mod.config_toml_path(collection.name).exists()),
    }


@admin_router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    collections = collections_mod.load_all()
    con = db.connect()   # worker_state is one global control-table toggle (see worker.py)
    return TEMPLATES.TemplateResponse(request, "library.html", {
        "active": "library",
        "shelves": [_collection_shelf(c) for c in collections],
        "plugins": sorted(collections_mod.PLUGIN_REGISTRY),
        "plugin_profiles": {p: _profile_order(p)
                             for p in collections_mod.PLUGIN_REGISTRY},
        "corpus_mounts": _corpus_mounts(),
        "corpus_mounts_dir": str(config.CORPUS_MOUNTS_DIR),
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
    """Growing = declaring the new profile (Tier-2 TOML). The WORKER then
    acquires: its hot-reload treats a profile change as re-discover+acquire
    within one round, with the download budgeted, resumable, and in the
    byte-moving process. Running the acquisition inside this web request —
    the previous implementation — blocked the response for the duration of
    a multi-GB download (observed live: the grow modal never closed) and
    moved corpus bytes in the wrong process."""
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    if body.profile not in spec.profiles:
        raise HTTPException(400, f"unknown profile {body.profile!r} — "
                                  f"known: {', '.join(spec.profiles)}")
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
        # space id → the pin never notices.
        embed=collections_mod.recipe_for(collection))
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


# ---------------- collection management (create / remove / bundle I/O) ----

_COLLECTION_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _write_collection_toml(path: Path, *, plugin: str, profile: str | None,
                            acquire_enabled: bool, corpus_dir: Path,
                            manifest: Path, fts: Path, lancedb: Path,
                            license_ack: str | None = None,
                            genesis_archive: str | None = None,
                            embed=None) -> None:
    lines = [f'plugin = "{plugin}"']
    if profile:
        lines.append(f'profile = "{profile}"')
    if acquire_enabled:
        lines.append("acquire_enabled = true")
    if license_ack:
        lines.append(f'license_ack = "{license_ack}"')
    if genesis_archive:
        lines.append(f'genesis_archive = "{genesis_archive}"')
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
                                "used_by": used.get(entry.as_posix())})
    return mounts


class CreateCollectionBody(BaseModel):
    name: str
    plugin: str
    profile: str = "demo"
    acquire_enabled: bool = False
    corpus_dir: str = ""     # a CORPUS_MOUNTS_DIR entry; empty = CORPUS_DIR/<name>
    acknowledge_terms: bool = False   # yellow-class sources: explicit consent


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
    toml_path = collections_mod.config_toml_path(body.name)
    if toml_path.exists():
        raise HTTPException(409, f"{toml_path} already exists")

    toml_path.parent.mkdir(parents=True, exist_ok=True)

    # Writing the FIRST TOML flips load_all() from synthesized-default mode
    # to TOML-only mode — without this, creating an empty new collection
    # would silently disown the default collection and everything in it
    # (observed live: 79k books vanished behind a test 'pubmed' TOML).
    # Materialize the currently-synthesized collections so what exists
    # keeps existing.
    if not any(toml_path.parent.glob("*.toml")):
        for existing in collections_mod.load_all():
            _write_collection_toml(
                collections_mod.config_toml_path(existing.name),
                plugin=existing.plugin, profile=existing.profile,
                acquire_enabled=existing.acquire_enabled,
                corpus_dir=existing.corpus_dir,
                manifest=existing.manifest_path, fts=existing.fts_dir,
                lancedb=existing.lance_dir, license_ack=existing.license_ack,
                genesis_archive=existing.genesis_archive,
                embed=collections_mod.recipe_for(existing))

    if body.corpus_dir:
        mounts = _corpus_mounts()
        chosen = next((m for m in mounts if m["path"] == body.corpus_dir.strip()),
                      None)
        if chosen is None:
            available = ", ".join(m["path"] for m in mounts) or "(none)"
            raise HTTPException(
                400, f"corpus folder must be one of the mounted entries under "
                     f"{config.CORPUS_MOUNTS_DIR} — available: {available}. "
                     f"Map your dataset there first (TrueNAS: add "
                     f"'- /mnt/<pool>/<dataset>:{config.CORPUS_MOUNTS_DIR}/"
                     f"<name>' to both services in the app YAML)")
        if chosen["used_by"]:
            raise HTTPException(
                409, f"{chosen['path']} already belongs to collection "
                     f"{chosen['used_by']!r} — a corpus folder can serve only "
                     f"one collection")
        corpus_dir = Path(chosen["path"])
    else:
        corpus_dir = config.CORPUS_DIR / body.name

    ack = _ack_for_acquisition(body.plugin, body.acquire_enabled,
                                body.acknowledge_terms)

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
        # explicit from birth: the space this collection will pin at its
        # first validation is written down, not implied by env. Switching
        # encoders before anything embeds stays a pin reset + TOML edit,
        # exactly as before — just visible now.
        embed=collections_mod.env_recipe())

    con = db.connect()
    db.log_event(con, "info", "control",
                 f"collection {body.name!r} created via admin UI ({toml_path})")
    note = ("Live now: the web process sees it immediately and the worker "
            "picks it up within one round (no restart). If your config dir "
            "is versioned, commit the new file. A yellow-class source still "
            "needs its license ack before acquisition (corpus add --ack-terms).")
    corpus_mounted = corpus_dir.is_dir()
    if not corpus_mounted:
        # only reachable via the default path (chosen mounts exist by
        # construction) — the operator either creates that folder or maps a
        # dataset under the mounts root and re-creates the collection
        note = (f"CORPUS PATH NOT PRESENT IN THE CONTAINER: {corpus_dir} does "
                 f"not exist yet. Either create it, or map a dedicated dataset "
                 f"under {config.CORPUS_MOUNTS_DIR} (TrueNAS: add "
                 f"'- /mnt/<pool>/<dataset>:{config.CORPUS_MOUNTS_DIR}/<name>' "
                 f"to BOTH services in the app YAML, save, and pick it in the "
                 f"corpus dropdown). Until then discovery reports 'corpus dir "
                 f"not found'. ") + note
    return {"ok": True, "toml_path": str(toml_path),
            "corpus_dir": corpus_dir.as_posix(), "corpus_mounted": corpus_mounted,
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
        raise HTTPException(
            400, f"{name!r} is the synthesized default collection (no TOML file) — "
                 f"it exists as long as no collections/ directory defines others, "
                 f"and cannot be removed from the UI")
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
    scope every round."""
    if body.mode not in ("off", "lazy", "all"):
        raise HTTPException(400, "mode must be off | lazy | all")
    collection = _collection_or_404(name)
    con = db.connect(collection)
    scope = db.get_scope(con)
    scope["embed_mode"] = body.mode
    db.set_scope(con, scope)
    db.log_event(con, "info", "control",
                 f"embed mode -> {body.mode!r} for {name!r} via admin UI")
    return {"ok": True, "embed_mode": body.mode}


@admin_router.get("/library/{name}/terms")
def collection_terms(name: str):
    """The source's terms text, for the acknowledgement modal."""
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    if spec.terms_text_path is None or not spec.terms_text_path.is_file():
        return {"license_class": spec.license_class.value, "terms_text": None}
    return {"license_class": spec.license_class.value,
            "terms_text": spec.terms_text_path.read_text()[:8000]}


@admin_router.post("/library/{name}/ack")
def collection_ack(name: str):
    """Record the license acknowledgement for an EXISTING collection and
    enable acquisition — the UI path to what `corpus add --ack-terms` +
    hand-editing the TOML does. Rewrites the collection's TOML with
    license_ack and acquire_enabled=true; the worker hot-reloads it."""
    collection = _collection_or_404(name)
    spec = _spec_or_400(collection)
    toml_path = collections_mod.config_toml_path(name)
    if not toml_path.exists():
        raise HTTPException(400, f"{name!r} has no TOML file to record the ack in")
    if spec.terms_text_path is None or not spec.terms_text_path.is_file():
        raise HTTPException(400, f"{collection.plugin}: no terms text shipped — "
                                  f"nothing to acknowledge")
    ack = acquire_engine.make_ack(spec.terms_text_path)
    _write_collection_toml(
        toml_path, plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=True, corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=ack,
        genesis_archive=collection.genesis_archive,
        embed=collections_mod.recipe_for(collection))
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
