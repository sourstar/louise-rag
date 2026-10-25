"""Model work by priority (app/dispatch.py, docs/notes/scheduling.md §12).

The operator's ladder, lowest first: corpus embedding, translation, chat,
reranking, query embedding. Every request passes its server's gate, which
admits the server's capacity at a time — strictly by level, then fairly by
service time within a level. The node's interactive claim is held while any
interactive call runs; the GPU governor reads it (tests/test_gpu_governor.py).
"""
import threading
import time

import httpx
import pytest

from app import config, dispatch, worker
from app.dispatch import Level


@pytest.fixture(autouse=True)
def fresh_gates():
    dispatch.reset()
    yield
    dispatch.reset()


def _hold(gate, level, plugin, started: threading.Event, release: threading.Event, order: list):
    def run():
        with gate.admit(level, plugin, timeout=10):
            order.append((level, plugin))
            started.set()
            release.wait(10)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _queue(gate, level, plugin, order: list):
    def run():
        with gate.admit(level, plugin, timeout=10):
            order.append((level, plugin))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _waiting(gate, n):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if gate.snapshot()["waiting"] >= n:
            return
        time.sleep(0.01)
    raise AssertionError(f"{n} waiters never queued: {gate.snapshot()}")


def test_the_highest_level_goes_first_whatever_arrived_first():
    g = dispatch.gate("http://enc", 1)
    order, started, release = [], threading.Event(), threading.Event()
    holder = _hold(g, Level.CORPUS, "a", started, release, order)
    started.wait(5)
    waiters = [_queue(g, Level.CORPUS, "a", order)]
    _waiting(g, 1)
    waiters += [_queue(g, Level.TRANSLATE, "web", order)]
    _waiting(g, 2)
    waiters += [_queue(g, Level.QUERY, "web", order)]
    _waiting(g, 3)
    release.set()
    for t in [holder, *waiters]:
        t.join(5)
    assert [lvl for lvl, _ in order] == [Level.CORPUS, Level.QUERY, Level.TRANSLATE, Level.CORPUS]


def test_within_a_level_the_least_served_plugin_goes_first():
    """Fair by SERVICE TIME, not by request count: a plugin whose requests
    are long (gutenberg's 1,500-token chunks) does not take the share of
    one whose requests are short (pubmed's abstracts)."""
    g = dispatch.gate("http://enc", 1)
    with g.admit(Level.CORPUS, "long"):
        time.sleep(0.2)
    with g.admit(Level.CORPUS, "short"):
        pass
    order, started, release = [], threading.Event(), threading.Event()
    holder = _hold(g, Level.CORPUS, "long", started, release, order)
    started.wait(5)
    a = _queue(g, Level.CORPUS, "long", order)
    _waiting(g, 1)
    b = _queue(g, Level.CORPUS, "short", order)
    _waiting(g, 2)
    release.set()
    for t in (holder, a, b):
        t.join(5)
    assert [p for _, p in order] == ["long", "short", "long"]


def test_a_plugin_back_from_idle_banks_nothing():
    """Idle must not earn credit (§10): a plugin that was away starts level
    with the least-served plugin present, not at zero — or it would take the
    gate for as long as the others had been working."""
    g = dispatch.gate("http://enc", 1)
    with g.admit(Level.CORPUS, "busy"):
        time.sleep(0.2)
    order, started, release = [], threading.Event(), threading.Event()
    holder = _hold(g, Level.CORPUS, "busy", started, release, order)
    started.wait(5)
    back = _queue(g, Level.CORPUS, "back", order)
    _waiting(g, 1)
    assert g._served["back"] >= g._served["busy"] - 1e-9
    release.set()
    holder.join(5)
    back.join(5)


def test_an_unbounded_gate_orders_nothing_and_waits_for_nothing():
    g = dispatch.gate("http://chat", None)
    inside = threading.Event()
    with g.admit(Level.TRANSLATE, "web"):
        t = threading.Thread(target=lambda: g.admit(Level.QUERY, "web").__enter__() or inside.set())
        t.start()
        assert inside.wait(2), "an unbounded gate admitted at once"


