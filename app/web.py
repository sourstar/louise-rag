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
from concurrent.futures import ThreadPoolExecutor
import time
import traceback
from dataclasses import asdict
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import (auth, cancel, collections as collections_mod, config, db, discovery, gpu, llm,
                node_state, platform_config, query_service, reader, retrieval, spaces)
from . import translate as mt
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


def _asset_version() -> str:
    """A tag that changes when the served assets do, for cache-busting.

    `StaticFiles` sends an ETag and a Last-Modified and no `Cache-Control`,
    so a browser applies HEURISTIC caching — typically a tenth of the
    file's age — and may serve a stale copy for hours without ever
    revalidating. That is invisible until a CSS change lands and the page
    keeps rendering the old rules.

    Observed 2026-09-10: a licence badge gained `.lic-yellow`, the server
    served it, and the browser kept a cached stylesheet that had no such
    rule — so the span matched nothing, inherited the badge's green, and
    the fix looked like it had not shipped. A URL that changes with the
    file makes that impossible rather than a thing to remember.

    The newest mtime across the static tree, not a build constant: it is
    right in development, where files change without a rebuild, and right
    on the node, where a deploy rewrites them.
    """
    d = Path(__file__).parent / "static"
    try:
        newest = max((p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()),
                     default=0)
    except OSError:
        return "0"
    return format(newest & 0xFFFFFFFF, "x")


TEMPLATES.env.globals["asset_v"] = _asset_version()
# The node's four modes, their words and what each runs — the ONE table the
# worker reads (app/node_state.py: MODE_RUNS), as template GLOBALS: a page
# cannot be rendered without them, so a button cannot promise what the node
# does not do, whoever builds the context.
TEMPLATES.env.globals.update(mode_labels=node_state.MODE_LABELS,
                             mode_says=node_state.MODE_SAYS,
                             mode_runs=node_state.runs)
# Dot-grouped digits ("4.020.578"): a comma reads as a decimal separator to
# half the world — an operator read "+377,217/h" as 377.217 docs/h and
# computed an 11-year ETA from a 4-day one. Dots as thousands separators,
# everywhere a count is shown.
TEMPLATES.env.filters["num"] = (
    lambda n: "" if n is None else f"{int(n):,}".replace(",", "."))
TEMPLATES.env.filters["size"] = gpu.size


def _ago(when) -> str:
    """A unix time as how long ago: '12 s ago', '5 min ago', '3 h ago'."""
    try:
        s = max(0, int(time.time() - float(when)))
    except (TypeError, ValueError):
        return ""
    if s < 90:
        return f"{s} s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} d ago"


TEMPLATES.env.filters["ago"] = _ago


#: What the ETA slot shows when work REMAINS and there is no rate to stand
#: behind. A dash, not "TBD" (operator, 2026-09-12): the card is read to
#: see whether anything is moving, and a word the reader has to look up is
#: worse than an honest blank. "TBD" also stood for four different states —
#: no history yet, one window so far, stalled, and not scheduled — none of
#: which it distinguished. The dash carries its reason in the tooltip.
#:
#: Nothing at all is shown once a stage is DONE. That half was already the
#: intent (`_human_eta` returns None "so the card shows NOTHING rather than
#: a placeholder") and it was unreachable on the dense row, which completed
#: against a denominator no stage could ever reach — see `stage_targets`.
ETA_UNKNOWN = "-"


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
    # "the maximum number of results a query returns before and after
    # reranking, with as default the recommended amount" (operator,
    # 2026-09-18): 05 §5.4's `limit` and §5.6's depth, per request
    limit: int = Field(retrieval.DEFAULT_RESULTS, ge=1, le=retrieval.MAX_RESULTS)
    rerank_depth: int = Field(retrieval.RERANK_TOP_N, ge=1, le=retrieval.MAX_RERANK_DEPTH)
    # the question into EVERY language held, not only the main ones — only
    # when asked (operator, 2026-09-18)
    all_languages: bool = False


# "show results in" — the CONTENT languages the Ask page offers. The
# pre-embedded corpus's eleven plus the common European ones; the model
# translates into any ISO code, so an operator can type another into the
# TOML later without a code change.
#
# THE THIRD COLUMN IS THE OPTION'S OWN LABEL, WRITTEN IN THE LANGUAGE IT
# SELECTS (operator, 2026-09-12). The page chrome stays in one stable
# language — nav, headings, buttons, badges, drawer labels — and this is
# the one carve-out, for the reason every operating system's language
# picker makes the same one: a label you cannot read is not a control.
# Someone looking for Persian finds "نتایج به فارسی", not a row of English
# they must first learn to parse.
#
# STATIC, never a model call: the picker has to render instantly and with
# the encoder down, and a phrase that arrived by translation could not be
# trusted to name its own language. A code added to the TOML later with no
# phrase falls back to its endonym alone, which is still readable.
RESULT_LANGUAGES = (
    ("en", "English", "results in English"),
    ("de", "Deutsch", "Ergebnisse auf Deutsch"),
    ("fr", "Français", "résultats en français"),
    ("it", "Italiano", "risultati in italiano"),
    ("es", "Español", "resultados en español"),
    ("pt", "Português", "resultados em português"),
    ("nl", "Nederlands", "resultaten in het Nederlands"),
    ("pl", "Polski", "wyniki po polsku"),
    ("ru", "Русский", "результаты на русском"),
    ("ja", "日本語", "日本語で表示"),
    ("zh", "中文", "以中文显示"),
    ("tr", "Türkçe", "sonuçları Türkçe göster"),
    ("fa", "فارسی", "نتایج به فارسی"),
)


@ask_router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "ask.html", {
        "active": "ask", "collections": collections_mod.load_all(), "result": None,
        "result_languages": RESULT_LANGUAGES,
        "default_results": retrieval.DEFAULT_RESULTS,
        "default_rerank_depth": retrieval.RERANK_TOP_N,
        "max_results": retrieval.MAX_RESULTS,
        "max_rerank_depth": retrieval.MAX_RERANK_DEPTH})


class TranslateBody(BaseModel):
    lang: str
    items: list[dict]          # [{"id": ..., "text": ..., "src": <its language>}]


# (lang, text) -> translation. Toggling the drop-down back and forth must
# not pay the model twice; bounded so a long session cannot grow it.
#
# KEYED ON THE TEXT, not the item's id. It was (id, lang), and the answer's
# id is one fixed string for every question — so switching a SECOND answer
# into a language the first had been switched into was served the FIRST
# answer's translation. A key on what is translated cannot do that, and
# two items with one text (a title two hits share, "CHAPTER I") share one
# translation for free.
_translation_cache: dict = {}
_TRANSLATION_CACHE_MAX = 4000
#: retry batch size for items the model returned verbatim. Three, because
#: three is the size measured to work where eleven did not.
_RETRY_BATCH = 3
#: above this many characters an item is translated ALONE and streamed —
#: an entry in full, the answer; at or below it, batched like a title
_STREAM_OVER = 400


def _remember(lang: str, text: str, value: str) -> None:
    if len(_translation_cache) >= _TRANSLATION_CACHE_MAX:
        _translation_cache.clear()
    _translation_cache[(lang, text)] = value


def _mt_items(lang: str, items: list[tuple[str, str, str]]
              ) -> tuple[dict[str, str], list[str]] | None:
    """(id, text, src) items through the translation model (app/translate.py):
    ({id: translation}, missed ids), or None when it could not be asked — the
    caller then falls back to the chat model. An output equal to its input
    is a translation here: the model is deterministic, so a name that
    stays a name is its answer, not the chat model's copy-through."""
    if not items:
        return {}, []
    got = mt.translate([(text, lang) for _cid, text, _src in items])
    if not got:
        return None
    out: dict[str, str] = {}
    missed: list[str] = []
    for (cid, text, _src), value in zip(items, got):
        if value is None:
            missed.append(cid)
            continue
        out[cid] = value
        _remember(lang, text, value)
    return out, missed


def _translate_items(body: TranslateBody) -> list[tuple[str, str, str]]:
    """(id, text, source language) for every item with something to say."""
    return [(str(i.get("id", "")), str(i.get("text", "")),
             str(i.get("src", "") or "?").lower())
            for i in body.items if str(i.get("text", "")).strip()]


