"""A model's cold start, kept out of the request that found it cold.

THE RULE (operator, 2026-10-09): "Timeouts shall not include cold start."
Found on the node: wikipedia's first search after idle loaded `vega-bge-m3`
and `louise-rerank-bge`; the reranker's 8 s budget (05 §5.6) ran out DURING
THE LOAD, so the list fell back unreranked — while warm the same search
takes 3.1 s in all, the rerank 1.7 s.

So, before a request to a model on a runner that can say what it holds —
LocalAI: `GET <server>/system` → `loaded_models[].id` — a model it does not
list is WARMED first: a minimal request of the same kind, bounded by the
model's LOAD timeout (`load_timeout_s`: the measured profile shipped in
`app/model_loads.json`, else `[models] load_timeout_s`, 07 §7.8). Only then
does the real request go, under its usual budget, which no longer holds the
load.

- ONE WARM PER (server, model) AT A TIME. A second caller that finds the
  same model cold waits for the first one's warm, as bounded as its own
  would have been, and never sends a second.
- `/system` IS ASKED AT MOST EVERY `SYSTEM_TTL_S` per server, so a hot path
  (the worker feeding the encoder) adds a cache lookup per request, not a
  request. LocalAI lists a model only once its load has finished (measured
  2026-10-09, docs/notes/model-profile.md), so a model someone else is
  loading reads as cold, and the warm simply joins that load.
- NOTHING CHANGES WHERE NOTHING IS KNOWN: a runner with no such route (404,
  a body that is not LocalAI's), an unreachable one, a request that names no
  model, a warm that fails other than by taking too long — the request goes
  exactly as it did before this existed, and meets its own fate.
- A LOAD PAST ITS BOUND raises `LoadTimeout` — an `httpx.ConnectTimeout`
  like the dispatch gate's own, so every caller already reads it as "not
  now": a deferral for embedding (never an error on a document), a visible
  fallback with the reason for a search's legs and its reranker.

Each cold start is recorded (`recent()`) and handed to a publisher thread
that writes it to the event log — never by the thread doing the work
(CLAUDE.md: telemetry is published by someone other than the worker).
"""
from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from . import httpc

#: how long one `/system` answer is trusted by the worker's hot path
SYSTEM_TTL_S = 2.0
#: how long it is trusted for a request a person waits on. In 2 s another
#: model's load can make LocalAI's reclaimer evict this one, and a check that
#: reads the older answer sends the request cold: the load is then paid
#: inside its budget (2026-10-25: 95 of 496 first searches lost their rerank
#: overnight beside normattiva's re-embed; the translator's load evicting the
#: idle reranker is the suspected path). A burst — a query's variants — shares
#: one read; a single request (rerank, chat, translation) asks with 0.
INTERACTIVE_TTL_S = 0.25
#: how long a server that has no `/system` (or not LocalAI's) is left alone
NO_ROUTE_TTL_S = 300.0
#: the bound on one `/system` read; measured 11 ms on the node
PROBE_TIMEOUT_S = 2.0
#: the measured profile's bounds, shipped with the code
BOUNDS_FILE = Path(__file__).with_name("model_loads.json")


class LoadTimeout(httpx.ConnectTimeout):
    """The model was still loading when its load bound ran out. A deferral
    for every caller, as `dispatch.GateTimeout` is; the message is the
    reason a fallback states."""

    def __init__(self, message: str, *, model: str, bound_s: float) -> None:
        super().__init__(message)
        self.model = model
        self.bound_s = bound_s


@dataclass
class ColdStart:
    """One warm: what was loaded, for whom, and how long it took."""
    time: float
    server: str
    model: str
    caller: str
    seconds: float
    bound_s: float
    outcome: str            # "loaded" · "timeout" · "failed: …"


@dataclass
class _Flight:
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    seconds: float | None = None


_lock = threading.Lock()
_systems: dict[str, tuple[float, frozenset[str] | None, float]] = {}   # server -> (at, ids, ttl)
_pids: dict[str, dict[str, int]] = {}                                    # server -> {model: backend pid}
_probe_locks: dict[str, threading.Lock] = {}
_flights: dict[tuple[str, str], _Flight] = {}
_recent: deque[ColdStart] = deque(maxlen=100)
_bounds: dict[str, float] | None = None


