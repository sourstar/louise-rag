"""The CPU applier (app/cpu_limit.py): the node profile's `cpu_cores` applied
to the parse pools — at most that many parts in flight, 0 a pause at the unit
boundary, an unreadable profile said and the last limit kept
(docs/notes/node-profiles-design.md, operator 2026-10-27)."""
from __future__ import annotations

import shutil
import threading
import time
from concurrent.futures import Future
from datetime import datetime
from pathlib import Path

import pytest

from app import collections as collections_mod, config, cpu_limit, db, discovery, node_profile, parallel, worker
from app.node_profile import Active, GpuLevel, Profile

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EIGHT = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)


def _active(cores, name=None, until=None):
    if cores == "none":
        return None
    name = name or ("quiet" if cores == 0 else "day")
    return Active(profile=Profile(name, cores, "full"), level=GpuLevel("full"), until=until, next=None)


class Script:
    """node_profile.active, scripted: the n-th read answers values[n] (the
    last one for ever after); an exception in the list is raised."""

    def __init__(self, *values):
        self.values, self.calls, self.now = list(values), 0, None

    def __call__(self, now=None):
        v = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(v, Exception):
            raise v
        self.now = v.profile.cpu_cores if v is not None else None
        return v


@pytest.fixture
def fast(monkeypatch):
    """Every look re-reads the profile, and a pause looks every 5 ms."""
    monkeypatch.setattr(cpu_limit, "TTL_S", 0.0)
    monkeypatch.setattr(cpu_limit, "PAUSE_TICK_S", 0.005)


def _script(monkeypatch, *values) -> Script:
    s = Script(*values)
    monkeypatch.setattr(node_profile, "active", s)
    return s


def _gutenberg(tmp_path, corpus_dir, name="g"):
    d = tmp_path / name
    c = collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus_dir,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    rows = [dict(r) for r in con.execute("SELECT * FROM documents ORDER BY uid")]
    con.close()
    return c, rows


class TimedPool:
    """Parts answered by a thread a few ms after submission. Counts the parts
    in flight at each submission, and what the profile said then."""

    def __init__(self, script: Script | None = None, delay: float = 0.003,
                 hold_until_peak: int | None = None, hold_s: float = 0.25):
        self.script, self.delay = script, delay
        self.live = self.peak = 0
        self.lock = threading.Lock()
        self.submitted_under: list = []
        # `hold_until_peak`: no part answers until more than that many are in
        # flight (or `hold_s` passes) — so a window's size is measured by what
        # the stream SUBMITS, not by whether its loop outruns a 3 ms answer on
        # a loaded machine (the node, 2026-11-01: peak 5 under a records build)
        self.hold_until_peak = hold_until_peak
        self.hold_s = hold_s
        self.opened = threading.Event()
        if hold_until_peak is None:
            self.opened.set()

    def submit(self, _fn, kind, collection, part):
        f = Future()
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.submitted_under.append(self.script.now if self.script else None)
            if self.hold_until_peak is not None and self.peak > self.hold_until_peak:
                self.opened.set()

        def answer():
            self.opened.wait(self.hold_s)
            time.sleep(self.delay)
            got = parallel._compute(kind, collection, part)
            with self.lock:
                self.live -= 1
            f.set_result(got)
        threading.Thread(target=answer, daemon=True).start()
        return f


# ---- the limit itself -------------------------------------------------------------------------

def test_the_profile_is_read_once_per_ttl_never_per_call(monkeypatch):
    """A COST test: a call per page, per unit, per pipeline round must not
    parse platform.toml each time (CLAUDE.md: no cost scales with the corpus)."""
    s = _script(monkeypatch, _active(8))
    for _ in range(10_000):
        assert cpu_limit.current().cores == 8
    assert s.calls == 1


def test_the_cache_ends_at_the_profile_s_own_boundary(monkeypatch):
    """quiet ends at 08:00: the limit is re-read then, not up to a TTL late."""
    soon = datetime.fromtimestamp(time.time() + 0.05)
    s = _script(monkeypatch, _active(0, until=soon), _active(8))
    assert cpu_limit.current().cores == 0
    time.sleep(0.08)
    assert cpu_limit.current().cores == 8 and s.calls == 2


def test_each_change_is_said_once_naming_the_profile(monkeypatch, fast):
    _script(monkeypatch, _active(0, until=EIGHT), _active(0, until=EIGHT), _active(8), _active(8), _active("none"))
    said: list[str] = []
    w = cpu_limit.Watch(said.append)
    for _ in range(5):
        w.limit()
    assert said == ["profile quiet: paused until 08:00", "profile day: 8 cores", "no profile: no core limit"]
    quiet = cpu_limit.Watch(said.append)
    monkeypatch.setattr(node_profile, "active", Script(None))
    quiet.limit()
    assert said[-1] == "no profile: no core limit", "a node with no profile says nothing at first"