def _translate_batch(lang: str, items: list[tuple[str, str, str]], use_mt: bool = True
                     ) -> tuple[dict[str, str], bool, list[str]]:
    """Short items into `lang`, batched: ({id: translation}, failed, missed).
    `failed` means nothing came back at all; `missed` names the items that
    did not while others did."""
    if use_mt and mt.available():
        via = _mt_items(lang, [it for it in items if (lang, it[1]) not in _translation_cache])
        if via is not None:
            got, missed = via
            done = {cid: got.get(cid) or _translation_cache.get((lang, text))
                    for cid, text, _src in items}
            done = {cid: v for cid, v in done.items() if v is not None}
            return done, not done and bool(missed), missed
    out: dict[str, str] = {}
    groups: dict[str, list[tuple[str, str]]] = {}
    for cid, text, src in items:
        hit = _translation_cache.get((lang, text))
        if hit is not None:
            out[cid] = hit
        else:
            # ONE BATCH PER SOURCE LANGUAGE. A mixed batch of SHORT items
            # comes back with some of them copied through untranslated —
            # measured on the node 2026-09-12: 11 titles in Chinese,
            # Greek, French, English and more returned 7 unchanged, while
            # the same English titles in an English-only batch translated
            # 3 of 3. Long passages do not show it (the mixed snippet
            # batch translates English fine), so the discriminator is
            # length, not language: a short phrase that already reads
            # well is "close enough" to leave, and grouping removes the
            # comparison that invites it.
            #
            # Two other fixes were tried first and MEASURED WORSE or
            # neutral: relaxing the prompt's "keep names as they are"
            # (which reads as covering titles) changed nothing, and
            # telling the model every item must come back translated took
            # unchanged from 7 to 9.
            groups.setdefault(src, []).append((cid, text))
    failed = False
    missed: list[str] = []
    retry: list[tuple[str, str]] = []
    if groups:
        # CONCURRENTLY: measured, not assumed — four calls to this node's
        # Ollama took 6.85 s of wall clock against 17.13 s of summed
        # parts. So grouping buys correctness AND cuts the wait that made
        # the switch look broken; serialising the groups would have paid
        # for the first with the second.
        def _one(batch: list[tuple[str, str]]) -> list[str | None]:
            return llm.translate_texts([t for _, t in batch], lang)

        order = list(groups.values())
        with ThreadPoolExecutor(max_workers=min(6, len(order))) as pool:
            results = list(pool.map(_one, order))

        pending: list[tuple[str, str]] = []
        translated: list[str | None] = []
        for batch, got in zip(order, results):
            if len(got) != len(batch):
                # this SOURCE LANGUAGE failed; the others may not have,
                # so its items are misses rather than everyone's failure
                missed.extend(cid for cid, _ in batch)
                continue
            pending.extend(batch)
            translated.extend(got)
        failed = not pending and bool(missed)
        if pending:
            for (cid, original), value in zip(pending, translated):
                # A DROPPED PASSAGE IS NEVER CACHED, and never reported as
                # a translation. `translate_texts` returns None for one,
                # and it used to return the original — which this then
                # stored as the translation, so the passage could never be
                # translated again for the life of the process.
                # Re-selecting the language could not repair it, which is
                # exactly what the switch is supposed to be able to do. A
                # value equal to its own input is refused too: the same
                # permanent-miss shape, arriving by a different route.
                if value is None or value == original:
                    missed.append(cid)
                    retry.append((cid, original))
                    continue
                out[cid] = value
                _remember(lang, original, value)

    # ONE RETRY, IN SMALL BATCHES — the correction loop, not a fourth
    # guess about the model. A batch that comes back with short items
    # copied through verbatim is the failure this endpoint keeps hitting,
    # and three attempts to predict it all missed: relaxing the prompt's
    # "keep names as they are" changed nothing, telling the model every
    # item must be translated made it WORSE (7 unchanged became 9), and
    # grouping by source language fixed the latency but not this.
    #
    # What IS established by measurement is that a SMALL batch works —
    # three English titles alone translated three of three, the same
    # three that a batch of eleven returned verbatim. So the copy-through
    # is detected (a value equal to its input already counts as a miss)
    # and just those items go back in threes. Whatever is still unchanged
    # after that is an honest miss and the page says so.
    if retry:
        chunks = [retry[i:i + _RETRY_BATCH] for i in range(0, len(retry), _RETRY_BATCH)]
        with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as pool:
            second = list(pool.map(
                lambda b: llm.translate_texts([t for _, t in b], lang), chunks))
        for batch, got in zip(chunks, second):
            if len(got) != len(batch):
                continue
            for (cid, original), value in zip(batch, got):
                if value is None or value == original:
                    continue
                out[cid] = value
                missed = [m for m in missed if m != cid]
                _remember(lang, original, value)
    return out, failed, missed


@ask_router.post("/ask/translate")
def ask_translate(body: TranslateBody):
    """Translate result CONTENT into `lang` in one reply — never authors or
    ids, never the interface. The Ask page uses the streaming route below;
    this stays for a client that wants the whole answer at once. Failure
    degrades to the originals with `failed` set so the page can say so
    instead of pretending."""
    lang = body.lang.strip().lower()
    if not lang or lang == "original":
        return {"lang": lang, "items": {}, "failed": False}
    # `failed` stays a BOOL: it means "nothing came back", which is what
    # the page says "translation unavailable" about. `missed` is the
    # partial case — some passages translated, some did not — and the page
    # shows what it got and names the rest rather than discarding good
    # content for the sake of a uniform screen.
    out, failed, missed = _translate_batch(lang, _translate_items(body))
    return {"lang": lang, "items": out, "failed": failed, "missed": missed}


@ask_router.post("/ask/translate/stream")
def ask_translate_stream(body: TranslateBody):
    """Everything on the Ask page into `lang`, AS IT IS WRITTEN — one JSON
    object per line:

        {"id", "text"}      an item's finished translation
        {"id", "delta"}     the next piece of a long item's translation
        {"id", "missed"}    no usable translation: show the original
        {"done", "failed", "missed"}   the end; `failed` = nothing came back

    The entries are translated IN FULL (operator, 2026-09-18), and one
    whole chunk measured 58.6 s on the node — its first words after
    1.96 s. The node's Ollama runs one request at a time, so a page of
    entries is minutes of model time however it is split; what can be
    immediate is that the page starts changing at once. So: what is cached
    first, then the short items in one batch per source language (titles,
    chapters — the page is readable at a glance within seconds), then each
    long item streamed in the order the page sent them (the answer, then
    the entries by rank). A page that switches again aborts this request,
    and the model stops with it."""
    lang = body.lang.strip().lower()
    items = _translate_items(body)

    def line(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False) + "\n"

    def gen():
        if not lang or lang == "original":
            yield line({"done": True, "failed": False, "missed": []})
            return
        got = 0
        missed: list[str] = []
        pending: list[tuple[str, str, str]] = []
        for cid, text, src in items:
            hit = _translation_cache.get((lang, text))
            if hit is not None:
                got += 1
                yield line({"id": cid, "text": hit})
            else:
                pending.append((cid, text, src))
        if mt.available():
            # THE TRANSLATION MODEL, in the page's order and in rounds the
            # page can watch arrive: the short items in one call (titles,
            # headings — measured 1.1 s for six), the first long one alone
            # (the answer, or the top entry: 5.9 s for a whole 3.8 kB entry),
            # then the entries three at a time, each round a batch on the GPU.
            short = [it for it in pending if len(it[1]) <= _STREAM_OVER]
            long_ = [it for it in pending if len(it[1]) > _STREAM_OVER]
            rounds = ([short] if short else []) + [long_[:1]] * bool(long_) + \
                [long_[i:i + 3] for i in range(1, len(long_), 3)]
            pending = []
            for n, round_ in enumerate(rounds):
                via = _mt_items(lang, round_)
                if via is None:
                    # unreachable: what is left goes to the chat model below
                    pending = [it for r in rounds[n:] for it in r]
                    break
                out, round_missed = via
                for cid, _text, _src in round_:
                    if cid in out:
                        got += 1
                        yield line({"id": cid, "text": out[cid]})
                    else:
                        yield line({"id": cid, "missed": True})
                missed += round_missed
        short = [it for it in pending if len(it[1]) <= _STREAM_OVER]
        long_ = [it for it in pending if len(it[1]) > _STREAM_OVER]
        if short:
            # the translation model already failed or is not there
            out, _failed, short_missed = _translate_batch(lang, short, use_mt=False)
            for cid, _text, _src in short:
                if cid in out:
                    got += 1
                    yield line({"id": cid, "text": out[cid]})
            for cid in short_missed:
                yield line({"id": cid, "missed": True})
            missed += short_missed
        # A MODEL THAT CANNOT BE REACHED IS ASKED ONCE, not once per entry:
        # a call that raised before writing a word marks the rest missed
        # instead of queueing each behind the same failure.
        down = False
        for cid, text, _src in long_:
            if down:
                missed.append(cid)
                yield line({"id": cid, "missed": True})
                continue
            parts: list[str] = []
            try:
                for delta in llm.translate_stream(text, lang):
                    parts.append(delta)
                    yield line({"id": cid, "delta": delta})
            except Exception:   # noqa: BLE001 — the item is a miss, the page says so
                down = not parts
            value = "".join(parts).strip()
            if llm.is_translation(value, text, lang):
                _remember(lang, text, value)
                got += 1
                yield line({"id": cid, "text": value})
            else:
                missed.append(cid)
                yield line({"id": cid, "missed": True})
        yield line({"done": True, "failed": got == 0 and bool(missed), "missed": missed})

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


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
    # invariant 9: the whole text, to anyone who can reach Ask — the most
    # exposed door of all, and it never asked (audit, 2026-09-13)
    _refuse_withheld(c, "its text is read")
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
async def ask_query(request: Request, body: AskQueryBody):
    """The Ask page's search, stopped if the page's request goes — a reader
    who asks again, or closes the tab, leaves no search running for nobody
    (app/cancel.py)."""
    try:
        result = await cancel.while_connected(request, _ask_result, body)
    except cancel.Cancelled:
        return Response(status_code=499)
    return TEMPLATES.TemplateResponse(request, "_ask_results.html", {"result": result})


def _ask_result(body: AskQueryBody) -> dict:
    result = query_service.run_search(body.collections, body.query, body.limit, body.mode, True,
                                      rerank_depth=body.rerank_depth,
                                      all_languages=body.all_languages)
    # every hit names its COLLECTION for the reader panel: federated hits
    # carry it; a single-collection hit is resolved from its source prefix
    # (the plugin id) to the first collection of that plugin
    by_plugin = {}
    for c in collections_mod.load_all():
        by_plugin.setdefault(c.plugin, c.name)
    for h in result.get("hits", []):
        plugin = str(h.get("chunk_id", "")).split(":", 1)[0]
        if not h.get("source_collection"):
            h["source_collection"] = by_plugin.get(plugin)
        # THE LINK TO THE REAL THING. `reader.html` has offered "open at
        # the source" all along, but the Ask page's reader panel loads the
        # FRAGMENT, which carries no header — so in the Ask flow the only
        # link was `/read/<collection>/<book>`, this node's own path.
        # Clicking an article took the reader to us instead of to PubMed
        # (operator, 2026-09-12).
        if not h.get("origin_url"):
            make = reader.ORIGIN.get(plugin)
            if make is not None:
                try:
                    h["origin_url"] = make({"native_id": h.get("book_id"),
                                            "lang": h.get("lang"),
                                            "title": h.get("title")})
                except Exception:      # noqa: BLE001 — a missing field is not a 500
                    h["origin_url"] = None
    con = db.connect()   # query_log lives in the default collection's manifest regardless
    # .get, not [...]: when every collection is withheld `run_search`
    # returns {hits, withheld} with no `timings` at all, and a KeyError
    # here answered the whole query with a 500 — the licence gate's own
    # message replaced by a crash (invariant 9's silence, again).
    db.log_query(con, "ask", body.query, body.mode, body.limit, result.get("timings") or {},
                 len(result["hits"]), query_service.source_qualified_ids(result["hits"]))
    return result


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
#: control key holding the last counts, so a restarted process can show
#: what was true instead of zeros while it re-scans (see _stored_counts)
_COUNTS_CONTROL_KEY = "document_counts"
#: A refresh may not cost more than this share of the time between
#: refreshes. THE INSTRUMENT MUST NOT COST WHAT IT MEASURES: the scan is a
#: full pass over `documents`, so its cost grows with the corpus while a
#: constant TTL does not. Measured on the live node 2026-09-11 — pubmed,
#: 40M rows: 56 s per scan against a 120 s TTL, so nearly half of every
#: interval was spent counting, on the same disk and the same SQLite page
#: cache the archive import was using. The interval now follows the
#: measured cost (pubmed: ~9 minutes), which is CLAUDE.md's rule that a
#: per-round cost scales with the work available and never with the size
#: of the corpus.
_COUNTS_MAX_DUTY = 0.1