def test_corpus_stands_aside_on_the_whole_card_while_a_person_s_query_runs(monkeypatch):
    """Operator, 2026-10-25: "Embedding shall have the lower priority and
    query processing the highest". Corpus work's next request, on ANY model,
    waits while the node's claim is held — then goes at once. (It did not
    from 2026-09-20: alone on the card the Arc dropped its clock and slowed
    chat; the GPU governor now holds the clock for the claim's lifetime.)"""
    claim_until = time.monotonic() + 0.3
    monkeypatch.setattr(dispatch, "interactive_active", lambda: time.monotonic() < claim_until)
    g = dispatch.gate("http://gpu-enc", 1)
    t0 = time.monotonic()
    with g.admit(Level.CORPUS, "normattiva", timeout=5):
        waited = time.monotonic() - t0
    assert 0.25 <= waited < 2, "it waited for the query, and not longer"
    for level in (Level.TRANSLATE, Level.QUERY, Level.RERANK, Level.CHAT):
        claim_until = time.monotonic() + 5
        t0 = time.monotonic()
        with g.admit(level, "web", timeout=1):
            assert time.monotonic() - t0 < 0.2, f"a query's {level.name.lower()} step never stands aside"


def test_standing_aside_is_bounded_and_a_deferral_and_wakes_on_stop(monkeypatch):
    monkeypatch.setattr(dispatch, "interactive_active", lambda: True)
    g = dispatch.gate("http://gpu-enc", 1)
    with pytest.raises(dispatch.GateTimeout, match="stood aside for a person's query") as err:
        with g.admit(Level.CORPUS, "normattiva", timeout=0.2):
            pass
    assert worker.transient_failure(err.value), "a deferral: nothing is written on the document"
    got = []

    def corpus():
        try:
            with g.admit(Level.CORPUS, "normattiva", timeout=60):
                got.append("admitted")
        except dispatch.GateTimeout:
            got.append("deferred")
    t = threading.Thread(target=corpus, daemon=True)
    t.start()
    time.sleep(0.2)
    dispatch.stop_all()
    t.join(2)
    assert got == ["deferred"], "SIGTERM wakes a corpus request standing aside"


def test_a_query_s_later_step_goes_first_at_a_shared_gate():
    """"The subsequent steps of a query in process shall be processed first
    to avoid wasting the effort spent for the previous steps": its answer,
    then its rerank, then its embedding, then its translation, then corpus."""
    g = dispatch.gate("http://shared", 1)
    started, release, order = threading.Event(), threading.Event(), []
    holder = _hold(g, Level.CORPUS, "a", started, release, order)
    started.wait(5)
    waiters = []
    for level in (Level.CORPUS, Level.TRANSLATE, Level.QUERY, Level.RERANK, Level.CHAT):
        waiters.append(_queue(g, level, "web" if level else "a", order))
        _waiting(g, len(waiters))
    release.set()
    for t in [holder, *waiters]:
        t.join(5)
    assert [lvl for lvl, _ in order] == [Level.CORPUS, Level.CHAT, Level.RERANK, Level.QUERY,
                                         Level.TRANSLATE, Level.CORPUS]


def _held(g):
    """A request holding the gate's one slot until released."""
    started, release = threading.Event(), threading.Event()
    _hold(g, Level.QUERY, "web", started, release, [])
    started.wait(5)
    return release


def test_a_wait_is_bounded_and_is_a_deferral_not_an_error():
    """CLAUDE.md: a pipeline thread never waits without a bound. The bound
    is the request's own timeout, and running out of it is the endpoint
    being busy — worker.transient_failure says so, so no error is ever
    written onto the document."""
    g = dispatch.gate("http://gpu-enc", 1)
    release = _held(g)
    try:
        with pytest.raises(dispatch.GateTimeout) as err:
            with g.admit(Level.CORPUS, "gutenberg", timeout=0.2):
                pass
    finally:
        release.set()
    assert isinstance(err.value, httpx.ConnectTimeout)
    assert worker.transient_failure(err.value)
    assert g.snapshot()["waiting"] == 0, "a waiter that gave up leaves the queue"


def test_stopping_wakes_every_waiter():
    """SIGTERM: a request not yet admitted is not a unit in flight."""
    g = dispatch.gate("http://gpu-enc", 1)
    release = _held(g)
    got = []

    def corpus():
        try:
            with g.admit(Level.CORPUS, "gutenberg", timeout=60):
                got.append("admitted")
        except dispatch.GateTimeout:
            got.append("deferred")
    t = threading.Thread(target=corpus, daemon=True)
    t.start()
    _waiting(g, 1)
    dispatch.stop_all()
    t.join(2)
    release.set()
    assert got == ["deferred"]