def reset() -> None:
    """Forget every cached answer, warm and record (tests)."""
    global _bounds
    with _lock:
        _systems.clear()
        _pids.clear()
        _probe_locks.clear()
        _flights.clear()
        _recent.clear()
        _bounds = None


def server_of(url: str) -> str:
    """The runner's root: `/system` is LocalAI's at the server, whatever path
    the endpoint has (`…/v1/rerank`)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url.rstrip("/")


def _fetch_system(server: str) -> frozenset[str] | None:
    """The ids LocalAI lists as loaded, or None when the server cannot say;
    each one's backend pid noted on the way (`backend_pid`)."""
    try:
        r = httpc.get(f"{server}/system", timeout=PROBE_TIMEOUT_S)
    except Exception:   # noqa: BLE001 — unreachable: nothing is known
        return None
    if r.status_code >= 500:
        return None                    # starting, failing: unknown for now, asked again soon
    if r.status_code != 200:
        raise _NoRoute()
    try:
        rows = r.json().get("loaded_models")
    except Exception:   # noqa: BLE001
        raise _NoRoute() from None
    if not isinstance(rows, list):
        raise _NoRoute()
    held = [m for m in rows if isinstance(m, dict) and isinstance(m.get("id"), str)]
    pids = {}
    for m in held:
        pid = (m.get("process") or {}).get("pid") if isinstance(m.get("process"), dict) else None
        if isinstance(pid, int):
            pids[m["id"]] = pid
    with _lock:
        _pids[server] = pids
    return frozenset(m["id"] for m in held)


class _NoRoute(Exception):
    """The server answered, and not as LocalAI's `/system` does."""


def loaded_models(server: str, max_age_s: float | None = None) -> frozenset[str] | None:
    """What `server` holds, from an answer at most `max_age_s` old (default
    `SYSTEM_TTL_S`; a runner with no `/system` is not asked again for
    `NO_ROUTE_TTL_S` whatever is asked); None when it cannot say. One read
    per server at a time: a caller arriving during a read takes its answer."""
    def fresh_enough(got) -> bool:
        age = time.monotonic() - got[0]
        limit = got[2] if got[2] == NO_ROUTE_TTL_S or max_age_s is None else min(got[2], max_age_s)
        return age < limit

    with _lock:
        got = _systems.get(server)
        if got is not None and fresh_enough(got):
            return got[1]
        lock = _probe_locks.setdefault(server, threading.Lock())
    with lock:
        with _lock:
            got = _systems.get(server)
            if got is not None and fresh_enough(got):
                return got[1]
        try:
            ids, ttl = _fetch_system(server), SYSTEM_TTL_S
        except _NoRoute:
            ids, ttl = None, NO_ROUTE_TTL_S
        with _lock:
            _systems[server] = (time.monotonic(), ids, ttl)
        return ids


def backend_pid(url: str, model: str | None, max_age_s: float = INTERACTIVE_TTL_S) -> int | None:
    """The pid of the backend that serves `model` on `url`'s runner, from an
    answer at most `max_age_s` old; None when it is not loaded or the runner
    cannot say. A different pid after a request than before it means the
    model was loaded again in between — a cold start that request paid."""
    if not model:
        return None
    server = server_of(url)
    ids = loaded_models(server, max_age_s)
    if not ids or model not in ids:
        return None
    with _lock:
        return _pids.get(server, {}).get(model)


def reloaded_since(url: str, model: str, pid: int | None) -> bool:
    """Was `model`, served by backend `pid` before a request, evicted since —
    gone from the runner now, or served by another backend? Asked fresh;
    False when nothing is known (no pid before, or a runner that cannot
    say)."""
    if pid is None or not model:
        return False
    server = server_of(url)
    ids = loaded_models(server, 0.0)
    if ids is None:
        return False
    if model not in ids:
        return True                       # evicted, and loading again or not
    with _lock:
        now = _pids.get(server, {}).get(model)
    return now is not None and now != pid


def note_cold_call(url: str, model: str, caller: str, seconds: float) -> None:
    """Record a cold start a request paid inside its own budget (the model
    was evicted between the check and the request): published like a warm's,
    so the event log says what the fallback's reason says."""
    _record(ColdStart(time.time(), server_of(url), model, caller, seconds, load_timeout_s(model),
                      "failed: evicted before the request, loaded inside its budget"))