#: manifest path -> seconds the last scan took, for the rule above
_counts_scan_secs: dict[str, float] = {}

_counts_cache: dict[str, tuple[float, dict]] = {}
_counts_lock = threading.Lock()
# Stage-rate samples (see _collection_shelf): memory-first, keyed on the
# manifest path. GIL-atomic dict ops; a rare lost update costs one sample.
_stage_rate_memo: dict[str, dict] = {}
# in-flight refresh per collection, WITH its start time: a refresh thread
# that hangs (or dies un-noticed) must not hold the guard forever, or the
# counters freeze at an ever-growing age with no refresh ever starting
_counts_refreshing: dict[str, float] = {}
# THE GENERATION a refresh was started under (see `invalidate_counts`). A
# refresh from before the latest invalidation neither blocks a new one nor
# writes what it counted — without this, a reset's own confirmation view
# waited behind a pre-reset refresh and then read its pre-reset number
# (test_counts_invalidation_race.py). Beside `_counts_refreshing`, whose
# value stays a bare start time; a guard with no generation is current.
_counts_refreshing_gen: dict[str, tuple[int, int]] = {}
_counts_gen: dict[str, int] = {}
_counts_epoch = 0
_COUNTS_REFRESH_ABANDON_S = 600.0
# a cache entry that cannot be refreshed (every attempt failing) is dropped
# once it is this stale, so the next viewer pays the cold scan and sees the
# truth instead of a frozen number of unbounded age
_COUNTS_MAX_AGE_S = 1800.0
# how long a COLD page view waits for the first scan before rendering
# "counting…" instead — small manifests answer well inside this
_COUNTS_COLD_WAIT_S = 2.0


def _archive_spans(collection) -> list[tuple[int, int]]:
    """The merged id spans the collection's archive declares — [] unless it
    is remote, known and declares coverage (genesis.declared_spans)."""
    if not getattr(collection, "genesis_archive", None):
        return []
    from .bundles.genesis import ARCHIVES, declared_spans
    return declared_spans(ARCHIVES.get(collection.genesis_archive))


def _document_counts_query(collection) -> dict:
    """THE scan. One pass for every count either page needs."""
    con = db.connect(collection)
    spans = _archive_spans(collection)
    sql, params = _document_counts_sql(spans)
    row = con.execute(sql, params).fetchone()
    counts = {k: (row[k] or 0) for k in row.keys()}
    if spans:
        # which range these two counts are over: a stored row from before
        # the declaration changed must not be read as today's range
        counts["arch_range_hi"] = spans[-1][1]
    return counts


def _counted_over_another_range(collection, counts: dict) -> bool:
    """Counts taken before the archive's declared range changed (a TOML
    edit adds, drops or moves it): the card reads them as absent, so they
    are refreshed now rather than at the TTL — pubmed's is ~9 minutes of
    "counting…" otherwise. Nothing to compare for a collection without."""
    spans = _archive_spans(collection)
    return counts.get("arch_range_hi") != (spans[-1][1] if spans else None)


def _document_counts_sql(spans: list[tuple[int, int]]) -> tuple[str, dict]:
    """THE scan's SQL and parameters. `spans` (an archive's declared id
    range, `_archive_spans`) adds four SUMs; without them the SQL is the
    base scan byte for byte."""
    # blocked vs unindexable: stage failures are prefixed with their stage
    # (worker._execute_step), so an UNPREFIXED error came from discovery and
    # describes the CORPUS — no text in the mirror, retracted upstream.
    # "Clear errors & retry" cannot fix those (the next scan re-applies
    # them), so reporting them as failures promised an impossible remedy.
    stage_error = ("(" + " OR ".join(
        f"error LIKE '{st}: %'" for st in ("rdf", "fts", "embed")) + ")")
    pending = (f"in_scope=1 AND fts_done=1 AND error IS NULL AND retries<:max "
               f"AND (embed_done=0 OR embed_version<:ver)")
    params: dict = {"max": config.MAX_RETRIES, "ver": config.EMBED_VERSION}
    archive_sums = ""
    if spans:
        from .bundles.genesis import NUMERIC_ID_SQL, in_spans_sql
        spans_sql, span_params = in_spans_sql(spans, named=True)
        params.update(span_params, span_top=spans[-1][1])
        # a non-numeric id casts to 0: inside a range from 0 unless excluded
        in_range = f"({spans_sql} AND {NUMERIC_ID_SQL})"
        # THE ARCHIVE'S DECLARED RANGE, for the pre-embedded row: documents
        # that HOLD a vector inside it, over in-scope documents inside it.
        # Vectors held, not the import ledger (SUM(genesis_imports.docs)),
        # which is pass bookkeeping and read 80,654 short on pubmed. Four
        # more SUMs on the pass this already makes — deliberately NOT a
        # walk of idx_documents_native_int, which needs a row lookup per
        # document for in_scope/embed_done (40.6 s on live pubmed, health.py).
        # Measured on 4M synthetic rows (2026-09-14): base scan 2.13 s; the
        # SUMs 2.76 s (two or four cost the same); with the numeric-id check
        # that keeps a non-numeric id out of the range, 3.23 s — +52% over
        # the base, the TTL stretching with it (_counts_ttl_for).
        #
        # arch_in_range_waiting is the gap `louise health` reports (its
        # _WAITING['embed'], past indexing), so the card can say how much of
        # its own gap health will explain; arch_past_range is what is ABOVE
        # the range — the rest of what lies outside it (a gap between files,
        # an id below the first, an id that is not a number) is not "past".
        archive_sums = f""",
          SUM(CASE WHEN in_scope=1 AND {in_range} THEN 1 ELSE 0 END) arch_in_range,
          SUM(CASE WHEN in_scope=1 AND embed_done=1 AND {in_range}
                   THEN 1 ELSE 0 END) arch_in_range_embedded,
          SUM(CASE WHEN in_scope=1 AND embed_done=0 AND rdf_done=1 AND fts_done=1
                        AND {in_range} THEN 1 ELSE 0 END) arch_in_range_waiting,
          SUM(CASE WHEN in_scope=1 AND CAST(native_id AS INTEGER) > :span_top
                        AND {NUMERIC_ID_SQL} THEN 1 ELSE 0 END) arch_past_range,
          -- Of what lies OUTSIDE the range, the documents with NO TEXT:
          -- `chunks` is written by the fts stage, so an indexed document
          -- with none has nothing for an encoder to embed. Wikipedia's
          -- redirects are that — a record of their target and no more —
          -- and the card said all 30.9 M pages past the range were the
          -- local encoder's work (2026-09-15). An UNINDEXED row is not
          -- counted: its chunk count is unknown, never zero.
          SUM(CASE WHEN in_scope=1 AND NOT {in_range} AND fts_done=1
                        AND COALESCE(chunks, 0) = 0
                   THEN 1 ELSE 0 END) arch_outside_empty,
          -- The local encoder's progress on THE REST: embedded, and not one
          -- of those. The embed stage marks a document with no chunk done
          -- without writing a vector, and the card, which derived this as
          -- embed_done minus the range's, counted 37 of pubmed's 53 empty
          -- records both as "nothing to embed" and as "done" (2026-09-18).
          SUM(CASE WHEN in_scope=1 AND NOT {in_range} AND embed_done=1
                        AND NOT (fts_done=1 AND COALESCE(chunks, 0) = 0)
                   THEN 1 ELSE 0 END) arch_outside_embedded"""
    return f"""
        SELECT COUNT(*) total, SUM(in_scope) in_scope,
          SUM(CASE WHEN in_scope=1 AND rdf_done=1 THEN 1 ELSE 0 END) rdf_done,
          SUM(CASE WHEN in_scope=1 AND fts_done=1 THEN 1 ELSE 0 END) fts_done,
          SUM(CASE WHEN in_scope=1 AND embed_done=1 THEN 1 ELSE 0 END) embed_done,
          SUM(CASE WHEN in_scope=1 AND ({stage_error} OR retries>=:max)
                   THEN 1 ELSE 0 END) blocked,
          -- PER STAGE, because "blocked" is collection-wide and says
          -- nothing about WHICH stage a document is stuck at. A document
          -- blocked at fts has already passed rdf, so counting it against
          -- rdf's remaining work makes a stage that is actively running
          -- look like it has nothing left to do. Three more SUMs on a scan
          -- that is already walking every row.
          SUM(CASE WHEN in_scope=1 AND rdf_done=0
                        AND ({stage_error} OR retries>=:max)
                   THEN 1 ELSE 0 END) blocked_rdf,
          SUM(CASE WHEN in_scope=1 AND fts_done=0
                        AND ({stage_error} OR retries>=:max)
                   THEN 1 ELSE 0 END) blocked_fts,
          SUM(CASE WHEN in_scope=1 AND embed_done=0
                        AND ({stage_error} OR retries>=:max)
                   THEN 1 ELSE 0 END) blocked_embed,
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
                        AND retries<:max THEN 1 ELSE 0 END) docs_unindexed{archive_sums}
        FROM documents""", params


def _counts_key(collection) -> str:
    """Keyed on the physical manifest path, not collection.name — the same
    lesson fts.get_index() records: two Collection instances can share a
    name while pointing at different files, and a name-keyed cache then
    serves one collection's numbers for another."""
    return str(collection.manifest_path)