def test_a_level_crosses_into_embed_texts_pool_threads(monkeypatch):
    """A pool thread starts with an EMPTY context. Without handing the
    caller's over, a corpus request made from embed_texts' sub-batch pool
    would reach the gate as QUERY — ahead of every person's request."""
    from app import collections as collections_mod, embeddings
    seen = []
    lock = threading.Lock()

    def fake_batch(batch, timeout, recipe):
        with lock:
            seen.append(dispatch.current())
        return [[0.0] for _ in batch]
    monkeypatch.setattr(embeddings, "_embed_batch", fake_batch)
    monkeypatch.setattr(config, "EMBED_BATCH", 1)
    monkeypatch.setattr(config, "EMBED_CONCURRENCY", 3)
    recipe = collections_mod.EmbedRecipe(url="http://enc", api="openai", model="m",
                                         dtype="f16", ctx=512)
    with dispatch.working_as(Level.CORPUS, "gutenberg"):
        embeddings._embed_texts(["a", "b", "c", "d"], recipe=recipe)
    assert seen and set(seen) == {(Level.CORPUS, "gutenberg")}


def test_every_embedding_request_passes_its_servers_gate(monkeypatch):
    from app import collections as collections_mod, embeddings
    calls = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": 0, "embedding": [1.0]}]}

    def fake_post(url, json, timeout, headers=None):
        calls.append(dispatch.gate("http://enc").snapshot()["busy"])
        return Resp()
    monkeypatch.setattr(embeddings.httpc, "post", fake_post)
    recipe = collections_mod.EmbedRecipe(url="http://enc", api="openai", model="m",
                                         dtype="f16", ctx=512)
    embeddings._request_vectors(["x"], 5, recipe)
    assert calls == [1], "the request was made INSIDE the gate"


def test_the_claim_is_held_across_nested_calls_and_cleared_by_the_last(
        tmp_path, isolated_config):
    """Cleared when the last interactive call ends, not a TTL later — a TTL
    later would idle the encoders for nothing after every search."""
    from app import node_state
    node_state.connect().close()
    with dispatch.interactive(Level.CHAT):
        dispatch._claim_seen.clear()
        assert dispatch.interactive_active()
        with dispatch.interactive(Level.TRANSLATE):
            pass
        dispatch._claim_seen.clear()
        assert dispatch.interactive_active(), "the outer call is still running"
    dispatch._claim_seen.clear()
    assert not dispatch.interactive_active()


def test_a_claim_never_creates_the_node_store(tmp_path, isolated_config):
    """The worker makes the store at boot. A process without one has no
    worker to stand aside, and a claim that made one would write a node's
    live state (conftest's guard exists because two tests once did)."""
    from app import node_state
    assert not node_state.path().exists()
    with dispatch.interactive(Level.QUERY):
        pass
    assert not node_state.path().exists()
    assert dispatch.interactive_active() is False


def test_interactive_endpoints_are_bounded_only_by_configuration(monkeypatch):
    monkeypatch.setattr(config, "GATE_CAPACITY", "http://chat:11434=1, http://mt=2")
    assert dispatch.configured_capacity("http://chat:11434/") == 1
    assert dispatch.configured_capacity("http://mt") == 2
    assert dispatch.configured_capacity("http://rerank") is None


def test_a_streamed_answer_ends_cleanly_though_each_step_runs_in_another_context(
        isolated_config):
    """The Ask page's streamed answer ended with "[error: <Token … was created
    in a different Context]" (operator, 2026-09-19). `llm.chat_stream` holds
    `dispatch.call` across its `yield`s, and Starlette steps a generator in
    its threadpool with a FRESH COPY of the request's context for every
    step: the level was set in the first copy and reset in the last."""
    import contextvars

    from app import node_state
    node_state.connect().close()
    seen = []

    def stream():
        with dispatch.call("http://chat", Level.CHAT, timeout=1):
            seen.append(dispatch.current()[0])      # the request is made here, at CHAT
            yield "one"
            yield "two"

    g = stream()
    step = lambda: contextvars.copy_context().run(next, g)   # noqa: E731 — Starlette's way
    assert step() == "one" and step() == "two"
    assert dispatch.interactive_active()
    with pytest.raises(StopIteration):
        step()                                      # the block is left HERE, in another copy
    assert seen == [Level.CHAT]
    dispatch._claim_seen.clear()
    assert not dispatch.interactive_active(), "the claim was released"
    assert dispatch.gate("http://chat").snapshot()["busy"] == 0, "and the gate"


def test_a_block_left_in_its_own_context_still_restores_the_level():
    with dispatch.working_as(Level.CORPUS, "gutenberg"):
        with dispatch.working_as(Level.QUERY):
            assert dispatch.current() == (Level.QUERY, "")
        assert dispatch.current() == (Level.CORPUS, "gutenberg")
