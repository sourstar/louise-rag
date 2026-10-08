"""Model work by priority — docs/notes/scheduling.md §12.

THE LEVELS. Every request to a model server carries one, lowest first:
corpus embedding, translation, chat, reranking, query embedding (operator,
2026-09-19). The worker runs at CORPUS; the web process declares each call's
level with `interactive()`.

ONE GATE PER ENDPOINT. Every request to one model server passes that
server's gate, which admits at most the server's capacity at a time and
chooses who goes next: strictly by level, then — within a level — the
plugin that has had the least service time at this gate, then arrival. A
plugin that returns from idle starts level with whoever is active and
banks nothing while away (§10: idle costs neither the entitlement nor
earns credit). An unbounded gate (capacity None) admits at once: it orders
nothing, and exists so that bounding an endpoint is configuration, not code.

LEVELS ORDER WHERE REQUESTS MEET, AT A SHARED GATE, and nowhere else: query
before corpus on one encoder, chat before translation on one CHAT_URL. Corpus
work does NOT stand aside from the whole device while something interactive
runs on another model. It did for one deploy, and measured live
(2026-09-20) it slowed chat: alone on the card, token-by-token decoding lets
the Arc drop its clock, so a chat call took 87-92 s (a mean of 990 MHz)
against 65-72 s beside corpus work (2,695 MHz). With the clock pinned,
standing aside bought chat 3 s and cost corpus its whole share
(docs/notes/scheduling.md §12).

THE CLAIM. The web process still holds a claim in `state/node.sqlite` while
any interactive call runs (`interactive()`). It is what the GPU governor
reads (app/gpu_governor.py): the card's clock is pinned at its maximum for
the claim's lifetime, which is where chat's speed actually comes from.

BOUNDED. A wait at a gate ends at the request's own timeout, or when the
process stops (`stop_all`), with `GateTimeout` — an `httpx.ConnectTimeout`,
so every caller already treats it as the endpoint being busy: a deferral,
never an error written onto a document (CLAUDE.md: a pipeline thread never
waits without a bound).
"""
from __future__ import annotations

import contextvars
import itertools
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import IntEnum

import httpx

from . import config


class Level(IntEnum):
    CORPUS = 0
    TRANSLATE = 1
    CHAT = 2
    RERANK = 3
    QUERY = 4