def _counts_ttl_for(name: str) -> float:
    """How long this collection's counts stay fresh — never less than
    `_COUNTS_TTL_S`, and never so short that scanning costs more than
    `_COUNTS_MAX_DUTY` of the interval. A small manifest keeps the two
    minutes it always had; a 40M-row one earns a longer one by being
    expensive to count."""
    took = _counts_scan_secs.get(name)
    if not took:
        return _COUNTS_TTL_S
    return max(_COUNTS_TTL_S, took / _COUNTS_MAX_DUTY)


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
        if now - at > _counts_ttl_for(name) or _counted_over_another_range(collection, counts):
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
    # THE LAST KNOWN COUNTS, rather than nothing. `_counts_cache` lives in
    # THIS PROCESS, so every web restart empties it — and pubmed's scan
    # takes 55 s, so for the next 55 seconds its card read
    # "total 0, in_scope 0, rdf_done 0" and the collection looked EMPTY.
    # Six restarts in one afternoon made that the normal view, and it is
    # indistinguishable from a stuck pipeline, which is exactly what it
    # was reported as (operator, 2026-09-12).
    #
    # The numbers survive in the manifest's own `control` row, so a cold
    # process can say what was true a few minutes ago and label it.
    # Stale-and-labelled beats zero-and-confident.
    stored = _stored_counts(collection)
    if stored is not None:
        counts, at = stored
        return counts, max(0.0, time.time() - at)
    return None, 0.0


def _stored_counts(collection) -> tuple[dict, float] | None:
    """The last counts this collection wrote to its manifest, with when.

    Written by every successful refresh (below), so it outlives the
    process. Read-only and cheap: one indexed row, never a scan — the
    point is to answer instantly when the scan cannot.
    """
    try:
        con = db.connect(collection)
        raw = db.get_control(con, _COUNTS_CONTROL_KEY)
        if not raw:
            return None
        got = json.loads(raw)
        counts, at = got.get("counts"), float(got.get("at") or 0)
    except Exception:      # noqa: BLE001 — a missing or malformed row is
        return None        # simply no answer, never a broken page
    if not isinstance(counts, dict) or not at:
        return None
    return {k: int(v or 0) for k, v in counts.items()}, at


def _persist_counts(collection, counts: dict) -> bool:
    """Write the counts to the manifest so the NEXT process can show them.

    BEGIN IMMEDIATE, and that is the whole point (CLAUDE.md): `db.connect`
    READS — schema checks, pragmas — before this writes, and under WAL a
    deferred transaction that has read fails its upgrade INSTANTLY if
    anyone committed meanwhile. The 30 s busy timeout never runs, so the
    write lost every race against a worker mid-batch. Taking the write
    lock up front is what makes the timeout mean anything.

    And it is logged, not swallowed. "Persistence is a courtesy" was true
    of any single write and false of all of them: a write that fails
    EVERY time restores the empty-collection-after-restart bug this
    exists to fix, silently and permanently. One line in `docker logs`
    is the difference between a bug and a mystery.
    """
    payload = json.dumps({"at": time.time(), "counts": counts})
    try:
        con = db.connect(collection)
        try:
            con.execute("BEGIN IMMEDIATE")
            db.set_control(con, _COUNTS_CONTROL_KEY, payload)
            con.commit()
        finally:
            con.close()
        return True
    except Exception:   # noqa: BLE001 — never this refresh's job to fail
        print(f"counts could not be persisted for {_counts_key(collection)} "
              f"(the next cold process will re-count):\n"
              f"{traceback.format_exc()[:800]}", flush=True)
        return False


def _refresh_counts_async(collection) -> None:
    """At most one refresh in flight per collection — without the guard, a
    page that is slow to load invites a reload, and every reload would start
    another 12-second scan of the same table. The guard expires
    (_COUNTS_REFRESH_ABANDON_S): a refresh thread that hangs must not stop
    every future refresh, which would freeze the counters for good."""
    name = _counts_key(collection)
    now = time.time()
    with _counts_lock:
        gen = (_counts_epoch, _counts_gen.get(name, 0))
        started = _counts_refreshing.get(name)
        if (started is not None and now - started < _COUNTS_REFRESH_ABANDON_S
                and _counts_refreshing_gen.get(name, gen) == gen):
            return
        _counts_refreshing[name] = now
        _counts_refreshing_gen[name] = gen

    def _run():
        try:
            t0 = time.time()
            counts = _document_counts_query(collection)
            took = time.time() - t0
            with _counts_lock:
                # counted under an older generation: an action changed the
                # counts meanwhile, and a newer refresh owns the answer
                current = (_counts_epoch, _counts_gen.get(name, 0)) == gen
                if current:
                    _counts_scan_secs[name] = took
                    _counts_cache[name] = (time.time(), counts)
            # …and to the manifest, so the NEXT process does not have to
            # show an empty collection while it re-counts (see
            # `_stored_counts`). A telemetry write from render code, so it
            # goes through _sample_write and never blocks a stage.
            # `db.set_control`, NOT `_sample_write`: that one SAMPLES, and
            # a dropped sample here means the next cold process shows an
            # empty collection again. This write happens at most once per
            # TTL — eighteen minutes for pubmed — so there is nothing to
            # sample away. Caught by its own test.
            if current:
                _persist_counts(collection, counts)
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
                # a stale refresh must not clear the guard a newer one holds
                if _counts_refreshing_gen.get(name, gen) == gen:
                    _counts_refreshing.pop(name, None)
                    _counts_refreshing_gen.pop(name, None)

    threading.Thread(target=_run, name=f"counts:{name}", daemon=True).start()


def invalidate_counts(collection=None) -> None:
    """Drop cached counts after an action that changes them (reset, clear
    errors, rescan), so the operator sees the effect of what they just did
    instead of a stale number. No argument clears everything.

    And bumps the generation, so a refresh already in flight — counted
    before the action — is discarded when it lands instead of standing in
    for the fresh count the view straight after the action starts."""
    global _counts_epoch
    with _counts_lock:
        if collection is None:
            _counts_cache.clear()
            _counts_epoch += 1
        else:
            key = _counts_key(collection)
            _counts_cache.pop(key, None)
            _counts_gen[key] = _counts_gen.get(key, 0) + 1


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


def _copy_state(collection) -> dict:
    """The copy's state on this peer — refused, quarantine or in use (03
    §3.6) — from THE derivation (`app/copy_state.py`), the one the worker and
    the search read. `enforced` says whether the node withholds a copy in
    quarantine or only reports it (the first of step 14's two deploys). The
    Library never raises for it."""
    from . import copy_state
    try:
        state = copy_state.of(collection)
        return {"state": state.state, "reasons": list(state.reasons),
                "origin_check": state.origin_check, "enforced": copy_state.enforced(),
                "vector_leg": state.vector_leg, "vector_leg_reason": state.vector_leg_reason}
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "reasons": [f"{type(e).__name__}: {e}"],
                "origin_check": "pending", "enforced": False,
                "vector_leg": True, "vector_leg_reason": ""}


