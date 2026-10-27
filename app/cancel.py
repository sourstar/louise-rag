"""A search whose caller has gone stops (operator, 2026-09-21).

It used not to. The node, 2026-09-21: a PubMed search was still running 75
minutes after its caller had given up, holding a core and a read snapshot
on a 51.5 GB manifest, answering nobody. The query it was stuck in is fixed
(98ae00d); this is what makes the NEXT slow path end with its caller.

Python cannot stop a thread, so stopping is cooperative, in two halves:

- a TOKEN, current for the search's own thread (a ContextVar), that the
  endpoint cancels when its caller disconnects (`while_connected`), and that
  the search asks at every stage boundary (`checkpoint`);
- every connection the search opens is WATCHED: `interrupt()` aborts the
  statement running when the caller leaves, and a progress handler aborts
  any statement after that — one begun a moment later included (`watch`).

What cannot be interrupted is one in-flight HTTP call to a model (a translation,
a query embedding, a rerank); each is bounded by its own timeout, and the
checkpoint after it stops the search. So a cancelled search stops within one
such step.

`Cancelled` is a BaseException, deliberately. Retrieval is full of handlers
that catch Exception on purpose — "an index problem empties the leg, never
fails a search". A Cancelled they could catch would be logged as a failed
leg, and the search would go on to fuse and rerank for nobody.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from typing import Any, Callable

#: how often an endpoint asks whether its caller is still there: a quarter
#: of a second is a single non-blocking receive, and bounds how long a
#: search runs for nobody before it is told
POLL_S = 0.25
#: how many SQLite VM instructions run between two looks at the token: a
#: statement stops within that many of its caller leaving, and a search's
#: statements pay one Python call per that many — nothing, at their sizes
PROGRESS_STEPS = 1000


class Cancelled(BaseException):
    """The caller has gone; nothing is answered. A BaseException so that no
    `except Exception` on the way out can swallow it (module docstring)."""


class Token:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._watched: list = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            self._event.set()
            watched = list(self._watched)
        for con in watched:
            try:
                con.interrupt()
            except Exception:   # noqa: BLE001 — already closed: nothing running on it
                pass

    def watch(self, con) -> None:
        """A connection this search runs statements on — called from the
        thread that owns it, right after it is opened.

        Two levers, because each misses what the other catches:
        `interrupt()` (in `cancel`) aborts the statement running AT THAT
        MOMENT, even inside one long operation such as a sort; the PROGRESS
        HANDLER, asked every PROGRESS_STEPS VM instructions, aborts any
        statement once the token is cancelled — including one that STARTS
        afterwards. `interrupt()` alone does not reach that one: an
        interrupt with nothing running is a no-op, and a statement begun a
        moment later ran on. tests/test_cancel.py found it with a query that
        never ends by itself: it ran forever."""
        with self._lock:
            self._watched.append(con)
        con.set_progress_handler(lambda: 1 if self._event.is_set() else 0, PROGRESS_STEPS)

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


_current: contextvars.ContextVar[Token | None] = contextvars.ContextVar(
    "search_token", default=None)


def current() -> Token | None:
    return _current.get()


def checkpoint() -> None:
    """Stop here if the caller has gone. Outside a cancellable search (the
    worker, a CLI, a test) there is no token and this does nothing."""
    token = _current.get()
    if token is not None:
        token.check()


def watch(con):
    """`con`, registered to be interrupted if the caller goes. Returns it, so
    `con = cancel.watch(db.connect(c))` reads as one step."""
    token = _current.get()
    if token is not None:
        token.watch(con)
    return con


def run(token: Token | None, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """`fn` in THIS thread with `token` current."""
    reset = _current.set(token)
    try:
        return fn(*args, **kwargs)
    finally:
        _current.reset(reset)


def carry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, run under the token current where `carry` is called — for a
    thread pool, whose threads do not inherit a ContextVar. Without it, a
    federated search's per-collection legs would never see the token."""
    token = _current.get()

    def carried(*args: Any, **kwargs: Any) -> Any:
        return run(token, fn, *args, **kwargs)
    return carried


async def while_connected(request, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """`fn(*args, **kwargs)` in a worker thread, for as long as `request`'s
    caller is there. Returns its result; raises Cancelled once the caller has
    disconnected, after cancelling the token the search runs under.

    Not awaited after that: nobody is waiting for the answer, and the thread
    stops at its next checkpoint by itself. Its outcome is consumed, so a
    Cancelled it raises is never reported as an unretrieved exception."""
    from starlette.concurrency import run_in_threadpool
    token = Token()
    t0 = time.monotonic()
    work = asyncio.ensure_future(run_in_threadpool(run, token, fn, *args, **kwargs))
    while True:
        done, _ = await asyncio.wait({work}, timeout=POLL_S)
        if done:
            return work.result()
        if await request.is_disconnected():
            token.cancel()
            work.add_done_callback(_consume)
            # SAID, because nothing else is: uvicorn logs no line for a caller
            # who left, so a question that got no answer left no trace at all
            # (2026-10-27: 65 s of cold model loads, the browser gone, nothing
            # in any log — München)
            path = getattr(getattr(request, "url", None), "path", "?")
            _say(f"{path}: the caller left after {time.monotonic() - t0:.1f} s; "
                 f"the search is cancelled")
            raise Cancelled()


def _say(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [web info:ask] {msg}", flush=True)


def _consume(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()
