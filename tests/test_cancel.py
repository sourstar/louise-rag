"""A search whose caller has gone stops (app/cancel.py).

The node, 2026-09-21: a PubMed search was still running 75 minutes after its
caller had given up — holding a core and a read snapshot on a 51.5 GB
manifest, answering nobody. Nothing ever told it to stop.
"""
import asyncio
import sqlite3
import threading
import time

import pytest

from app import cancel, retrieval

#: a statement that never ends by itself — only an interrupt stops it
ENDLESS = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
           "SELECT count(*) FROM c")


def _in_thread(fn):
    out: dict = {}

    def body():
        try:
            out["value"] = fn()
        except BaseException as e:      # noqa: BLE001 — the test reads what it was
            out["error"] = e
    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t, out


# ---- the token -----------------------------------------------------------------------------

def test_cancelling_interrupts_a_statement_already_running(tmp_path):
    """Not at its next row: the statement never has one. `interrupt()` is
    made to be called from another thread, and this is that."""
    con = sqlite3.connect(tmp_path / "m.db", check_same_thread=False)
    token = cancel.Token()
    token.watch(con)
    t, out = _in_thread(lambda: con.execute(ENDLESS).fetchone())
    time.sleep(0.2)
    assert t.is_alive(), "the statement is running"
    started = time.monotonic()
    token.cancel()
    t.join(5)
    assert not t.is_alive(), "still running 5 s after its caller left"
    assert time.monotonic() - started < 2
    assert isinstance(out.get("error"), sqlite3.OperationalError)
    assert "interrupt" in str(out["error"])


def test_a_connection_watched_after_cancelling_is_interrupted_at_once(tmp_path):
    con = sqlite3.connect(tmp_path / "m.db", check_same_thread=False)
    token = cancel.Token()
    token.cancel()
    token.watch(con)                   # a leg that opened its connection too late
    t, out = _in_thread(lambda: con.execute(ENDLESS).fetchone())
    t.join(5)
    assert not t.is_alive() and isinstance(out.get("error"), sqlite3.OperationalError)


def test_no_except_exception_can_swallow_it():
    """Retrieval catches Exception on purpose ("an index problem empties the
    leg"). A Cancelled one of those caught would be logged as a failed leg,
    and the search would fuse and rerank for nobody."""
    token = cancel.Token()
    token.cancel()
    with pytest.raises(cancel.Cancelled):
        cancel.run(token, _a_leg_that_swallows_errors)


def _a_leg_that_swallows_errors():
    try:
        cancel.checkpoint()
    except Exception:                   # noqa: BLE001 — the pattern in retrieval.py
        return "swallowed"


def test_outside_a_cancellable_search_nothing_changes():
    """The worker, the CLI and tests call the same retrieval code with no
    token: checkpoint and watch are then no-ops."""
    assert cancel.current() is None
    cancel.checkpoint()
    assert cancel.watch("a connection") == "a connection"


def test_a_pool_thread_sees_the_token_only_when_it_is_carried():
    """A ThreadPoolExecutor thread does not inherit a ContextVar: without
    `carry`, every per-collection leg of a federated search ran on."""
    from concurrent.futures import ThreadPoolExecutor
    token = cancel.Token()
    token.cancel()

    def leg(_):
        cancel.checkpoint()
        return "ran"

    def search():
        with ThreadPoolExecutor(2) as ex:
            bare = list(ex.map(leg, [1, 2]))
            with pytest.raises(cancel.Cancelled):
                list(ex.map(cancel.carry(leg), [1, 2]))
        return bare

    assert cancel.run(token, search) == ["ran", "ran"], "uncarried: never told"


# ---- the search stops ----------------------------------------------------------------------

def _stubbed(monkeypatch, *, when_leg_runs=lambda: None, completed=None):
    enqueued: list = []
    monkeypatch.setattr(retrieval.refs, "get_store", lambda: None)
    monkeypatch.setattr(retrieval.refs.RESOLVER, "resolve", lambda q, s: [])

    def one_collection(con, c, q, k, mode, tr, entity_ids, plan=None, depth=0):
        when_leg_runs()
        cancel.checkpoint()
        if completed is not None:
            completed.append(c.name)       # got past a checkpoint it should have seen
        return {"catalog": [], "merged": [], "translations": {},
                "timings": {"catalog": 0.0}, "scope": {}}

    monkeypatch.setattr(retrieval, "_search_one_collection", one_collection)
    monkeypatch.setattr(retrieval, "_attach_work_fields", lambda con, c, hits, q: hits)
    monkeypatch.setattr(retrieval, "_attach_metadata_and_enqueue",
                        lambda con, c, hits: enqueued.append(c))
    monkeypatch.setattr(retrieval, "_rerank_budget_s", lambda depth=50: 0.4)
    monkeypatch.setattr(retrieval.db, "connect", lambda c=None: _Con())
    return enqueued