def _collection_shelf(collection: collections_mod.Collection,
                       measured: dict | None = None,
                       worker_state: str = "running") -> dict:
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
    #: Why the ETA slot says what it says, in words, for the tooltip. The
    #: template used to build this itself and could only guess: it rendered
    #: `title="+/h"` — a literal `+` and `/h` around nothing — on every row
    #: whose rate was unknown, which is exactly the rows an operator asks
    #: about.
    stage_eta_why = {k: None for k in _RATE_FIELDS}
    #: THE TARGET EACH STAGE IS MEASURED AGAINST, computed once here so the
    #: bar, the "complete" colour and the ETA cannot disagree about what
    #: done means. They did: the template derived `indexable = in_scope -
    #: unindexable` for rdf and fts and used raw `in_scope` for the dense
    #: row, while this function computed `remaining` from `in_scope` for
    #: all three. On gutenberg — 79,320 in scope, 33 unindexable — that
    #: made the dense row's target 33 documents no stage will ever finish,
    #: so its ETA slot could never go quiet. "A bar that cannot reach its
    #: end is not a progress bar" is the note already above the rdf row;
    #: this is that note applied to the one row that was missing it.
    #: Always present, zeros while the counts are still being scanned, so
    #: the template can read it unconditionally instead of keeping its own
    #: copy of the arithmetic — which is how the two came to disagree.
    stage_targets: dict[str, int] = {k: 0 for k in _RATE_FIELDS}
    if not counts_pending:
        now_i = int(time.time())
        cur = {
            "rdf": counts["rdf_done"] or 0,
            "fts": counts["fts_done"] or 0,
            "embed": counts["embed_done"] or 0,
            # FILES, always — the unit that moves when bytes move, and the
            # one `fetched["complete"]` is judged on, so the rate, the ETA,
            # the stall and the green label finally agree about one thing.
            #
            # This counted DOCUMENTS for a one-dir-per-doc layout, and a
            # document is a DIRECTORY: a pass that fills missing files into
            # directories already present cannot move that counter at all.
            # Live on gutenberg — 4,696 files and 1 GB landed into existing
            # book directories — the card reported "no progress in the last
            # 30 minutes" on a fetch running at full speed, which is worse
            # than a dash: it asserts a stall that is not happening.
            "fetch": ((fetched or {}).get("done") or 0),
        }
        indexable = max(0, (counts["in_scope"] or 0) - unindexable)
        stage_targets = {
            "rdf": indexable, "fts": indexable, "embed": indexable,
            # `remote_total` is the file count `complete` compares against:
            # max(the origin's recorded listing, the ledger). Same unit as
            # `cur["fetch"]` above, by construction.
            "fetch": ((fetched.get("remote_total") or 0) if fetched else 0),
        }
        remaining = {k: max(0, stage_targets[k] - cur[k]) for k in _RATE_FIELDS}
        # WHAT A STAGE CAN STILL TAKE. A blocked document waits for a human
        # ("clear errors & retry"), so while the only thing left is blocked
        # the stage is as finished as it can be on its own — and a dash
        # that never resolves is the noise this change is removing. Unlike
        # `unindexable` it is NOT taken out of the target: retrying can fix
        # it, so the bar legitimately stops short and the badge says why.
        #
        # PER STAGE. This subtracted the COLLECTION-WIDE `blocked` from all
        # three document stages, and `blocked` counts a document stuck at
        # ANY stage. A document blocked at fts has already passed rdf — so
        # two of them were enough to make rdf's slot go silent while the
        # worker was actively parsing two other documents. Counted against
        # the stage it is actually stuck at, which is what `blocked_<stage>`
        # is for.
        workable = {k: max(0, remaining[k]
                           - int(counts.get("blocked_" + k) or 0))
                    for k in _RATE_FIELDS}
        # THE FETCH ROW IS JUDGED THE WAY ITS OWN LABEL IS. Its counter is
        # in DOCUMENTS (`docs_done`) and its target came from the origin
        # listing's `docs_total`, which counts directories the origin
        # publishes including the ones holding nothing readable — so the
        # counter tops out below the target and the slot never went quiet.
        # Live on gutenberg: 79,287 against 79,389, an eternal dash on a
        # finished fetch. `fetched["complete"]` is the same test the row's
        # green label already uses — no pass running and every listed file
        # landed — so the two now agree by construction.
        if fetched and fetched.get("complete"):
            workable["fetch"] = 0
        # A STAGE THE NODE HAS BEEN TOLD NOT TO RUN HAS NOTHING TO SAY. With
        # embedding off, the dense row reported "no progress in the last 30
        # minutes" — true, and read as a fault on a collection doing exactly
        # what it was configured to do. The row's own label already carries
        # the mode; the estimate slot has no business guessing at a stage
        # nobody asked to run. "Work it can take on its own" is the
        # invariant's wording and this is part of what it means.
        if db.get_scope(con)["embed_mode"] == "off":
            workable["embed"] = 0
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
        if rec_sr is None or any(k not in rec_sr for k in _RATE_FIELDS):
            # first sight or a pre-upgrade record: fresh baseline
            baseline = {**cur, "ts": now_i}
            _stage_rate_memo[memo_key] = baseline
            _sample_write(con, "stage_rate", json.dumps(baseline))
            for k in _RATE_FIELDS:
                if workable[k] > 0:
                    stage_etas[k] = ETA_UNKNOWN  # work remains, no history yet
                    stage_eta_why[k] = "measuring — no rate yet"
        else:
            # A COUNT THAT SHRANK RE-BASELINES ITS OWN STAGE, not all four.
            # This used to be `any(cur[k] < rec_sr[k] ...)` over every
            # field, so a single dip — a reset, a re-mint, a corpus whose
            # fetch total was revised down — discarded the accumulated
            # rate and sample count of the other three stages as well, and
            # every one of them went back to a placeholder for the next
            # two windows. Each stage's history is its own.
            for k in _RATE_FIELDS:
                if cur[k] < int(rec_sr.get(k) or 0):
                    # `_stalled` clears with the rest of it: a stage whose
                    # history was just thrown away has not been observed
                    # standing still, and a sticky flag made it claim so.
                    rec_sr = {**rec_sr, k: cur[k], k + "_ph": None,
                              k + "_n": 0, k + "_stalled": False,
                              k + "_pts": now_i, k + "_pc": cur[k]}
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
            stalled = {k: bool(rec_sr.get(k + "_stalled")) for k in _RATE_FIELDS}
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
                        # A STAGE SLOWER THAN ONE DOCUMENT AN HOUR still has
                        # a rate. `round(ema)` gave 0 for anything under
                        # 0.5/h, and 0 is falsy — so the slot said
                        # "measuring — no rate yet" forever on a stage that
                        # was demonstrably moving, and could never reach the
                        # stall reason either (that needs a prior window).
                        stage_rates[k] = round(ema) if ema >= 1 else round(ema, 2)
                        samples[k] += 1
                        stalled[k] = False
                        p_ts, p_count = now_i, cur[k]
                    elif span >= _STALL_S:
                        stage_rates[k] = None       # stalled: no number to stand behind
                        samples[k] = 0
                        stalled[k] = True
                    progress_ts[k], progress_count[k] = p_ts, p_count
                updated = {**cur, "ts": now_i,
                           **{k + "_ph": stage_rates[k] for k in _RATE_FIELDS},
                           **{k + "_n": samples[k] for k in _RATE_FIELDS},
                           # PERSISTED, because the stall has to outlive the
                           # render that noticed it. The reason used to be
                           # inferred from the PREVIOUS record's sample count
                           # being non-zero — true on the detecting render and
                           # false on every one after it, since the stall
                           # itself writes that count to zero. So a stage dead
                           # for hours said "measuring — no rate yet".
                           **{k + "_stalled": stalled[k] for k in _RATE_FIELDS},
                           **{k + "_pts": progress_ts[k] for k in _RATE_FIELDS},
                           **{k + "_pc": progress_count[k] for k in _RATE_FIELDS}}
                _stage_rate_memo[memo_key] = updated
                _sample_write(con, "stage_rate", json.dumps(updated))
            for k in _RATE_FIELDS:
                if workable[k] <= 0:
                    continue
                if stage_rates[k] and samples[k] >= _MIN_WINDOWS:
                    stage_etas[k] = _human_eta(remaining[k] * 3600.0 / stage_rates[k])
                    stage_eta_why[k] = f"+{stage_rates[k]:,}/h".replace(",", ".")
                else:
                    # work remains and the rate is not yet trustworthy:
                    # say so, never a number that will be revised 10x.
                    # WHICH of the reasons it is goes in the tooltip — the
                    # one token used to cover all of them.
                    stage_etas[k] = ETA_UNKNOWN
                    stage_eta_why[k] = (
                        "no progress in the last 30 minutes"
                        if stalled[k]
                        else "measuring — one window so far, waiting for a second"
                        if stage_rates[k]
                        else "measuring — no rate yet")
        # rdf is not stalled when it is simply AHEAD: the worker gives the
        # thread to fts whenever a full batch is ready (_next_book), so a
        # stage whose output already outruns its consumer by a batch or
        # more waits by design — pubmed sat at rdf 12.1M / fts 7.9M and the
        # card said "TBD", which reads as "unknown". Say what it is.
        #
        # IN WORDS, and only when it is TRUE. "after fts (607 ahead)" named
        # an internal stage, an unstated unit and an arithmetic the reader
        # has no way to check (operator, 2026-09-12: "means nothing to the
        # user"), and it was the one count in the whole interface formatted
        # with comma grouping — the separator `num` exists to avoid,
        # because an operator once read "+377,217/h" as 377 per hour.
        #
        # Three conditions, where there used to be one. It no longer claims
        # a cause while the WORKER IS STOPPED — nothing is waiting for
        # indexing then, it is waiting for the operator — and it no longer
        # overwrites a real ETA: the assignment sat after the branch that
        # computes one, so a measured rdf rate was discarded in favour of
        # this sentence.
        ahead = cur["rdf"] - cur["fts"]
        if (workable["rdf"] > 0 and ahead >= config.FTS_BATCH_DOCS
                and stage_etas["rdf"] in (None, ETA_UNKNOWN)
                and worker_state == "running"):
            stage_etas["rdf"] = "waiting for the index"
            stage_eta_why["rdf"] = (
                f"parsing is {ahead:,}".replace(",", ".")
                + " documents ahead of the index; the worker gives the thread "
                  "to indexing while a full batch is ready, and parsing "
                  "resumes when indexing catches up")

    return {
        "embed_mode": db.get_scope(con)["embed_mode"],
        "name": collection.name, "plugin": collection.plugin,
        "plugin_label": _plugin_label(collection.plugin),
        "stage_rates": stage_rates,
        "stage_etas": stage_etas,
        # the tooltip for the slot above, in words — built HERE because
        # only this function knows which of the reasons applies
        "stage_eta_why": stage_eta_why,
        # what each stage counts up to: one answer for the bar, the colour
        # and the ETA, so they cannot disagree about what done means
        "stage_targets": stage_targets,
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
        "copy_state": _copy_state(collection),
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
    # STAND-ALONE: a collection whose card IS the archive (wikivec, the
    # `embedded` plugin). Every other collection with pre-embeddings —
    # pubmed and MedCPT, wikipedia and Upstash — shows them in the same
    # sub-box with the same rows (operator, 2026-09-14).
    standalone = bool(archive and archive.local and collection.plugin == "embedded")
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
    if standalone:
        if fetched and fetched.get("complete"):
            fetch_state = "done"
        elif fetched and fetched.get("active"):
            fetch_state = "active"
        else:
            fetch_state = "idle"
    elif archive is not None and archive.local:
        # pre-embeddings whose files are all on disk: every one counts as
        # fetched, the way a remote archive's landed files do
        fetch_state = "done" if shard_total else "idle"
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
    # THE ARCHIVE'S DECLARED RANGE (the pre-embedded row's numbers), from
    # THE scan's two range SUMs — never a scan here. None whenever they are
    # absent or were counted over another range (`arch_range_hi`): the
    # first cold process after a deploy reads a stored row without them,
    # and the card says "counting…" rather than the ledger or a zero. The
    # rest follows by subtraction. "Hold" is the honest verb: under "all"
    # the local encoder may have filled documents inside the range, and no
    # column says per document where a vector came from.
    from .bundles.genesis import declared_spans
    spans = declared_spans(archive)
    range_hi = spans[-1][1] if spans else None
    sums = ("arch_in_range", "arch_in_range_embedded", "arch_in_range_waiting",
            "arch_past_range", "arch_outside_empty", "arch_outside_embedded")
    ranged = (counts is not None and range_hi is not None
              and counts.get("arch_range_hi") == range_hi
              and all(k in counts for k in sums))
    in_range, in_range_embedded, waiting, past, outside_empty, outside_embedded = (
        (int(counts[k] or 0) for k in sums) if ranged else (None,) * 6)
    missing = max(0, in_range - in_range_embedded) if ranged else None
    return {
        "archive": collection.genesis_archive,
        "known": archive is not None,
        "local": bool(archive and archive.local),
        "standalone": standalone,
        "shard_total": shard_total,
        "applied": applied,
        "final": totals["final"] or 0,
        "fetched": (shard_total if archive is not None and archive.local and not standalone
                    else totals["fetched"] or 0),
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
        "spans": spans,
        "range_hi": range_hi,
        "in_range": in_range,
        "in_range_embedded": in_range_embedded,
        # "without a vector", never "to import": inside the range does not
        # prove the archive holds one (health.archive_gap says which)
        "in_range_missing": missing,
        # the part of that gap past indexing — health's number — and the
        # part still being indexed, which health does not count
        "in_range_waiting": min(waiting, missing) if ranged else None,
        "in_range_indexing": max(0, missing - waiting) if ranged else None,
        # above the range's end ("past id N"), and everything outside it
        "past_range": past,
        "past_note": archive.past_range if archive is not None else "",
        "outside_range": (max(0, int(counts.get("in_scope") or 0) - in_range)
                          if ranged else None),
        # of those, the ones an encoder would find nothing to embed in:
        # indexed and holding no chunk (a wikipedia redirect is a record of
        # its target and no more). Never a document still to be indexed.
        "outside_empty": outside_empty,
        # the local encoder's progress outside the archive
        "outside_embedded": outside_embedded,
    }


def _license_state(collection: collections_mod.Collection) -> dict:
    """One resolution for every plugin (app/licensing.py): the class and the
    terms come from the spec, and the ack must match them."""
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
        # the badge's ✓: a known class and nothing gating use — green needs
        # no acknowledgement, yellow/red have a valid one. It used to be
        # `ack_recorded` alone, so only the one yellow collection showed a
        # ✓ and every green one looked unchecked (operator, 2026-09-15).
        "ok": bool(cls in ("green", "yellow", "red") and state != licensing.MISSING),
    }


