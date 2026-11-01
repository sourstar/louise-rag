"""RAGE's bench (app/rage/bench.py): a collection's BEFORE/AFTER row from the
card record alone — busy, chunks a second, lost, gaps, and whether it ran
alone (docs/notes/gpu-saturation-plan.md, the testbench)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import stats
from app.rage import bench, card_record as cr
from tools import sched_report

PDEV = "0000:03:00.0"
URL = "http://localai:8080"
T0 = 1_762_000_000


@pytest.fixture(autouse=True)
def fresh():
    stats.recorder().reset()
    yield
    stats.recorder().reset()


def test_the_recorder_counts_what_was_embedded_since_start():
    r = stats.recorder()
    r.record_embed("wikidata", seconds=0.5, docs=1, chunks=3, chars=900)
    r.record_embed("wikidata", seconds=0.5, docs=2, chunks=2, chars=600)
    assert r.done() == {"wikidata": (3, 5)}
    r.reset()
    assert r.done() == {}


def test_each_second_records_the_work_done_in_it(tmp_path):
    done = {"wikidata": (0, 0)}
    t = [float(T0)]
    rec = cr.CardRecorder("u", tmp_path, gates=lambda: {}, feeders=lambda: {},
                          claim=lambda: False, done=lambda: dict(done),
                          fetch=lambda url, since: {"cards": {}}, wall=lambda: t[0])
    rec.tick()                                     # the baseline
    t[0] += 1
    done["wikidata"] = (2, 7)
    rec.tick()
    t[0] += 1
    rec.tick()                                     # nothing new
    rec.flush(final=True)
    rows = [json.loads(l) for f in tmp_path.glob("*.jsonl") for l in f.read_text().splitlines()]
    assert [r.get("done") for r in rows] == [None, {"wikidata": [2, 7]}, None]


def _write(out: Path, seconds: int, *, busy: float, chunks_per_s: int, others: dict | None = None,
           start: int = T0, backlog: bool = True) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with open(out / f"{cr._day(start)}.jsonl", "a") as fh:
        for i in range(seconds):
            served = {"wikipedia": 1.8, **(others or {})}
            fh.write(json.dumps({"t": start + i, "q": 0, "cards": {PDEV: busy},
                                 "gates": {URL: {"running": 2, "waiting": 0, "served": served}},
                                 "feed": {"wikipedia": {"in": 6, "backlog": int(backlog)}},
                                 "done": {"wikipedia": [1, chunks_per_s]}}) + "\n")


def test_a_window_alone_is_a_row_and_a_settings_measurement(tmp_path):
    _write(tmp_path, 900, busy=0.465, chunks_per_s=30)
    r = bench.row("wikipedia", T0, T0 + 899, out_dir=tmp_path)
    assert r.seconds == 900 and r.alone
    assert r.busy == pytest.approx(0.465)
    assert r.chunks_per_s == pytest.approx(30.0) and r.docs_per_s == pytest.approx(1.0)
    assert r.lost_per_min == pytest.approx(60 * 0.535, abs=0.05)
    assert r.gaps_per_min == 0.0 and r.waiting_s == 900
    assert "46.5% busy" in bench.text(r) and "alone" in bench.text(r)
    cmd = bench.record_command(r, batch=16, concurrency=2)
    assert cmd == ("louise hold rage-settings record wikipedia --batch 16 --concurrency 2 "
                   "--busy 0.465 --chunks-per-s 30.0 --window-s 900")


def test_a_window_shared_with_another_collection_is_not_its_row_to_record(tmp_path):
    _write(tmp_path, 900, busy=0.9, chunks_per_s=30, others={"wikidata": 1.2})
    r = bench.row("wikipedia", T0, T0 + 899, out_dir=tmp_path)
    assert not r.alone and r.others == {"wikidata": pytest.approx(1080.0)}
    assert "NOT alone" in bench.text(r)
    assert bench.record_command(r, batch=16, concurrency=2) is None
    assert "another collection" in bench.why_not_recorded(r)


def test_a_short_window_is_a_row_but_not_a_measurement(tmp_path):
    _write(tmp_path, 120, busy=0.8, chunks_per_s=10)
    r = bench.row("wikipedia", T0, T0 + 119, out_dir=tmp_path)
    assert r.alone and bench.record_command(r, batch=16, concurrency=4) is None
    assert "600 s" in bench.why_not_recorded(r)


def test_an_idle_card_with_work_waiting_counts_gaps(tmp_path):
    _write(tmp_path, 60, busy=0.01, chunks_per_s=0)
    r = bench.row("wikipedia", T0, T0 + 59, out_dir=tmp_path)
    assert r.gaps_per_min == 60.0 and r.lost_per_min == pytest.approx(59.4)


def test_nothing_recorded_says_so(tmp_path):
    r = bench.row("wikipedia", T0, T0 + 60, out_dir=tmp_path)
    assert r.seconds == 0 and "nothing recorded" in bench.text(r)


def test_the_window_is_written_as_the_log_writes_it():
    now = 1_762_000_000.0
    assert sched_report.parse_when("1762000000") == 1_762_000_000.0
    t = sched_report.parse_when("22:32", now=now)
    import datetime as dt
    d = dt.datetime.fromtimestamp(t)
    assert (d.hour, d.minute, d.second) == (22, 32, 0)
    assert sched_report.parse_when("2026-11-01T22:32:00+01:00") == \
        dt.datetime(2026, 11, 1, 21, 32, tzinfo=dt.timezone.utc).timestamp()


def test_the_command_prints_the_row(tmp_path, monkeypatch, capsys):
    _write(tmp_path, 900, busy=0.5, chunks_per_s=20)
    monkeypatch.setattr(cr, "directory", lambda: tmp_path)
    assert sched_report.main(["--bench", "wikipedia", "--since", str(T0),
                              "--until", str(T0 + 899), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["collection"] == "wikipedia" and out["chunks_per_s"] == pytest.approx(20.0)
    assert out["alone"] is True