class GateTimeout(httpx.ConnectTimeout):
    """Not admitted in time, or the process is stopping. The endpoint is
    busy with higher-priority work: a deferral for every caller."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# ---- the caller's level ----------------------------------------------------

_default_level = Level.QUERY
_ctx: contextvars.ContextVar[tuple[Level, str] | None] = \
    contextvars.ContextVar("louise_dispatch", default=None)


def set_default_level(level: Level) -> None:
    """The level of a request that declares none: CORPUS in the worker,
    whose unmarked calls are background work (sample proofs, checks);
    QUERY in the web, whose unmarked calls have a person waiting."""
    global _default_level
    _default_level = level


def current() -> tuple[Level, str]:
    got = _ctx.get()
    return got if got is not None else (_default_level, "")


#: docs/09-formats.md §9.12, *the request's level*
LEVEL_HEADER = "Louise-Level"


def level_headers() -> dict[str, str]:
    """The header every request to a model endpoint carries: this caller's
    level, by name (`corpus`, `translate`, `chat`, `rerank`, `query`).

    ADVISORY, as the spec says: RAGE holds the card's clock while a request
    at `translate` or above is in flight (docs/notes/rage.md §11), and no
    answer depends on it — an endpoint that ignores the header loses nothing
    but speed. It is what lets the GPU governor's claim in the node store
    retire: RAGE sees every model request itself."""
    return {LEVEL_HEADER: current()[0].name.lower()}


@contextmanager
def working_as(level: Level, plugin: str = ""):
    """Declare the level and plugin of the requests made inside the block,
    on this thread (and on the threads the block hands context to — see
    `embeddings._embed_texts`)."""
    token = _ctx.set((Level(level), plugin))
    try:
        yield
    finally:
        try:
            _ctx.reset(token)
        except ValueError:
            # ENTERED IN ONE CONTEXT, LEFT IN ANOTHER. A block that spans the
            # `yield`s of a generator (llm.chat_stream) is stepped by
            # Starlette in its threadpool, and every step runs in a FRESH
            # COPY of the request's context. The value was set in the first
            # step's copy, which went with that step; in this one the
            # variable is what the request had, so there is nothing to put
            # back — and `reset` refuses a token from another Context.
            # Left to raise, it ended every streamed answer on the Ask page
            # with "[error: <Token … was created in a different Context]"
            # (operator, 2026-09-19), AFTER the gate and the claim had been
            # released correctly: only the message was wrong.
            pass


# ---- stopping --------------------------------------------------------------

_stopping = threading.Event()


def stop_all() -> None:
    """Wake every waiter with GateTimeout: the process is stopping, and a
    request not yet admitted is not a unit in flight (invariant 2)."""
    _stopping.set()
    for g in list(_gates.values()):
        with g._cv:
            g._cv.notify_all()


def reset() -> None:
    """Tests: forget every gate, the claim memo, the stop and the default."""
    global _default_level
    _stopping.clear()
    _gates.clear()
    _claim_seen.clear()
    _default_level = Level.QUERY


# ---- the gates -------------------------------------------------------------

@dataclass
class _Waiter:
    level: Level
    plugin: str
    seq: int


@dataclass
class _LevelStats:
    admitted: int = 0
    waited_s: float = 0.0
    served_s: float = 0.0


@dataclass
class Gate:
    name: str
    capacity: int | None
    _cv: threading.Condition = field(default_factory=threading.Condition)
    _busy: int = 0
    _waiting: list = field(default_factory=list)
    _served: dict = field(default_factory=dict)      # plugin -> seconds
    _active: Counter = field(default_factory=Counter)
    _seq: itertools.count = field(default_factory=itertools.count)
    stats: dict = field(default_factory=dict)          # Level -> _LevelStats

    def _join(self, plugin: str) -> None:
        """A plugin with nothing waiting or running here starts level with
        the least-served plugin that is: it banks nothing while away."""
        if self._active[plugin] or any(w.plugin == plugin for w in self._waiting):
            return
        present = [self._served.get(p, 0.0) for p in
                   {w.plugin for w in self._waiting} | {p for p, n in self._active.items() if n}]
        if present:
            self._served[plugin] = max(self._served.get(plugin, 0.0), min(present))

    def _head(self) -> _Waiter:
        return min(self._waiting,
                   key=lambda w: (-w.level, self._served.get(w.plugin, 0.0), w.seq))

    def _may_go(self, w: _Waiter) -> bool:
        if self.capacity is None:
            return True
        return self._busy < self.capacity and self._head() is w

    @contextmanager
    def admit(self, level: Level | None = None, plugin: str | None = None, *,
              timeout: float | None = None):
        lvl, plg = current()
        lvl = Level(level) if level is not None else lvl
        plg = plugin if plugin is not None else plg
        w = _Waiter(lvl, plg, next(self._seq))
        started = time.monotonic()
        deadline = None if timeout is None else started + timeout
        with self._cv:
            self._join(plg)
            self._waiting.append(w)
            try:
                while not self._may_go(w):
                    if _stopping.is_set():
                        raise GateTimeout(f"{self.name}: the process is stopping")
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        raise GateTimeout(f"{self.name}: not admitted within {timeout:.0f}s "
                                          f"(level {lvl.name.lower()})")
                    self._cv.wait(timeout=None if deadline is None else deadline - now)
            except BaseException:
                self._waiting.remove(w)
                self._cv.notify_all()
                raise
            self._waiting.remove(w)
            self._busy += 1
            self._active[plg] += 1
            s = self.stats.setdefault(lvl, _LevelStats())
            s.admitted += 1
            s.waited_s += time.monotonic() - started
        began = time.monotonic()
        try:
            yield
        finally:
            spent = time.monotonic() - began
            with self._cv:
                self._busy -= 1
                self._active[plg] -= 1
                self._served[plg] = self._served.get(plg, 0.0) + spent
                self.stats.setdefault(lvl, _LevelStats()).served_s += spent
                self._cv.notify_all()

    def snapshot(self) -> dict:
        with self._cv:
            return {"capacity": self.capacity, "busy": self._busy,
                    "waiting": len(self._waiting),
                    "levels": {lvl.name.lower(): vars(s).copy()
                               for lvl, s in sorted(self.stats.items())}}


_gates: dict[str, Gate] = {}
_gates_lock = threading.Lock()


def gate(url: str, capacity: int | None = None) -> Gate:
    """The gate of the server at `url` — one per server per process, keyed
    on the URL (the physical endpoint, never a collection name). The
    capacity given by the first caller stands until one gives another."""
    key = url.rstrip("/")
    with _gates_lock:
        g = _gates.get(key)
        if g is None:
            g = _gates[key] = Gate(name=key, capacity=capacity)
        elif capacity is not None and g.capacity != capacity:
            with g._cv:
                g.capacity = capacity
                g._cv.notify_all()
        return g


def configured_capacity(url: str) -> int | None:
    """An interactive endpoint's capacity from GATE_CAPACITY
    (`url=n,url=n`), or None — unbounded. Nothing measured says interactive
    endpoints contend (docs/notes/scheduling.md §12), and bounding one
    without a measurement would slow what it serves; so bounding is the
    operator's line of configuration, not a default."""
    for part in (config.GATE_CAPACITY or "").split(","):
        name, sep, n = part.strip().rpartition("=")
        if sep and name.rstrip("/") == url.rstrip("/") and n.strip().isdigit():
            return max(1, int(n))
    return None


