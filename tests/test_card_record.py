"""The worker's record of the card (app/card_record.py) and what feeds it:
the gates' held time (I2, app/dispatch.py), the feeders' backlog (I3,
app/stats.py, app/worker.py), the probe's busy samples (I1).

docs/notes/gpu-saturation-plan.md: a second is a GAP when the card was idle
while a collection had work it could send and no query held the claim.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from app import card_record as cr
from app import dispatch, stats, worker
from app.dispatch import Level

PDEV = "0000:03:00.0"
URL = "http://localai:8080"
T0 = 1_762_000_000          # 2025-11-01T12:26:40Z


@pytest.fixture(autouse=True)
def fresh():
    dispatch.reset()
    stats.recorder().reset()
    yield
    dispatch.reset()
    stats.recorder().reset()


# ---------------------------------------------------------------- I2: the gate

def test_a_gate_reports_what_each_plugin_really_held_running_requests_included():
    g = dispatch.gate("http://enc", 1)
    inside, release = threading.Event(), threading.Event()

    def hold():
        with g.admit(Level.CORPUS, "wikidata", timeout=5):
            inside.set()
            release.wait(5)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    inside.wait(5)
    time.sleep(0.2)
    running = g.snapshot()["plugins"]["wikidata"]
    assert running["running"] == 1 and running["held_s"] >= 0.2, "a running request counts"
    release.set()
    t.join(5)
    after = g.snapshot()["plugins"]["wikidata"]
    assert after["running"] == 0 and after["held_s"] >= running["held_s"]


def test_held_time_is_a_measurement_the_fair_share_account_never_raises():
    """`_served` is raised for a plugin that joins (it banks nothing while
    away); `held_s` is what it held, and joining holds nothing."""
    g = dispatch.gate("http://enc", 1)
    with g.admit(Level.CORPUS, "wikidata", timeout=5):
        time.sleep(0.15)
    inside, release = threading.Event(), threading.Event()

    def hold():
        with g.admit(Level.CORPUS, "wikidata", timeout=5):
            inside.set()
            release.wait(5)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    inside.wait(5)
    waiter = threading.Thread(target=lambda: g.admit(Level.CORPUS, "wikipedia", timeout=5)
                              .__enter__(), daemon=True)
    waiter.start()
    deadline = time.monotonic() + 5
    while g.snapshot()["waiting"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    snap = g.snapshot()["plugins"]
    assert g._served["wikipedia"] >= 0.15, "the fair-share account was raised on joining"
    assert snap["wikipedia"] == {"running": 0, "waiting": 1, "corpus": 1, "held_s": 0.0}
    release.set()
    t.join(5)


# ---------------------------------------------------------------- I3: the backlog

def test_the_recorder_says_which_feeders_have_work_waiting():
    r = stats.recorder()
    r.set_in_flight("wikidata", 4)
    r.set_backlog("wikidata", True)
    r.set_backlog("pubmed", True)
    r.set_backlog("pubmed", False)
    assert r.feeders() == {"wikidata": {"in_flight": 4, "backlog": True}}
    assert "backlog" not in r.snapshot().get("wikidata", {}), "the page's snapshot is unchanged"


def test_a_stream_with_candidates_left_has_work_waiting_and_one_run_dry_has_none(
        tmp_path, monkeypatch):
    from tests.test_embed_producers import RECIPE, _collection, _feed_once
    from app import embeddings
    c, con = _collection(tmp_path)
    _feed_once(monkeypatch, con)
    seen = []
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])

    def upsert(*a, **k):
        seen.append(stats.recorder().feeders().get(c.name, {}).get("backlog"))
        return 1
    monkeypatch.setattr(embeddings, "upsert_book", upsert)
    assert worker._embed_parallel(con, c, RECIPE, slots=2)
    assert seen and seen[0] is True, "while its candidates are sent, work is waiting"
    assert c.name not in stats.recorder().feeders(), "the selection ran dry: nothing waits"
    assert not worker._embed_parallel(con, c, RECIPE, slots=2)
    assert c.name not in stats.recorder().feeders()


def test_a_stream_that_ends_at_its_deadline_still_has_work_waiting(tmp_path, monkeypatch):
    from tests.test_embed_producers import RECIPE, _collection
    from app import config, embeddings
    c, con = _collection(tmp_path)
    rows = con.execute("SELECT rowid AS rid, * FROM documents").fetchall()
    monkeypatch.setattr(worker, "_embed_candidates", lambda *a: rows)   # never runs dry
    monkeypatch.setattr(embeddings, "held_chunks", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])
    monkeypatch.setattr(embeddings, "upsert_book", lambda *a, **k: 1)
    monkeypatch.setattr(config, "EMBED_STREAM_S", 0.0)
    worker._embed_parallel(con, c, RECIPE, slots=2)
    assert stats.recorder().feeders()[c.name]["backlog"] is True, \
        "the gap until the next stream is the engine's"


# ---------------------------------------------------------------- the recorder

class Fake:
    def __init__(self) -> None:
        self.t = float(T0)
        self.held = 0.0
        self.claim = False
        self.backlog = True
        self.samples: list[dict] = []
        self.fail = False
        self.asked: list[float | None] = []

    def gates(self) -> dict:
        return {URL: {"busy": 1, "waiting": 0,
                      "plugins": {"wikidata": {"running": 1, "waiting": 0, "corpus": 0,
                                               "held_s": self.held}}}}

    def feeders(self) -> dict:
        return {"wikidata": {"in_flight": 4, "backlog": self.backlog}}

    def fetch(self, url: str, since: float | None) -> dict:
        self.asked.append(since)
        if self.fail:
            raise ConnectionError("probe down")
        return {"period_s": 1.0, "cards": {PDEV: [s for s in self.samples
                                                  if since is None or s["t"] > since]}}


@pytest.fixture
def rig(tmp_path):
    fake = Fake()
    rec = cr.CardRecorder("http://gpu-probe:8080", tmp_path / "card", gates=fake.gates,
                          feeders=fake.feeders, claim=lambda: fake.claim, fetch=fake.fetch,
                          wall=lambda: fake.t)
    return fake, rec, tmp_path / "card"


def _lines(out: Path) -> list[dict]:
    return [json.loads(l) for f in sorted(out.glob("*.jsonl")) for l in f.read_text().splitlines()]


def test_each_second_joins_the_cards_busy_with_who_held_the_gate(rig):
    fake, rec, out = rig
    for i, busy in enumerate((0.5, 0.9, 0.95)):
        fake.held += 0.97
        rec.tick()
        fake.samples.append({"t": fake.t + 0.4, "dt": None if i == 0 else 1.0, "busy": busy})
        fake.t += 1
    assert rec.flush() == 3
    rows = _lines(out)
    assert [r["t"] for r in rows] == [T0, T0 + 1, T0 + 2]
    assert rows[0]["cards"] is None, "the probe's first sample is a baseline, no interval"
    assert [r["cards"] for r in rows[1:]] == [{PDEV: 0.9}, {PDEV: 0.95}]
    assert rows[0]["gates"][URL]["served"] == {}, "the first tick only sets the gate's baseline"
    assert rows[1]["gates"][URL] == {"running": 1, "waiting": 0, "served": {"wikidata": 0.97}}
    assert rows[1]["feed"] == {"wikidata": {"in": 4, "backlog": 1}}
    assert (out / "2025-11-01.jsonl").is_file()


def test_a_second_waits_for_its_card_figure_then_is_written_without_it(rig):
    fake, rec, out = rig
    rec.tick()
    fake.t += 1
    assert rec.flush() == 0, "the probe has not answered for it yet"
    fake.t += cr.LATE_S + 1
    assert rec.flush() == 1
    assert _lines(out)[0]["cards"] is None


def test_a_probe_that_is_down_never_stops_the_record(rig):
    fake, rec, out = rig
    fake.fail = True
    rec.tick()
    fake.t += 1
    assert rec.flush(final=True) == 1
    assert rec.probe_error.startswith("ConnectionError")
    assert _lines(out)[0]["q"] == 0


def test_the_probe_is_asked_only_for_what_is_new(rig):
    fake, rec, _ = rig
    rec.tick()
    fake.samples.append({"t": fake.t + 0.4, "dt": 1.0, "busy": 0.5})
    fake.t += 1
    rec.flush()
    rec.flush()
    assert fake.asked[0] == pytest.approx(T0 + 1 - 2 * cr.FLUSH_S), "not the probe's whole hour"
    assert fake.asked[1] == pytest.approx(T0 + 0.4)


def test_two_ticks_in_one_second_add_their_served_time():
    a = {"t": 1, "q": 0, "gates": {URL: {"running": 1, "waiting": 2, "served": {"w": 0.4}}},
         "feed": {"w": {"in": 2, "backlog": 0}}}
    b = {"t": 1, "q": 1, "gates": {URL: {"running": 3, "waiting": 0, "served": {"w": 0.5, "p": 0.1}}},
         "feed": {"w": {"in": 1, "backlog": 1}}}
    m = cr._merge(a, b)
    assert m["q"] == 1
    assert m["gates"][URL] == {"running": 3, "waiting": 2, "served": {"w": 0.9, "p": 0.1}}
    assert m["feed"] == {"w": {"in": 2, "backlog": 1}}
    assert a["gates"][URL]["served"] == {"w": 0.4}, "the inputs are not changed"


def test_old_days_are_removed(rig, tmp_path):
    fake, rec, out = rig
    out.mkdir(parents=True)
    (out / "2025-10-20.jsonl").write_text("{}\n")
    (out / "2025-10-31.jsonl").write_text("{}\n")
    rec.tick()
    rec.flush(final=True)
    assert sorted(f.name for f in out.glob("*.jsonl")) == ["2025-10-31.jsonl", "2025-11-01.jsonl"]


def test_run_ticks_flushes_and_writes_what_is_left_on_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "TICK_S", 0.01)
    fake = Fake()
    rec = cr.CardRecorder("u", tmp_path, gates=fake.gates, feeders=fake.feeders,
                          claim=lambda: False, fetch=fake.fetch)
    stop = threading.Event()
    t = threading.Thread(target=rec.run, args=(stop,))
    t.start()
    time.sleep(0.15)
    stop.set()
    t.join(5)
    assert not t.is_alive() and _lines(tmp_path), "the final flush wrote the pending seconds"


# ---------------------------------------------------------------- reading it

def _row(t: int, busy: float | None, *, q: int = 0, backlog: int = 1, served: float = 0.0):
    row = {"t": t, "q": q, "cards": None if busy is None else {PDEV: busy, "0000:07:00.0": 0.0}}
    if backlog:
        row["feed"] = {"wikidata": {"in": 4, "backlog": backlog}}
    if served:
        row["gates"] = {URL: {"running": 1, "waiting": 0, "served": {"wikidata": served}}}
    return row


def test_a_minute_says_busy_gaps_lost_and_who_held_the_card():
    start = T0 - T0 % 60
    rows = ([_row(start + i, 0.9, served=1.0) for i in range(50)]
            + [_row(start + 50 + i, 0.0) for i in range(5)]            # idle, work waiting: gaps
            + [_row(start + 55 + i, 0.0, q=1) for i in range(3)]       # a query: not a gap
            + [_row(start + 58, 0.0, backlog=0), _row(start + 59, None)])
    (w,) = cr.summarize(rows)
    assert w.start == start and w.seconds == 60 and w.card_seconds == 59
    assert w.busy == pytest.approx(45 / 59)
    assert (w.query, w.waiting, w.gaps) == (3, 56, 5)
    assert w.lost == pytest.approx(50 * 0.1 + 5 * 1.0)
    assert w.served == {"wikidata": pytest.approx(50.0)}


def test_a_card_can_be_picked_and_minutes_are_kept_apart():
    start = T0 - T0 % 60
    rows = [_row(start, 0.8), _row(start + 60, 0.4)]
    assert [w.busy for w in cr.summarize(rows)] == [0.8, 0.4]
    assert [w.busy for w in cr.summarize(rows, card="0000:07:00.0")] == [0.0, 0.0]
    assert cr.summarize(rows, minute=False)[0].busy == pytest.approx(0.6)


def test_read_keeps_the_window_and_skips_a_torn_line(tmp_path):
    (tmp_path / "2025-11-01.jsonl").write_text(
        "\n".join(json.dumps(_row(T0 + i, 0.5)) for i in range(5)) + "\n{\"t\": 17")
    got = [r["t"] for r in cr.read(T0 + 1, T0 + 3, tmp_path)]
    assert got == [T0 + 1, T0 + 2, T0 + 3]


def test_schedule_report_prints_the_card(tmp_path, monkeypatch, capsys):
    from tools import sched_report
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    (tmp_path / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(_row(now - 30 + i, 0.92, served=1.0)) for i in range(20)) + "\n")
    monkeypatch.setattr(cr, "directory", lambda: tmp_path)
    sched_report.gpu(5)
    out = capsys.readouterr().out
    assert "busy  92.0%" in out and "wikidata 20" in out and "gaps 0.0 s/min" in out