class _Con:
    def interrupt(self):
        pass

    def set_progress_handler(self, handler, n):
        pass


class _Col:
    def __init__(self, name):
        self.name, self.plugin = name, "gutenberg"


def test_a_cancelled_search_stops_and_enqueues_nothing(isolated_config, monkeypatch):
    """The lazy-embed enqueue WRITES; a search nobody is waiting for must
    not reach it."""
    token = cancel.Token()
    enqueued = _stubbed(monkeypatch, when_leg_runs=token.cancel)   # the caller leaves mid-leg
    with pytest.raises(cancel.Cancelled):
        cancel.run(token, retrieval.search, _Con(), "q", k=5, collection=_Col("a"),
                   translate=False)
    assert enqueued == []


def test_a_cancelled_federated_search_is_not_logged_as_a_failed_collection(
        isolated_config, monkeypatch):
    """No per-collection leg runs past its checkpoint once the caller has
    gone — which needs the token CARRIED into the pool thread: uncarried, a
    leg cannot see it and runs to the end.

    Not "every leg sees it": the first leg to raise ends `Executor.map`, and
    its `finally` cancels every leg not yet started, so those never run at
    all. That is the better outcome — no work for nobody — and a test that
    demanded both legs observe the token failed 30 times in 30."""
    token = cancel.Token()
    completed: list = []
    enqueued = _stubbed(monkeypatch, when_leg_runs=token.cancel, completed=completed)
    logged: list = []
    monkeypatch.setattr(retrieval.db, "log_event", lambda *a, **k: logged.append(a))
    with pytest.raises(cancel.Cancelled):
        cancel.run(token, retrieval.federated_search, [_Col("a"), _Col("b")], "q", k=5,
                   translate=False)
    assert completed == [], "a leg ran on after its caller had gone"
    assert logged == [], "a cancelled search is not a failed collection"
    assert enqueued == []


def test_a_search_left_alone_answers_as_before(isolated_config, monkeypatch):
    enqueued = _stubbed(monkeypatch)
    out = cancel.run(cancel.Token(), retrieval.search, _Con(), "q", k=5,
                     collection=_Col("a"), translate=False)
    assert out["hits"] == [] and len(enqueued) == 1


# ---- the endpoint notices ------------------------------------------------------------------

class _Caller:
    """A request whose caller disconnects after `stays` looks."""

    def __init__(self, stays: int) -> None:
        self.looks, self.stays = 0, stays

    async def is_disconnected(self) -> bool:
        self.looks += 1
        return self.looks > self.stays


def test_a_caller_who_leaves_stops_the_search(monkeypatch):
    monkeypatch.setattr(cancel, "POLL_S", 0.01)
    stopped = threading.Event()

    def a_long_search():
        try:
            while True:
                cancel.checkpoint()
                time.sleep(0.005)
        except cancel.Cancelled:
            stopped.set()
            raise

    async def ask():
        with pytest.raises(cancel.Cancelled):
            await cancel.while_connected(_Caller(stays=3), a_long_search)
        return await asyncio.to_thread(stopped.wait, 5)

    assert asyncio.run(ask()), "the search thread stopped at its next checkpoint"


def test_a_caller_who_leaves_is_said_in_the_log(monkeypatch, capsys):
    """uvicorn logs no line for a caller who left: a question that got no
    answer left no trace (München, 2026-10-27, 65 s of cold model loads)."""
    monkeypatch.setattr(cancel, "POLL_S", 0.01)

    def a_long_search():
        while True:
            cancel.checkpoint()
            time.sleep(0.005)

    async def ask():
        with pytest.raises(cancel.Cancelled):
            await cancel.while_connected(_Caller(stays=3), a_long_search)

    asyncio.run(ask())
    out = capsys.readouterr().out
    assert "the caller left after" in out and "the search is cancelled" in out


def test_a_caller_who_stays_gets_the_answer(monkeypatch):
    monkeypatch.setattr(cancel, "POLL_S", 0.01)

    def a_search(x, *, y):
        time.sleep(0.05)
        cancel.checkpoint()
        return x + y

    assert asyncio.run(cancel.while_connected(_Caller(stays=10_000), a_search, 2, y=3)) == 5