def _refuse_withheld(collection, use: str) -> None:
    """Invariant 9 at the web's doors, from THE resolution
    (`licensing.require_ack`): a 403 that names what is missing and how to
    record it — never a silent success. The worker, the CLI and `hold` all
    asked; the Library's rescan and bundle-export buttons did not
    (verification pass, 2026-09-13)."""
    from . import licensing
    try:
        licensing.require_ack(collection, use=use)
    except LicenseAckRequiredError as e:
        raise HTTPException(403, str(e)) from None


@admin_router.get("/library", response_class=HTMLResponse)
def library_page(request: Request):
    collections = collections_mod.load_all()
    measured = _worker_stats()
    # PASSED IN, not re-read per collection: it is one node-wide state (in
    # state/node.sqlite, app/node_state.py), and the shelf needs it to tell
    # "waiting for the index" (true only while the worker runs) from
    # "waiting for the operator".
    worker_state = node_state.worker_state()
    shelves = [_collection_shelf(c, measured, worker_state) for c in collections]
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
        "worker_state": worker_state,
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
    slices = collection.slices
    if collection.codex is not None:
        # A Codex collection's file holds SLICES (07 §7.8), and today a node
        # holds "all" of one or none: part of one is `hold records fetch
        # --slice` (07 §7.3), not built yet. So growing means everything.
        everything = acquire_spec.everything_profile(spec)
        if body.profile != everything:
            raise HTTPException(
                409, f"{name} is held by its Codex: its file names slices "
                     f"(07 §7.8), and a node holds all of them ({everything!r}) "
                     f"or none until `hold records fetch --slice` exists")
        slices = "all"
    toml_path = collections_mod.config_toml_path(name)
    if not toml_path.exists():
        raise HTTPException(400, f"{name!r} has no TOML file (synthesized "
                                  f"default) — declare it before growing")
    _write_collection_toml(
        toml_path, codex=_codex_id(collection),
        plugin=collection.plugin, profile=body.profile,
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
        embed_mode=collection.embed_mode_default, layers=collection.layers,
        snapshot=collection.snapshot, extras=extras,
        peer_source=collection.peer_source, title=collection.title,
        slices=slices, kinds=collection.kinds, share=collection.share,
        reranker=collection.reranker)
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


#: manifest paths with a background "clear errors" running (library_reset)
_clearing: set[str] = set()
_clearing_lock = threading.Lock()


def _clear_errors_job(collection, key: str) -> None:
    try:
        con = db.connect(collection)
        try:
            n = db.apply_reset(con, "errors", [])
            db.log_event(con, "info", "control",
                         f"reset errors (all): {n} document(s) cleared")
        finally:
            con.close()
        invalidate_counts(collection)
    except Exception as e:  # noqa: BLE001 — a background job must not fail silently
        print(f"clear errors on {collection.name} failed: {type(e).__name__}: {e}",
              flush=True)
    finally:
        with _clearing_lock:
            _clearing.discard(key)


@admin_router.post("/library/{name}/reset")
def library_reset(name: str, body: LibraryResetBody):
    collection = _collection_or_404(name)
    if body.stage == "errors" and not body.book_ids:
        # IN THE BACKGROUND. No index answers "which documents hold a stage
        # error", so finding them reads the whole manifest — about 75 s on
        # pubmed's 40M rows (2026-09-15). The page returns at once and the
        # counts are refreshed when the clear is done; a second press while
        # one runs is the same clear, not another.
        key = str(collection.manifest_path)
        with _clearing_lock:
            running = key in _clearing
            _clearing.add(key)
        if not running:
            threading.Thread(target=_clear_errors_job, args=(collection, key),
                             name=f"clear-errors-{collection.name}", daemon=True).start()
        return {"ok": True, "background": True, "already_running": running}
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
    # invariant 9, "not discovered": the worker's scan asks; this button did not
    _refuse_withheld(collection, "its corpus is discovered")
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
    corpus_dir: str = ""      # a folder under CORPUS_MOUNTS_DIR


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


def _codex_id(collection) -> str | None:
    """The `collection_id` a rewrite must carry through, or None for a
    collection that predates the migration. One helper so no call site
    has to remember the `.codex and .codex.collection_id` dance."""
    cx = getattr(collection, "codex", None)
    return None if cx is None else cx.collection_id


