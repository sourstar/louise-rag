"""Measured throughput, and the honesty rules around it.

The page exists to answer one question — is a GPU version of this encoder
worth running — so the numbers have to be comparable across collections and
have to distinguish "not observed" from "stopped". Both are easy to get
wrong in ways that look fine.
"""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, spaces, stats, web
from app.api import app


@pytest.fixture(autouse=True)
def fresh_recorder():
    stats.recorder().reset()
    yield
    stats.recorder().reset()


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


# ---------------- the recorder ----------------

def test_nothing_measured_reports_nothing_not_zero():
    """A zero and an absence look identical on a dashboard and mean opposite
    things: one says the encoder stalled, the other says nobody looked."""
    assert stats.recorder().snapshot() == {}
    assert stats.rates(None) == {}
    assert stats.rates({}) == {}


def test_a_single_sample_is_not_enough_to_publish():
    r = stats.recorder()
    r.record_embed("g", seconds=1.0, docs=1, chunks=10, chars=1000)
    assert r.snapshot() == {}, "one document's luck is not a rate"
    for _ in range(stats.MIN_EVENTS):
        r.record_embed("g", seconds=1.0, docs=1, chunks=10, chars=1000)
    assert "g" in r.snapshot()


def test_encoder_throughput_is_work_over_time_inside_the_call():
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("g", seconds=2.0, docs=1, chunks=5, chars=10_000)
    rates = stats.rates(r.snapshot()["g"])["embed"]

    assert rates["chars_per_s"] == pytest.approx(40_000 / 8.0)   # 5000
    assert rates["chunks_per_s"] == pytest.approx(20 / 8.0)
    assert rates["ms_per_chunk"] == pytest.approx(8000.0 / 20)
    assert rates["docs"] == 4 and rates["chunks"] == 20


def test_duty_separates_a_slow_encoder_from_a_contended_one():
    """The distinction the hardware decision turns on: an encoder at 20% duty
    is idle four fifths of the time, and buying a faster one changes nothing
    until whatever it is queueing behind gets faster."""
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("busy", seconds=0.5, docs=1, chunks=1, chars=100)
        time.sleep(0.01)
    entry = r.snapshot()["busy"]
    # busy time is real work; span is elapsed wall-clock across the samples
    assert entry["embed"]["busy_seconds"] == pytest.approx(2.0)
    assert entry["embed"]["span_seconds"] < entry["embed"]["busy_seconds"], \
        "this fixture reports more work than elapsed time — duty caps at 1 in reality"

    assert stats.rates(entry)["embed"]["duty"] is not None


def test_characters_not_chunks_is_the_cross_collection_unit():
    """Two encoders doing identical work, one with half the context window.
    Chunks/second ranks the small-context one twice as fast; chars/second
    correctly calls them equal."""
    r = stats.recorder()
    for _ in range(4):                       # big ctx: 1 chunk of 8000 chars
        r.record_embed("big", seconds=1.0, docs=1, chunks=1, chars=8000)
        r.record_embed("small", seconds=1.0, docs=1, chunks=2, chars=8000)
    snap = r.snapshot()
    big = stats.rates(snap["big"])["embed"]
    small = stats.rates(snap["small"])["embed"]

    assert big["chars_per_s"] == pytest.approx(small["chars_per_s"])
    assert small["chunks_per_s"] == pytest.approx(big["chunks_per_s"] * 2), \
        "chunks/s really does disagree — which is why it is not the headline"


def test_adopted_files_never_enter_the_fetch_rate():
    """A verify-only pass moves no bytes. Counting it would report an
    infinite download speed and poison every ETA on the page."""
    r = stats.recorder()
    for _ in range(4):
        r.record_fetch("g", seconds=0.0, bytes_moved=0)
    assert r.snapshot() == {}


def test_old_samples_fall_out_of_the_window(monkeypatch):
    r = stats.recorder()
    for _ in range(4):
        r.record_embed("g", seconds=1.0, docs=1, chunks=1, chars=100)
    assert "g" in r.snapshot()

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + stats.MAX_AGE_S + 60)
    assert r.snapshot() == {}, "a two-hour-old rate is not a current rate"


# ---------------- ETA ----------------

@pytest.mark.parametrize("remaining,rate", [
    (100, None),     # never measured
    (100, 0),        # measured as stopped
    (0, 5),          # nothing left
    (-5, 5),         # nonsense input
])
def test_eta_is_none_when_it_cannot_be_known(remaining, rate):
    assert stats.eta_seconds(remaining, rate) is None


def test_eta_divides_remaining_by_rate():
    assert stats.eta_seconds(1000, 10) == pytest.approx(100.0)


def test_durations_are_coarse_on_purpose():
    """An ETA from a rolling sample is accurate to a factor, not a minute."""
    assert web._human_duration(None) == "—"
    assert web._human_duration(45) == "45s"
    assert web._human_duration(600) == "10 min"
    assert web._human_duration(3600 * 5) == "5.0 h"
    assert web._human_duration(3600 * 24 * 10) == "10.0 days"
    assert "years" in web._human_duration(3600 * 24 * 900)


# ---------------- the page ----------------

def test_the_page_renders_with_no_measurements(client):
    r = client.get("/admin/stats")
    assert r.status_code == 200
    assert "no measurements yet" in r.text
    assert "gutenberg" in r.text


def test_the_page_shows_published_worker_measurements(client, isolated_config):
    r = stats.recorder()
    for _ in range(5):
        r.record_embed("gutenberg", seconds=1.0, docs=1, chunks=2, chars=6000)
    db.publish_worker_stats(db.connect(), r.snapshot())

    page = client.get("/admin/stats").text
    assert "measuring" in page
    assert "6000" in page or "6,000" in page.replace(",", ""), \
        "the measured chars/s must reach the page"


def test_stale_measurements_are_dropped(client, isolated_config):
    import json
    con = db.connect()
    old = time.time() - (db.WORKER_STATS_MAX_AGE_S + 60)
    con.execute("INSERT OR REPLACE INTO control(key,value) VALUES(?,?)",
                (db.WORKER_STATS_KEY,
                 json.dumps({"at": old, "payload": {"gutenberg": {}}})))
    payload, age = db.read_worker_stats(con)
    assert payload is None
    assert age > db.WORKER_STATS_MAX_AGE_S


def test_the_nav_offers_the_tab(client):
    assert 'href="/admin/stats"' in client.get("/admin/library").text


def test_the_page_requires_admin(client):
    del client.headers["X-Admin-Token"]
    r = client.get("/admin/stats", headers={"Accept": "application/json"})
    assert r.status_code == 401
