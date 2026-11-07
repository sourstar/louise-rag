"""THE READ CACHE, one per process (app/backend.py `READ_CACHE`): every
reader's decoded units under ONE byte budget. Five readers kept five
budgets, 704 MiB of possible cache a process; a parse child serves several
collections, and eleven of them at ~1.05 GB RssAnon each put the worker over
its 24 GiB cap (München, 2026-11-07: OOM-killed at 03:23)."""
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from app import backend, config
from app.backend import GroupCache

ROOT = Path(__file__).resolve().parent.parent

#: the readers and the names their units carry
READERS = {
    "app.plugins.wikidata.pack": ("_groups", "wikidata pack"),
    "app.plugins.wikipedia.archive": ("_groups", "wikipedia archive"),
    "app.plugins.pubmed.pack": ("_groups", "pubmed pack"),
    "app.plugins.wikipedia.page_props": ("_groups", "wikipedia page_props"),
    "app.pack.records": ("_frames", "records frames"),
}


def test_two_readers_count_against_one_budget():
    c = GroupCache(100)
    a, b = c.reader("a"), c.reader("b")
    a.put(1, "a1", 40)
    b.put(1, "b1", 40)
    assert c.nbytes == 80 and len(c) == 2
    assert (a.nbytes, len(a), b.nbytes, len(b)) == (40, 1, 40, 1)
    assert a.get(1) == "a1" and b.get(1) == "b1", "the same key in two readers is two units"
    b.put(2, "b2", 40)                                    # 120 > 100: one goes
    assert c.nbytes == 80 <= c.budget and len(c) == 2
    assert a.budget == b.budget == c.budget == 100


def test_the_oldest_unit_across_readers_goes_first():
    c = GroupCache(100)
    a, b = c.reader("a"), c.reader("b")
    a.put("x", "ax", 30)
    b.put("x", "bx", 30)
    a.put("y", "ay", 30)
    b.get("x")                                            # b's x is now more recent than a's y
    b.put("z", "bz", 30)                                  # 120: a's x, the least recent, goes
    assert len(a) == 1 and len(b) == 2                    # order now: a:y, b:x, b:z
    a.get("y")                                            # order now: b:x, b:z, a:y
    a.put("w", "aw", 30)                                  # 120: b's x goes, though a put
    assert b.get("x") is None, "the least recent of either reader goes, whoever puts"
    assert a.get("y") == "ay" and a.get("w") == "aw" and b.get("z") == "bz"
    assert a.get("x") is None
    assert a.nbytes + b.nbytes == c.nbytes == sum(c.sizes()) == 90


def test_the_unit_just_read_is_kept_whatever_the_budget_and_whoever_holds_the_rest():
    c = GroupCache(0)
    a, b = c.reader("a"), c.reader("b")
    a.put(1, "a1", 10)
    assert b.put(1, "big", 500) == "big"
    assert len(c) == 1 and b.get(1) == "big" and a.get(1) is None and len(a) == 0


def test_a_reader_s_forget_and_clear_leave_the_other_s_units():
    c = GroupCache(1 << 20)
    a, b = c.reader("a"), c.reader("b")
    for i in range(3):
        a.put(("f", i), f"a{i}", 10)
        b.put(("f", i), f"b{i}", 10)
    a.forget(lambda k: k[0] == "f")                       # the reader's own key shape
    assert len(a) == 0 and a.nbytes == 0 and a.sizes() == []
    assert [b.get(("f", i)) for i in range(3)] == ["b0", "b1", "b2"] and b.nbytes == 30
    a.put(("g", 1), "ag", 10)
    b.clear()
    assert len(b) == 0 and b.nbytes == 0
    assert a.get(("g", 1)) == "ag" and c.nbytes == 10 and len(c) == 1


def test_a_reader_s_budget_is_the_shared_one():
    """tests/test_plugins_wikidata_families.py narrows wikidata's budget
    through its handle; the handle has no budget of its own to narrow."""
    c = GroupCache(100)
    a, b = c.reader("a"), c.reader("b")
    a.budget = 20
    assert c.budget == b.budget == 20


def test_release_runs_after_an_eviction_and_only_then():
    calls = []
    c = GroupCache(100, release=lambda: calls.append(1) or True)
    a, b = c.reader("a"), c.reader("b")
    a.put(1, "a1", 50)
    b.put(1, "b1", 50)                                    # 100: at the budget, nothing goes
    a.get(1)
    b.get(2)                                              # a miss
    a.put(1, "a1 again", 50)                              # a replacement, not an eviction
    assert calls == [], "no release on a get, nor on a put that dropped nothing"
    b.put(2, "b2", 50)                                    # 150: one goes
    assert calls == [1]
    b.forget(lambda k: True)
    a.clear()
    assert calls == [1], "forget and clear are not evictions"
    assert c.stats()["evictions"] == 1 and c.stats()["releases"] == 1