def test_a_bad_platform_toml_is_said_and_the_last_limit_kept(tmp_path, monkeypatch, fast):
    """Deferral, not failure: a typo in the file must never stop a build."""
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    good = '[schedule]\nprofile = "else day"\n[profiles.day]\ncpu_cores = 3\n'
    (tmp_path / "platform.toml").write_text(good)
    said: list[str] = []
    w = cpu_limit.Watch(said.append)
    assert w.limit().cores == 3
    (tmp_path / "platform.toml").write_text('[schedule]\nprofile = "else nowhere"\n[profiles.day]\n')
    got = w.limit()
    assert got.cores == 3 and got.profile == "day" and "ProfileError" in got.error
    assert said[-1].startswith("profile unreadable (ProfileError") and said[-1].endswith("keeping profile day: 3 cores")
    w.limit()
    assert len(said) == 2, "said once, not at every look"
    (tmp_path / "platform.toml").write_text("not toml [")
    assert w.limit().cores == 3
    (tmp_path / "platform.toml").write_text(good)
    assert w.limit().error is None and said[-1] == "profile day: 3 cores"


def test_a_bad_profile_before_any_good_one_limits_nothing(monkeypatch, fast):
    _script(monkeypatch, OSError("unreadable"))
    got = cpu_limit.current()
    assert got.cores is None and got.error == "OSError: unreadable"


# ---- the pools -------------------------------------------------------------------------------

def test_in_flight_never_exceeds_the_profile_s_cores(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """Six children, a profile of two cores: at most two parts in flight, in
    the page stream and in a worker batch, and the same results."""
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 40)[:120]]
    pages = [rows[i:i + 24] for i in range(0, 120, 24)]
    truth = [parallel.compute("chunks", c, p) for p in pages]             # serial, unlimited
    s = _script(monkeypatch, _active(2))
    pool = TimedPool(s)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 6)
    monkeypatch.setattr(parallel, "pool", lambda: pool)
    assert list(parallel.compute_pages("chunks", c, iter(pages))) == truth
    assert 1 < pool.peak <= 2, f"{pool.peak} parts in flight under a profile of 2 cores"
    pool.peak = 0
    assert parallel.compute("chunks", c, rows) == [r for p in truth for r in p]
    assert 1 < pool.peak <= 2
    assert parallel.held() == {} and parallel.wanting() == {}, "every slot given back"
    # and without a limit the window is the pool's own, as before
    monkeypatch.setattr(node_profile, "active", Script(None))
    cpu_limit.reset()
    pool.peak = 0
    assert list(parallel.compute_pages("chunks", c, iter(pages))) == truth
    assert pool.peak > 2


def test_a_profile_allowing_every_child_keeps_a_part_queued_behind_each(
        isolated_config, tmp_path, corpus_dir, monkeypatch):
    """The profile bounds the parts COMPUTING. Clamped to the cores, a
    window of one part per child left each child that finished idle until
    the stream ran again — suspended while the build works on a page:
    wikidata-bge's held build, 12 children under a 12-core night profile,
    children ~32 % busy, the build's process 76 % idle (München,
    2026-10-31). With cores ≥ children the window keeps a part queued
    behind each child; the results are the same."""
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 40)[:120]]
    pages = [rows[i:i + 24] for i in range(0, 120, 24)]
    truth = [parallel.compute("chunks", c, p) for p in pages]
    s = _script(monkeypatch, _active(6))
    pool = TimedPool(s, hold_until_peak=6)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 6)
    monkeypatch.setattr(parallel, "pool", lambda: pool)
    assert list(parallel.compute_pages("chunks", c, iter(pages))) == truth
    assert pool.peak > 6, f"{pool.peak} parts in flight for 6 children under 6 cores"
    assert parallel.held() == {} and parallel.wanting() == {}


def test_a_pause_submits_nothing_finishes_what_is_in_flight_and_resumes(
        isolated_config, tmp_path, corpus_dir, monkeypatch, fast):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 40)[:120]]
    pages = [rows[i:i + 24] for i in range(0, 120, 24)]
    truth = [parallel.compute("chunks", c, p) for p in pages]
    s = _script(monkeypatch, _active(3), _active(3), *[_active(0, until=EIGHT)] * 6, _active(3))
    pool = TimedPool(s)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(parallel, "pool", lambda: pool)
    said: list[str] = []
    assert list(parallel.compute_pages("chunks", c, iter(pages), say=said.append)) == truth
    assert 0 not in pool.submitted_under, "a part was submitted while the profile paused the pool"
    assert said == ["profile day: 3 cores", "profile quiet: paused until 08:00", "profile day: 3 cores"]
    assert s.calls >= 8, "the pause looked at the profile until it allowed cores again"


def test_a_stop_during_a_pause_ends_the_stream_promptly(isolated_config, tmp_path, corpus_dir, monkeypatch, fast):
    c, rows = _gutenberg(tmp_path, corpus_dir)
    pages = [[dict(r) for r in rows] for _ in range(4)]
    _script(monkeypatch, _active(2), _active(0))
    stop = threading.Event()
    stream = parallel.compute_pages("chunks", c, iter(pages), stop=stop)
    got = [next(stream)]                                   # serial here; then paused
    threading.Timer(0.05, stop.set).start()
    t0 = time.monotonic()
    with pytest.raises(parallel.Interrupted):
        for out in stream:
            got.append(out)
    assert time.monotonic() - t0 < 2
    # the page already read when the pause came is finished and handed over;
    # nothing after it was started
    assert len(got) == 2 and all(len(p) == 3 for p in got)
    assert parallel.held() == {} and parallel.wanting() == {}


