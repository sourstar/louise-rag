"""The Library card's p2p line is computed off the request path (app/web.py
`_p2p_summary`). On it, every bundle gutenberg accepted made the next view
recompute coverage over 1,639 parts — 6-14 s each, every 1-3 min, seven
views stacked on the same rebuild (München, 2026-10-27)."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app import web


@pytest.fixture
def slow(monkeypatch):
    calls: list[float] = []
    gate = threading.Event()

    def compute(collection):
        calls.append(time.time())
        gate.wait(5)
        return {"parts": len(calls)}

    monkeypatch.setattr(web, "_p2p_compute", compute)
    monkeypatch.setattr(web, "_p2p_cache", {})
    monkeypatch.setattr(web, "_p2p_refreshing", {})
    monkeypatch.setattr(web, "_COUNTS_COLD_WAIT_S", 0.2)
    c = SimpleNamespace(name="g", manifest_path="/x/g/state/manifest.sqlite",
                        codex=SimpleNamespace(space=object()))
    return c, calls, gate


def test_a_stale_value_is_served_at_once_and_refreshed_once_behind_it(slow):
    c, calls, gate = slow
    web._p2p_cache[c.manifest_path] = (time.time() - web._P2P_TTL_S - 1, {"parts": 0})
    t0 = time.perf_counter()
    got = [web._p2p_summary(c) for _ in range(7)]           # seven views at once
    assert time.perf_counter() - t0 < 0.5, "never waits for the computation"
    assert got == [{"parts": 0}] * 7
    gate.set()
    for _ in range(100):
        if web._p2p_cache[c.manifest_path][1] == {"parts": 1}:
            break
        time.sleep(0.02)
    assert len(calls) == 1, "one refresh in flight, not one per view"
    assert web._p2p_summary(c) == {"parts": 1}


def test_a_cold_cache_waits_a_bounded_time_then_renders_without_the_line(slow):
    c, calls, gate = slow
    t0 = time.perf_counter()
    assert web._p2p_summary(c) is None
    assert time.perf_counter() - t0 < 1.0
    gate.set()


def test_no_codex_no_line_and_no_work(slow):
    c, calls, gate = slow
    assert web._p2p_summary(SimpleNamespace(name="x", manifest_path="/y", codex=None)) is None
    assert calls == []