def test_a_release_that_fails_never_fails_the_put():
    def boom():
        raise RuntimeError("no")
    c = GroupCache(10, release=boom)
    r = c.reader("a")
    r.put(1, "one", 10)
    assert r.put(2, "two", 10) == "two" and r.get(2) == "two" and len(c) == 1


def test_release_arrow_memory_is_harmless_without_pyarrow_or_its_call(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyarrow", None)     # not importable
    assert backend.release_arrow_memory() is False
    monkeypatch.setitem(sys.modules, "pyarrow", types.SimpleNamespace())   # no default_memory_pool
    assert backend.release_arrow_memory() is False
    monkeypatch.setitem(sys.modules, "pyarrow", types.SimpleNamespace(
        default_memory_pool=lambda: types.SimpleNamespace()))               # a pool without the call
    assert backend.release_arrow_memory() is False

    def raising():
        raise OSError("mallctl")
    monkeypatch.setitem(sys.modules, "pyarrow", types.SimpleNamespace(
        default_memory_pool=lambda: types.SimpleNamespace(release_unused=raising)))
    assert backend.release_arrow_memory() is False
    called = []
    monkeypatch.setitem(sys.modules, "pyarrow", types.SimpleNamespace(
        default_memory_pool=lambda: types.SimpleNamespace(release_unused=lambda: called.append(1))))
    assert backend.release_arrow_memory() is True and called == [1]


def test_release_arrow_memory_with_the_real_pyarrow():
    pytest.importorskip("pyarrow")
    assert backend.release_arrow_memory() is True


def test_the_process_cache_releases_arrow_memory_and_has_the_configured_budget():
    assert backend.READ_CACHE._release is backend.release_arrow_memory
    assert backend.READ_CACHE.budget == config.READ_CACHE_BYTES


def test_every_reader_is_a_handle_on_the_one_process_cache():
    import importlib
    names = set()
    for module, (attr, name) in READERS.items():
        h = getattr(importlib.import_module(module), attr)
        assert isinstance(h, backend.CacheReader) and h.cache is backend.READ_CACHE, module
        assert h.name == name
        names.add(h.name)
    assert len(names) == len(READERS), "two readers under one name would forget each other's units"
    # and nothing in app/ makes a cache of its own beside it
    own = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
           if p.name != "backend.py" and re.search(r"\bGroupCache\(", p.read_text(encoding="utf-8"))]
    assert own == []


def test_a_child_takes_the_budget_from_the_environment_it_inherits():
    p = subprocess.run([sys.executable, "-c", "from app import backend; print(backend.READ_CACHE.budget)"],
                       cwd=ROOT, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin",
                                      "READ_CACHE_BYTES": str(32 << 20)},
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.strip() == str(32 << 20)


def test_the_stats_count_each_reader_s_hits_and_misses():
    c = GroupCache(100)
    a, b = c.reader("a"), c.reader("b")
    assert a.get(1) is None
    a.put(1, "a1", 10)
    a.get(1)
    a.get(1)
    b.get(1)
    got = c.stats()
    assert got["readers"]["a"] == {"bytes": 10, "units": 1, "hits": 2, "misses": 1}
    assert got["readers"]["b"] == {"bytes": 0, "units": 0, "hits": 0, "misses": 1}
    assert (got["bytes"], got["units"], got["budget"]) == (10, 1, 100)


def test_the_beat_logs_this_process_s_read_cache(monkeypatch):
    """Published by the stats beat's `_log_shares`, never by the reading
    thread (CLAUDE.md: telemetry is published by someone other than the
    thread doing the work)."""
    from app import db, worker
    c = GroupCache(256 << 20)
    monkeypatch.setattr(backend, "READ_CACHE", c)
    lines = []
    monkeypatch.setattr(db, "log_event", lambda con, level, stage, msg, *a, **k: lines.append((stage, msg)))
    monkeypatch.setattr(worker, "_last_shares_log", 0.0)
    worker._log_shares(None)
    assert not [m for _s, m in lines if m.startswith("read cache")], "nothing read, nothing to say"

    archive, frames = c.reader("wikipedia archive"), c.reader("records frames")
    archive.put(("shard", 0), "table", 49 << 20)
    archive.get(("shard", 0))
    frames.get(("part", 3))
    monkeypatch.setattr(worker, "_last_shares_log", 0.0)
    worker._log_shares(None)
    got = [(s, m) for s, m in lines if m.startswith("read cache")]
    assert len(got) == 1 and got[0][0] == "sched"
    msg = got[0][1]
    assert "49.0 of 256 MiB, 1 units" in msg
    assert "wikipedia archive: 49.0 MiB in 1 units, 1 hits / 0 misses" in msg
    assert "records frames: 0.0 MiB in 0 units, 0 hits / 1 misses" in msg