def test_the_profile_holds_back_prefetch_under_a_limit(isolated_config, monkeypatch):
    """Fire-and-forget work is outside every window, so it cannot be counted
    against the limit: it is not sent while one binds."""
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(parallel, "pool", lambda: object())
    _script(monkeypatch, _active(2))
    assert parallel.prefetch_allowed() is False
    cpu_limit.reset()
    monkeypatch.setattr(node_profile, "active", Script(_active(12)))
    assert parallel.prefetch_allowed() is True


# ---- the worker ------------------------------------------------------------------------------

def test_the_pipeline_pauses_its_parse_stages_and_resumes(tmp_path, monkeypatch):
    """rdf and fts stand down while the profile says 0, at the unit boundary,
    and the collection's log says so; they resume when it allows cores."""
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    monkeypatch.setattr(cpu_limit, "TTL_S", 0.0)
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURES / "gutenberg-mini" / "corpus", corpus)
    c = collections_mod.Collection(
        name="alpha", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus,
        manifest_path=tmp_path / "alpha" / "manifest.db", fts_dir=tmp_path / "alpha" / "fts",
        lance_dir=tmp_path / "alpha" / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    state = {"cores": 0}
    monkeypatch.setattr(node_profile, "active", lambda now=None: _active(state["cores"], until=EIGHT))
    gone = threading.Event()
    thread = threading.Thread(target=worker._pipeline_worker, args=(c, gone))
    thread.start()
    try:
        time.sleep(0.5)
        assert con.execute("SELECT COALESCE(SUM(rdf_done),0) FROM documents").fetchone()[0] == 0, \
            "rdf ran while the profile paused the parse pool"
        state["cores"] = 8
        deadline = time.time() + 30
        while time.time() < deadline:
            row = con.execute("SELECT SUM(rdf_done) r, SUM(fts_done) f FROM documents").fetchone()
            if (row["r"], row["f"]) == (3, 3):
                break
            time.sleep(0.1)
        assert (row["r"], row["f"]) == (3, 3), "the pipeline did not resume"
    finally:
        gone.set()
        thread.join(timeout=10)
    said = [r[0] for r in con.execute("SELECT message FROM events WHERE stage='worker' ORDER BY id")]
    assert "profile quiet: paused until 08:00" in said and "profile day: 8 cores" in said
    assert said.index("profile quiet: paused until 08:00") < said.index("profile day: 8 cores")


def test_the_main_loop_step_embeds_only_while_paused(tmp_path, monkeypatch):
    """The rollback path (`_run_one_step` with no pipeline thread) obeys the
    same pause: no rdf, and no false "caught up"."""
    corpus = tmp_path / "corpus"
    shutil.copytree(FIXTURES / "gutenberg-mini" / "corpus", corpus)
    c = collections_mod.Collection(
        name="alpha", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus,
        manifest_path=tmp_path / "alpha" / "manifest.db", fts_dir=tmp_path / "alpha" / "fts",
        lance_dir=tmp_path / "alpha" / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _script(monkeypatch, _active(0, until=EIGHT))
    assert worker._run_one_step(con, c, {}, pipeline_active=False) is False
    assert con.execute("SELECT COALESCE(SUM(rdf_done),0) FROM documents").fetchone()[0] == 0
    said = [r[0] for r in con.execute("SELECT message FROM events ORDER BY id")]
    assert not any("caught up" in m for m in said), said
    cpu_limit.reset()
    monkeypatch.setattr(node_profile, "active", Script(_active(8)))
    assert worker._run_one_step(con, c, {}, pipeline_active=False) is True
    assert con.execute("SELECT SUM(rdf_done) FROM documents").fetchone()[0] > 0


def test_the_stream_looks_at_the_profile_once_per_page_never_per_document(
        isolated_config, tmp_path, corpus_dir, monkeypatch, fast):
    """A COST test: even with no cache at all, a page stream reads the
    profile at its start and once per page — 120 documents, 5 pages, 6 reads."""
    c, rows = _gutenberg(tmp_path, corpus_dir)
    rows = [dict(r) for r in (rows * 40)[:120]]
    pages = [rows[i:i + 24] for i in range(0, 120, 24)]
    s = _script(monkeypatch, _active(8))
    monkeypatch.setattr(config, "PARSE_PROCESSES", 4)
    monkeypatch.setattr(parallel, "pool", lambda: TimedPool())
    assert len(list(parallel.compute_pages("chunks", c, iter(pages)))) == 5
    assert s.calls == 1 + len(pages)
    s.calls = 0
    parallel.compute("chunks", c, rows)
    assert s.calls == 1, "a worker batch reads it once"