def snapshot() -> dict:
    with _gates_lock:
        gates = dict(_gates)
    return {name: g.snapshot() for name, g in sorted(gates.items())}


# ---- the interactive claim (crosses processes) -----------------------------

CLAIM_KEY = "interactive_until"


def _node_con() -> sqlite3.Connection | None:
    """The node store, with a ONE-second busy wait rather than the store's
    usual sixty: the claim is written on the path of a person's request,
    and a claim that cannot be had quickly is better lost than waited for.

    None when the store does not exist: the claim never CREATES it. The
    worker makes it at boot, so on a node it is always there, and a
    process with no store has no worker to stand aside (and a test that
    made one would have written a node's live state, conftest)."""
    from . import node_state
    p = node_state.path()
    if not p.is_file():
        return None
    return sqlite3.connect(p, timeout=1.0, isolation_level=None)


def _write_claim(until: float) -> None:
    """Never raises: an interactive request is answered even when the node
    store cannot be written — losing a claim costs one request's wait."""
    try:
        con = _node_con()
        if con is None:
            return
        with closing(con):
            con.execute("INSERT INTO control(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (CLAIM_KEY, repr(until)))
    except Exception:   # noqa: BLE001
        pass


_claim_seen: dict[str, tuple[float, bool]] = {}
_claim_lock = threading.Lock()


def interactive_active() -> bool:
    """Is an interactive request live anywhere on the node? Memoised for
    GATE_POLL_S per process: the governor asks this every poll, and the
    rollback rotation between documents."""
    now = time.monotonic()
    with _claim_lock:
        seen = _claim_seen.get("claim")
        if seen is not None and now - seen[0] < config.GATE_POLL_S:
            return seen[1]
    active = False
    try:
        con = _node_con()
        if con is not None:
            with closing(con):
                row = con.execute("SELECT value FROM control WHERE key=?",
                                  (CLAIM_KEY,)).fetchone()
            active = row is not None and float(row[0]) > time.time()
    except Exception:   # noqa: BLE001 — an unreadable claim holds nothing back
        active = False
    with _claim_lock:
        _claim_seen["claim"] = (now, active)
    return active


class _Board:
    """This process's live interactive calls. The claim is written when the
    first one starts, refreshed while any runs (a chat stream can outlast
    the TTL), and cleared when the last one ends — so the governor lets the
    card's clock go the moment nothing interactive is left, not a TTL
    later."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.live = 0
        self.refresher: threading.Thread | None = None

    def enter(self) -> None:
        with self.lock:
            self.live += 1
            first = self.live == 1
            if first:
                _write_claim(time.time() + config.INTERACTIVE_CLAIM_TTL_S)
            if self.refresher is None or not self.refresher.is_alive():
                self.refresher = threading.Thread(target=self._refresh,
                                                  name="interactive-claim", daemon=True)
                self.refresher.start()

    def leave(self) -> None:
        with self.lock:
            self.live -= 1
            if self.live == 0:
                _write_claim(0.0)

    def _refresh(self) -> None:
        while True:
            time.sleep(config.INTERACTIVE_CLAIM_TTL_S / 2)
            with self.lock:
                if self.live == 0:
                    self.refresher = None
                    return
                _write_claim(time.time() + config.INTERACTIVE_CLAIM_TTL_S)


_board = _Board()


@contextmanager
def interactive(level: Level):
    """An interactive model call at `level`: declares the level for the
    requests inside, and holds the node's claim for as long as the call
    runs — the GPU governor keeps the card's clock at its maximum while it
    is held."""
    _board.enter()
    try:
        with working_as(level):
            yield
    finally:
        _board.leave()


@contextmanager
def call(url: str, level: Level, *, timeout: float | None = None):
    """The whole of an interactive request to the server at `url`: the
    claim, the level, and that server's gate."""
    with interactive(level):
        with gate(url, configured_capacity(url)).admit(level, timeout=timeout):
            yield