def speaks_localai(server: str) -> bool | None:
    """Is `server` LocalAI? True when its `/system` answers as LocalAI's,
    False when it answers as something else (llama-server, an OpenAI-style
    service), None when it cannot be asked now. From `loaded_models`' cache."""
    if loaded_models(server) is not None:
        return True
    with _lock:
        got = _systems.get(server)
    return False if got is not None and got[2] == NO_ROUTE_TTL_S else None


#: the bound on one unload request
UNLOAD_TIMEOUT_S = 30.0


def activity(server: str) -> dict[str, float] | None:
    """`{model: cpu_percent}` of what a LocalAI runner holds, read fresh from
    `/system` (its `loaded_models[].process.cpu_percent`); None when the
    runner cannot say. A model whose backend burns no CPU is not serving."""
    try:
        r = httpc.get(f"{server}/system", timeout=PROBE_TIMEOUT_S)
        rows = r.json().get("loaded_models") if r.status_code == 200 else None
    except Exception:   # noqa: BLE001
        return None
    if not isinstance(rows, list):
        return None
    out: dict[str, float] = {}
    for m in rows:
        if isinstance(m, dict) and isinstance(m.get("id"), str):
            proc = m.get("process") or {}
            try:
                out[m["id"]] = float(proc.get("cpu_percent") or 0.0)
            except (TypeError, ValueError):
                out[m["id"]] = 0.0
    return out


def unload(url: str, model: str | None) -> bool:
    """Ask a LocalAI runner to stop `model`'s backend (`POST <server>/backend/
    shutdown {"model": …}`), freeing its memory; it loads again on the next
    request. Only where the runner lists the model as loaded — a runner that
    cannot say (not LocalAI, unreachable) is left alone. True when it was
    stopped. Never raises (operator, 2026-10-14: an embedder whose collection
    does not embed everything must not hold the card)."""
    if not url or not model:
        return False
    server = server_of(url)
    ids = loaded_models(server)
    if ids is None or model not in ids:
        return False
    try:
        r = httpc.post(f"{server}/backend/shutdown", json={"model": model},
                       timeout=UNLOAD_TIMEOUT_S)
    except Exception:   # noqa: BLE001 — the runner keeps it; nothing breaks
        return False
    with _lock:
        _systems.pop(server, None)           # what it holds has changed
    return r.status_code == 200


def _note_loaded(server: str, model: str) -> None:
    with _lock:
        got = _systems.get(server)
        if got is not None and got[1] is not None:
            _systems[server] = (got[0], got[1] | {model}, got[2])


def _profile_bounds() -> dict[str, float]:
    global _bounds
    if _bounds is None:
        try:
            raw = json.loads(BOUNDS_FILE.read_text(encoding="utf-8"))
            _bounds = {str(m): float(v["load_timeout_s"])
                       for m, v in (raw.get("models") or {}).items()
                       if isinstance(v, dict) and v.get("load_timeout_s")}
        except (OSError, ValueError, TypeError, AttributeError):
            _bounds = {}
    return _bounds


def load_timeout_s(model: str) -> float:
    """The model's load bound: its measured one (app/model_loads.json, from
    the node's profile), else the node's `[models] load_timeout_s`."""
    measured = _profile_bounds().get(model)
    if measured:
        return measured
    from . import platform_config
    try:
        return float(platform_config.load().models.load_timeout_s)
    except Exception:   # noqa: BLE001 — a broken platform.toml must not break a request
        return float(platform_config.ModelsConfig().load_timeout_s)