def _toml_scalar(v) -> str:
    """A layer param as TOML. Ints and floats bare, bools lowercase,
    everything else quoted — the set `Chunker.params` actually holds."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return '"%s"' % str(v).replace('"', '\\"')


#: 07 §7.8 spells the middle mode `on-demand`; the stored value is `lazy`
#: (app/collections.py's `_EMBED_MODE_ALIASES`). A Codex file is written in
#: the DOCUMENT's word — CLAUDE.md: the documents fix the vocabulary.
_MODE_ON_DISK = {"lazy": "on-demand"}


def _write_codex_toml(path: Path, *, codex: str, profile: str | None,
                       acquire_enabled: bool, corpus_dir: Path,
                       genesis_archive: str | None, encoder,
                       embed_mode: str | None, snapshot: str | None,
                       extras: tuple[str, ...], title: str | None = None,
                       slices=None, kinds=None, share: bool = False,
                       reranker=None) -> None:
    """07 §7.8's file: `codex`, `title`, `slices`, `kinds`, `embed`,
    `share`, `[encoder]` naming where this node reaches the encoder, and
    `[reranker]` where it reaches the collection's reranker (05 §5.6).

    What is NOT written is the point. `plugin`, the chunker `[[layers]]`
    and the `[paths]` tree are the CODEX's answers — all three are inside
    `collection_id` or derived from `<LOUISE_ROOT>/collections/<name>/` —
    and a file that repeated them could disagree with them; so are the
    space's `dtype`, `ctx` and `dim`, which `[encoder]` no longer states,
    and its `query.instruction` — which a file that stated it would have
    refused on the next load (collections.parse_recipe).
    07 §7.8: "A file's `codex` is never edited: a name holds one collection
    for its life, and another collection is another name." So this function
    carries the id through every rewrite and never invents one.

    The keys a Codex file carried before §7.8 spelled the file out —
    `profile`, `acquire_enabled`, `genesis_archive`, `snapshot`,
    `corpus_dir`, `extras` — are written only where loading the file back
    without them would give a different value (`collections.codex_defaults`
    is the one derivation both sides use), so a rewrite never changes what
    a collection does.

    `license_ack` is absent for the same reason as `plugin`: a Codex
    collection's acknowledgement is `state/ack.json` (09 §9.13), read by
    `collections._codex_ack`, not a line in this file.
    """
    name = path.stem
    cx = collections_mod.codex_at(name)
    kinds = tuple(kinds or collections_mod.DEFAULT_KINDS)
    if slices is None and profile and \
            profile == collections_mod.codex_defaults(cx, name, None, kinds)["everything"]:
        slices = "all"
    derived = collections_mod.codex_defaults(cx, name, slices, kinds)
    lines = [f'codex = "{codex}"']
    if title:
        lines.append(f"title = {_toml_scalar(title)}")
    if slices == "all":
        lines.append('slices = "all"')
    elif slices is not None:
        lines.append("slices = [%s]" % ", ".join(_toml_scalar(s) for s in slices))
    elif profile:
        # a profile that selects PART of the corpus has no §7.8 spelling
        # until `hold records fetch --slice` exists: kept, not translated
        lines.append(f"profile = {_toml_scalar(profile)}")
    lines.append("kinds = [%s]" % ", ".join(_toml_scalar(k) for k in kinds))
    if embed_mode:
        lines.append(f'embed = "{_MODE_ON_DISK.get(embed_mode, embed_mode)}"')
    lines.append(f"share = {'true' if share else 'false'}")
    kept = []
    if bool(acquire_enabled) != derived["acquire_enabled"]:
        kept.append(f"acquire_enabled = {'true' if acquire_enabled else 'false'}")
    if (genesis_archive or None) != derived["genesis_archive"]:
        kept.append(f"genesis_archive = {_toml_scalar(genesis_archive or '')}")
    if snapshot and snapshot != derived["snapshot"]:
        kept.append(f"snapshot = {_toml_scalar(snapshot)}")
    if Path(corpus_dir) != derived["corpus_dir"]:
        kept.append(f"corpus_dir = {_toml_scalar(Path(corpus_dir).as_posix())}")
    if extras:
        kept.append("extras = [%s]" % ", ".join(_toml_scalar(e) for e in extras))
    if kept:
        lines += ["", "# Not keys of 07 §7.8's file: each is kept because the Codex and",
                  "# <LOUISE_ROOT>/collections/<name>/ do not give the same answer.",
                  *kept]
    if encoder is not None:
        # `[encoder]`, not `[embed]`: on a Codex file the bare `embed` key
        # above is the MODE (07 §7.8), so the recipe cannot share its name.
        lines += ["", "[encoder]", f"url = {_toml_scalar(encoder.url)}"]
        if encoder.query_url:
            lines.append(f"query_url = {_toml_scalar(encoder.query_url)}")
        # No query instruction: it is the Codex's (`space.query.instruction`),
        # and a file that states one is refused (collections.parse_recipe).
        if encoder.api != "openai":
            lines.append(f"api = {_toml_scalar(encoder.api)}")
        if encoder.model and encoder.model != cx.space.encoder.label:
            lines.append(f"model = {_toml_scalar(encoder.model)}")
        if getattr(encoder, "concurrency", 1) > 1:
            lines.append(f"concurrency = {encoder.concurrency}")
        if getattr(encoder, "device", None):
            lines.append(f"device = {_toml_scalar(encoder.device)}")
    lines += _reranker_lines(reranker)
    lines.append("")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def _reranker_lines(reranker) -> list[str]:
    """A collection's `[reranker]` (05 §5.6), carried through every rewrite
    like `[encoder]`: node-local and query-time, never the Codex's. The
    writers used to drop it. On 2026-09-15 pubmed's file was rewritten when
    its embedding was switched back on, came out without the table, and its
    searches went unreranked with nothing saying so. The round-trip guard
    passed throughout: its fixtures declared no reranker, so None == None."""
    if reranker is None:
        return []
    lines = ["", "[reranker]", f"url = {_toml_scalar(reranker.url)}",
             f"api = {_toml_scalar(reranker.api)}"]
    if reranker.model:
        lines.append(f"model = {_toml_scalar(reranker.model)}")
    return lines


def _write_collection_toml(path: Path, *, codex: str | None,
                            plugin: str, profile: str | None,
                            acquire_enabled: bool, corpus_dir: Path,
                            manifest: Path, fts: Path, lancedb: Path,
                            license_ack: str | None = None,
                            genesis_archive: str | None = None,
                            embed=None, embed_mode: str | None = None,
                            snapshot: str | None = None,
                            extras: tuple[str, ...] = (),
                            peer_source: str | None = None,
                            layers=None, title: str | None = None,
                            slices=None, kinds=None, share: bool = False,
                            reranker=None) -> None:
    """Rewrite a collection's Tier-2 file. `codex` has NO DEFAULT on
    purpose: a call site that forgets it must fail loudly here, because
    the failure it would otherwise cause is silent and unrepairable.

    Observed live 2026-09-11 on the only peer in existence. gutenberg had
    been migrated to a Codex; the operator changed its embed mode in the
    Library; this writer — which had never heard of a Codex — re-emitted
    the file in the legacy shape. That dropped nothing visible and did two
    invisible things: it un-migrated the collection back to the legacy
    load path, and it materialised the plugin's implicit layer as an
    explicit `[[layers]] chunker = "fixed"` with no `params`, which
    `chunk_fixed` refuses. 10,212 documents errored at the embed stage and
    left every selection. Both halves stayed internally consistent, so
    nothing detected it (invariant 1's failure mode); only the chunker's
    refusal to guess a window made it visible at all.
    """
    if codex:
        _write_codex_toml(
            path, codex=codex, profile=profile,
            acquire_enabled=acquire_enabled, corpus_dir=corpus_dir,
            genesis_archive=genesis_archive, encoder=embed,
            embed_mode=embed_mode, snapshot=snapshot, extras=extras,
            title=title, slices=slices, kinds=kinds, share=share,
            reranker=reranker)
        return
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
    if layers:
        # CHUNKER LAYERS (amendment 41). These are part of space_id
        # (invariant 1 as amended), so dropping them on a rewrite does not
        # merely lose a setting — it silently redefines the collection's
        # SPACE, and the next boot either refuses or, worse, accepts a
        # collection whose stored vectors belong to a different function.
        # `version` is deliberately not written: parse_layers derives it
        # from the chunker, so writing it back would let a stale number
        # outlive a chunker bump.
        for layer in layers:
            lines += ["", "[[layers]]", f'chunker = "{layer.chunker}"',
                      f'rule = "{layer.rule}"']
            # `params` ARE the chunker's window and are inside space_id
            # exactly as the chunker id is (09 §9.3). Omitting them was the
            # same bug as omitting the layer, one level down: `fixed`
            # refuses a params-less layer outright, and a chunker that
            # defaulted instead would have cut a whole collection by a
            # window its own id does not describe.
            if layer.params:
                lines.append("params = { %s }" % ", ".join(
                    f"{k} = {_toml_scalar(v)}"
                    for k, v in sorted(layer.params.items())))
    lines += _reranker_lines(reranker)
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
    # packs under PACK_MOUNTS_DIR are no longer offered: the `packed` plugin
    # that read them is boxed in attic/ (2026-09-14)
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
    # THE ENCODER, an [encoder]-shaped table ({url, api, model, dtype, ctx}):
    # a collection names its own and there is no default one (operator,
    # 2026-09-18) — this route used to take the plugin default or EMBED_URL
    encoder: dict | None = None
    companion_encoder: dict | None = None


def _requested_encoder(raw, what: str):
    """The request's own encoder table, or a 400 saying it must name one."""
    try:
        recipe = collections_mod.parse_recipe(raw, where=f"the {what}'s encoder")
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if recipe is None:
        raise HTTPException(400, f"the {what}: {collections_mod.NO_ENCODER}")
    return recipe


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
    if spec is None:
        return None
    # NOT keyed on `acquire_enabled`. Invariant 9 requires the ack before the
    # data is USED, "however the data arrived", and names hand-populated
    # folders as explicitly not a bypass — so a yellow source declared with
    # acquisition off still needs one. Keying this on acquisition meant
    # `acquire_source = "off"` created an unacknowledged yellow collection
    # and the gate never ran.
    if spec.license_class.value not in ("yellow", "red"):
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
    """DEPRECATED (07 §7.6): the web interface's new collection is
    `POST /api/hold/collection/create` — `hold collection create --recipe
    FILE`, which writes a Codex and 07 §7.8's file. The Library no longer
    calls this; it stays for one release, as the migration keeps every old
    name, and it writes a file that names no Codex.

    Writes the collection's Tier-2 TOML — the same file an operator would
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
                codex=_codex_id(existing),
                plugin=existing.plugin, profile=existing.profile,
                acquire_enabled=existing.acquire_enabled,
                corpus_dir=existing.corpus_dir,
                manifest=existing.manifest_path, fts=existing.fts_dir,
                lancedb=existing.lance_dir, license_ack=existing.license_ack,
                genesis_archive=existing.genesis_archive,
                embed=collections_mod.recipe_for(existing),
                embed_mode=existing.embed_mode_default,
                layers=existing.layers,
                snapshot=existing.snapshot, extras=existing.extras,
                peer_source=existing.peer_source, title=existing.title,
                slices=existing.slices, kinds=existing.kinds,
                share=existing.share, reranker=existing.reranker)

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
        # its encoder too, BEFORE either file is written: no half a creation
        companion = (companion_name, companion_dir, companion_ack,
                     _requested_encoder(body.companion_encoder, "companion"))

    # default ON: a plugin with a known published-embeddings archive gets it
    # declared, so the worker reuses pre-embedded data before spending GPU.
    # Operators opt out by deleting the line from the TOML.
    from .bundles.genesis import DEFAULT_ARCHIVE_BY_PLUGIN
    genesis_archive = DEFAULT_ARCHIVE_BY_PLUGIN.get(body.plugin)

    # the last check before anything is written: its own encoder
    embed = _requested_encoder(body.encoder, "collection")
    base = config.DATA_DIR / body.name
    _write_collection_toml(
        toml_path, codex=None,
        plugin=body.plugin, profile=body.profile,
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
        embed=embed,
        extras=tuple(body.extras),
        peer_source=(body.peer_source.strip() or None))

    companion_note = ""
    if companion is not None:
        companion_name, companion_dir, companion_ack, companion_embed = companion
        companion_base = config.DATA_DIR / companion_name
        _write_collection_toml(
            collections_mod.config_toml_path(companion_name),
            codex=None,
            plugin="embedded", profile="all", acquire_enabled=True,
            corpus_dir=companion_dir,
            manifest=companion_base / "manifest.db",
            fts=companion_base / "fts", lancedb=companion_base / "lancedb",
            license_ack=companion_ack,
            genesis_archive=DEFAULT_ARCHIVE_BY_PLUGIN.get("embedded"),
            embed=companion_embed,
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
                codex=_codex_id(existing),
                plugin=existing.plugin, profile=existing.profile,
                acquire_enabled=existing.acquire_enabled,
                corpus_dir=existing.corpus_dir,
                manifest=existing.manifest_path, fts=existing.fts_dir,
                lancedb=existing.lance_dir, license_ack=existing.license_ack,
                genesis_archive=existing.genesis_archive,
                embed=collections_mod.recipe_for(existing),
                embed_mode=existing.embed_mode_default,
                layers=existing.layers,
                snapshot=existing.snapshot, extras=existing.extras,
                peer_source=existing.peer_source, title=existing.title,
                slices=existing.slices, kinds=existing.kinds,
                share=existing.share, reranker=existing.reranker)
    _write_collection_toml(
        toml_path, codex=_codex_id(collection),
        plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=collection.acquire_enabled,
        corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=collection.license_ack,
        genesis_archive=collection.genesis_archive,
        embed=collections_mod.recipe_for(collection),
        embed_mode=body.mode, layers=collection.layers,
        snapshot=collection.snapshot,
        extras=collection.extras, peer_source=collection.peer_source,
        title=collection.title, slices=collection.slices,
        kinds=collection.kinds, share=collection.share,
        reranker=collection.reranker)
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
    if getattr(collection, "codex", None) is not None:
        # A Codex collection's acknowledgement is state/ack.json, recorded
        # by `louise hold collection ack` after it checks the terms against
        # the Codex's pin (09 §9.13). This wrote `license_ack` into a TOML
        # the Codex loader never reads and answered "ok" — the gate stayed
        # shut while the page said it was open (verification pass).
        raise HTTPException(
            400, f"{name} is run from its Codex: record the acknowledgement with "
                 f"`louise hold collection ack {name}` — it checks the terms "
                 f"against the Codex's pin first")
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
        toml_path, codex=_codex_id(collection),
        plugin=collection.plugin, profile=collection.profile,
        acquire_enabled=acquire, corpus_dir=collection.corpus_dir,
        manifest=collection.manifest_path, fts=collection.fts_dir,
        lancedb=collection.lance_dir, license_ack=ack,
        genesis_archive=collection.genesis_archive,
        embed=collections_mod.recipe_for(collection),
        embed_mode=collection.embed_mode_default, layers=collection.layers,
        snapshot=collection.snapshot, extras=collection.extras,
        # peer_source was missing here: acknowledging a licence would
        # silently un-declare a collection fed from a peer torrent
        peer_source=collection.peer_source, title=collection.title,
        slices=collection.slices, kinds=collection.kinds,
        share=collection.share, reranker=collection.reranker)
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
    tar.gz download. The bundle is built in a scratch dir under the
    collection's export workspace (EXPORT_DIR, else beside its data — no
    /tmp assumptions) and cleaned up after the response streams out."""
    from .bundles import export as bundle_export
    collection = _collection_or_404(name)
    # invariant 9, "not exchanged": the download is a signed, shareable bundle
    _refuse_withheld(collection, "its vectors are exported as a bundle")
    con = db.connect(collection)
    uids = body.uids or [r["uid"] for r in con.execute(
        "SELECT uid FROM documents WHERE in_scope=1 AND embed_done=1 "
        "ORDER BY CAST(native_id AS INTEGER) LIMIT ?", (max(1, body.limit),))]
    if not uids:
        raise HTTPException(400, "no embedded documents to export")

    try:
        where = bundle_export.workspace(collection)
    except bundle_export.ExportError as e:
        raise HTTPException(503, str(e))
    scratch = Path(tempfile.mkdtemp(prefix="bundle-export-", dir=where))
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
    # The sample is keyed on collection NAME and nothing prunes it, so a
    # retired collection kept appearing on the Network page — named, with a
    # frozen rate — after it had left the Library entirely. The listing has
    # to agree with what the node actually holds.
    live_names = {c.name for c in collections_mod.load_all()}
    for name, entry in worker_stats.items():
        if name not in live_names:
            continue
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
    # node.sqlite: the worker reads it from the same place (app/node_state.py)
    node_state.set_bandwidth_override(rate_bps, ttl_seconds=max(1, body.hours) * 3600)
    # apply to this process too, so its own SSE feed reflects the change at once
    transfer_manager.get_manager().set_override(rate_bps)
    message = (f"bandwidth override {'cleared' if rate_bps is None else body.rate} "
               f"({body.hours}h)")
    try:
        db.log_event(db.connect(), "info", "control", message)
    except Exception:   # noqa: BLE001 — the override is set; the event is a courtesy
        print(f"[info:control] {message}", flush=True)
    return {"ok": True, "rate_bps": rate_bps, "hours": body.hours}


# ---------------- System ----------------

def _reranker_label(recipe) -> str:
    if recipe is None:
        return "(none — fused order)"
    return f"{recipe.model or '(model not named)'} @ {recipe.url} ({recipe.api})"


def _encoder_label(c) -> str:
    """The encoder the collection RUNS — `collections.recipe_for`, the one
    resolution the worker and search use: its own file's, since there is no
    default one (operator, 2026-09-18)."""
    try:
        r = collections_mod.recipe_for(c)
    except collections_mod.NoEncoder:
        return "(none — non-compliant)"
    return f"{r.model} @ {r.url}" + (" (+query encoder)" if r.query_url else "")


def _falls_back(names: list[str]) -> str:
    return ("used by " + ", ".join(names)) if names else \
        "used by no collection — each names its own"


def _effective_config() -> dict:
    # ONLY WHAT IS GLOBAL (operator, 2026-09-18). The page listed
    # EMBED_MODEL, CHUNK_TOKENS, CHUNK_OVERLAP, CHUNK_VERSION and
    # CLEANING_VERSION as the node's, while each collection's encoder comes
    # from its own file and its chunker and cleaning from its Codex — a
    # column of values that decided nothing it appeared to decide. What
    # each collection runs is Tier 2, below. EMBED_URL is gone altogether:
    # there is no default encoder. RERANK_URL stays, saying who reaches it.
    cols = collections_mod.load_all()
    rerank_users = [c.name for c in cols if getattr(c, "reranker", None) is None]
    tier1 = {
        "CHAT_URL": config.CHAT_URL,
        "CHAT_API": f"{config.CHAT_API} — the protocol CHAT_URL speaks",
        "CHAT_MODEL": config.CHAT_MODEL,
        "RAGE_URL": config.RAGE_URL or "(unset — no list of models, no load or unload)",
        "TRANSLATE_URL": config.TRANSLATE_URL or "(unset — the chat model translates, slowly)",
        "TRANSLATE_API": f"{config.TRANSLATE_API} — the protocol TRANSLATE_URL speaks",
        "GPU_PROBE_URL": config.GPU_PROBE_URL or "(unset — no GPU panel)",
        "RERANK_URL": (f"{config.RERANK_URL} — the reranker of last resort, "
                       f"{_falls_back(rerank_users)}") if config.RERANK_URL
                      else "(unset — a collection with no [reranker] keeps its fused order)",
        "FTS_VERSION": config.FTS_VERSION, "EMBED_VERSION": config.EMBED_VERSION,
        "ENTITY_VERSION": config.ENTITY_VERSION, "FETCH_VERSION": config.FETCH_VERSION,
        "DISCOVER_INTERVAL_MIN": config.DISCOVER_INTERVAL_MIN,
        "ADMIN_TOKEN": "(set)" if config.ADMIN_TOKEN else "(unset — admin routes locked)",
    }
    tier2 = [{
        "collection": c.name, "plugin": _plugin_label(c.plugin), "profile": c.profile,
        "embed_mode": c.embed_mode_default, "acquire_enabled": c.acquire_enabled,
        "license_ack": "(recorded)" if c.license_ack else "(none)",
        "embed_recipe": _encoder_label(c),
        # THE resolution search uses (collections.reranker_for): the
        # collection's [reranker], else RERANK_URL, else none
        "reranker": _reranker_label(collections_mod.reranker_for(c)),
    } for c in cols]

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
        "ask.latency_budget_ms": pc.ask.latency_budget_ms,
    }
    if pc.embed_defaults:
        # still parsed, so an older platform.toml loads — and said to be
        # ignored, so nobody believes it decides anything
        tier2_platform["encoder_defaults"] = (
            f"IGNORED ({', '.join(sorted(pc.embed_defaults))}): there is no default "
            f"encoder — a collection names its own in its file (operator, 2026-09-18)")

    # the node's overrides from THE resolution the worker uses
    # (app/node_state.py); scope is per-collection and stays in its manifest
    override = node_state.bandwidth_override()
    tier3 = {"worker_state (state/node.sqlite)": node_state.worker_state(),
             "scope (control table)": db.get_scope(db.connect()),
             "bandwidth override (state/node.sqlite, expires)":
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
    endpoints.setdefault(config.CHAT_URL, []).append("CHAT_URL")
    if config.TRANSLATE_URL:
        endpoints.setdefault(mt.health_url(), []).append("TRANSLATE_URL")
    if config.RERANK_URL:
        endpoints.setdefault(config.RERANK_URL, []).append("RERANK_URL")
    # per-collection [embed] recipes (doc amendment 9): each declared
    # endpoint — including a bi-encoder's query side — is pinged too
    for c in collections_mod.load_all():
        if c.embed is not None:
            endpoints.setdefault(c.embed.url, []).append(f"{c.name}.embed")
            if c.embed.query_url:
                endpoints.setdefault(c.embed.query_url, []).append(f"{c.name}.query")
        # and its reranker (05 §5.6) — a collection file's [reranker] is
        # an endpoint like any other, and was the one the page left out
        own = getattr(c, "reranker", None)
        if own is not None:
            endpoints.setdefault(own.url, []).append(f"{c.name}.rerank")
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
                # the manifest that STORES a node-wide line is not its subject
                event["collection"] = ("node" if event.get("stage") in db.NODE_STAGES
                                       else collection.name)
                rows.append(event)
        except Exception:   # noqa: BLE001 — same reasoning per collection
            continue
    rows.sort(key=lambda e: (e.get("ts") or 0, e.get("id") or 0), reverse=True)
    return rows[:limit]


@admin_router.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    collections = collections_mod.load_all()
    return TEMPLATES.TemplateResponse(request, "system.html", {
        "active": "system",
        # what load_all refused, and why — Refusal's own docstring promised
        # this page and nothing showed it; a collection naming no encoder is
        # one (operator, 2026-09-18: "a non compliant collection")
        "refusals": collections_mod.refusals(),
        "effective": _effective_config(),
        "boot_report": spaces.report_boot_status(collections),
        "health": _health_pings(),
        "worker_state": node_state.worker_state(),
    })


@admin_router.get("/system/log", response_class=HTMLResponse)
def system_log_fragment(request: Request):
    return TEMPLATES.TemplateResponse(request, "_fragments/log_rows.html",
                                       {"events": _recent_events_all()})


@admin_router.get("/system/gpu", response_class=HTMLResponse)
def system_gpu_fragment(request: Request):
    # asked of the probe over HTTP, bounded (app/gpu.py): this process never
    # reads /proc — only the gpu-probe container may see the host's processes
    # RAGE is asked ONCE: its name for itself colours the bar, its reading of
    # the card and its events are its notification to Louise
    return TEMPLATES.TemplateResponse(request, "_fragments/gpu.html",
                                       gpu.panel(config.GPU_PROBE_URL)
                                       | {"governor": gpu.governor(),
                                          "rage": gpu.rage_status(config.RAGE_URL)})


@admin_router.get("/system/rage-alerts", response_class=HTMLResponse)
def rage_alerts_fragment(request: Request):
    """RAGE's notification to Louise, for the strip on top of the Library and
    the System page (operator, 2026-09-20: "if Louise needs a model that RAGE
    cannot fit in memory I want a notification to Louise"). Empty when there
    is nothing to say; bounded like every call to RAGE."""
    return TEMPLATES.TemplateResponse(request, "_fragments/rage_alerts.html",
                                       {"rage": gpu.rage_status(config.RAGE_URL)})