def ensure_loaded(url: str, model: str | None, warm: Callable[[float], object], *,
                  caller: str, max_age_s: float | None = None) -> float | None:
    """Warm `model` on the runner behind `url` if the runner says it is not
    loaded. `warm(bound_s)` sends one minimal request bounded by `bound_s`.
    The runner's answer may be `max_age_s` old: by default `SYSTEM_TTL_S`
    for corpus work and `INTERACTIVE_TTL_S` above it; a single request a
    person waits on (a rerank, a chat answer, a translation) passes 0.

    Returns the seconds this call spent waiting for the load, or None when
    nothing was waited for (loaded, or nothing known). Raises `LoadTimeout`
    when the load outlasted its bound."""
    if not model:
        return None
    if max_age_s is None:
        from . import dispatch
        max_age_s = INTERACTIVE_TTL_S if dispatch.current()[0] > dispatch.Level.CORPUS else SYSTEM_TTL_S
    server = server_of(url)
    ids = loaded_models(server, max_age_s)
    if ids is None or model in ids:
        return None
    key = (server, model)
    with _lock:
        flight = _flights.get(key)
        owner = flight is None
        if owner:
            flight = _flights[key] = _Flight()
    bound = load_timeout_s(model)
    if not owner:
        t0 = time.monotonic()
        if not flight.done.wait(bound):
            raise LoadTimeout(_reason(model, bound), model=model, bound_s=bound)
        if isinstance(flight.error, LoadTimeout):
            raise flight.error
        return time.monotonic() - t0
    t0 = time.monotonic()
    outcome = "failed: interrupted"
    try:
        warm(bound)
        outcome = "loaded"
    except httpx.ConnectTimeout:
        outcome = "failed: the server did not accept a connection"    # down, not loading
    except httpx.TimeoutException:
        flight.error = LoadTimeout(_reason(model, bound), model=model, bound_s=bound)
        outcome = "timeout"
    except Exception as e:   # noqa: BLE001 — the request goes as it did before
        outcome = f"failed: {type(e).__name__}: {str(e)[:200]}"
    finally:
        flight.seconds = time.monotonic() - t0
        if outcome == "loaded":
            # before the flight ends: a caller arriving now finds it loaded,
            # never cold with no warm in flight
            _note_loaded(server, model)
        with _lock:
            _flights.pop(key, None)
        flight.done.set()
    _record(ColdStart(time.time(), server, model, caller, flight.seconds, bound, outcome))
    if flight.error is not None:
        raise flight.error
    return flight.seconds


def _reason(model: str, bound: float) -> str:
    return f"model loading took longer than {_seconds(bound)} s ({model})"


def _seconds(v: float) -> str:
    return str(int(v)) if math.isclose(v, round(v)) else f"{v:.1f}"


# ---- the record, and who publishes it ---------------------------------------

_queue: queue.Queue[ColdStart] = queue.Queue(maxsize=1000)
_publisher: threading.Thread | None = None


def recent() -> list[ColdStart]:
    with _lock:
        return list(_recent)


def message(c: ColdStart) -> str:
    if c.outcome == "loaded":
        what = f"loaded in {c.seconds:.1f} s"
    elif c.outcome.startswith("failed: evicted"):
        what = (f"was evicted between the check and the request, which then paid the load inside "
                f"its budget and gave up after {c.seconds:.1f} s")
    elif c.outcome == "timeout":
        what = f"still loading after {_seconds(c.bound_s)} s — the caller fell back or deferred"
    else:
        what = f"warm {c.outcome} after {c.seconds:.1f} s — the request went as before"
    return (f"cold start: {c.model} on {c.server} {what} (for {c.caller}; "
            f"load bound {_seconds(c.bound_s)} s)")


def _record(c: ColdStart) -> None:
    global _publisher
    with _lock:
        _recent.append(c)
        if _publisher is None or not _publisher.is_alive():
            _publisher = threading.Thread(target=_publish_forever, name="model-load-log",
                                          daemon=True)
            _publisher.start()
    try:
        _queue.put_nowait(c)
    except queue.Full:
        pass


def _publish_forever() -> None:
    while True:
        c = _queue.get()
        try:
            publish(c)
        except Exception:   # noqa: BLE001 — a log line must never take a thread down
            pass


def publish(c: ColdStart) -> None:
    """Write one cold start to the node's event log: the stdout line, and the
    `events` table of the first collection's manifest, as every node line is
    (db.NODE_STAGES)."""
    from contextlib import closing
    from . import collections as collections_mod, db
    text = message(c)
    level = "info" if c.outcome == "loaded" else "warn"
    collections = collections_mod.load_all()
    if not collections:
        print(f"{time.strftime('%H:%M:%S')} [node {level}:models] {text}", flush=True)
        return
    with closing(db.connect(collections[0])) as con:
        db.log_event(con, level, "models", text)
