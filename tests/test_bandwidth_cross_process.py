"""The Network page's rates must include the process that moves the bytes.

A token bucket is per-process state. Acquisition and seeding run in the
WORKER; the page is served by the WEB process. Reporting only the web
process's buckets meant a node pulling 80 GB showed a flat zero line next
to a saturated link — technically true of the process asked, and useless.

The worker publishes its rates through the control table (the same channel
the bandwidth override already travels down, in the other direction) and
the web process sums them in.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, spaces, web
from app.api import app


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_published_rates_round_trip(isolated_config):
    con = db.connect()
    assert db.read_worker_rates(con) == (None, None), "nothing published yet"

    db.publish_worker_rates(con, {"down": {"backfill": 5.0}, "up": {}})
    rates, age = db.read_worker_rates(con)
    assert rates == {"down": {"backfill": 5.0}, "up": {}}
    assert age is not None and age < 5


def test_a_stale_sample_is_dropped_not_carried_forward(isolated_config):
    """A frozen reading on a live graph reads as traffic that is not
    happening — worse than an honest gap."""
    con = db.connect()
    old = time.time() - (db.WORKER_RATES_MAX_AGE_S + 60)
    db.set_control(con, db.WORKER_RATES_KEY,
                   json.dumps({"at": old, "rates": {"down": {"backfill": 900.0}}}))

    rates, age = db.read_worker_rates(con)
    assert rates is None, "past max age the sample is not believable"
    assert age > db.WORKER_RATES_MAX_AGE_S, "but the age is still reported"


def test_a_malformed_sample_never_raises(isolated_config):
    con = db.connect()
    for junk in ("not json", "[]", '{"at": "soon"}', '{"rates": {}}',
                 '{"at": 1, "rates": "nope"}'):
        db.set_control(con, db.WORKER_RATES_KEY, junk)
        rates, _ = db.read_worker_rates(con)
        assert rates is None, junk


def test_the_payload_sums_both_processes(client, isolated_config, monkeypatch):
    from app.transfer import manager as transfer_manager

    monkeypatch.setattr(transfer_manager.get_manager(), "rates",
                        lambda: {"down": {"backfill": 100.0, "interactive": 7.0},
                                  "up": {"seeding": 1.0},
                                  "limits": {"down": None, "up": None}})
    db.publish_worker_rates(db.connect(),
                            {"down": {"backfill": 400.0, "delta": 3.0}, "up": {}})

    payload = web._bandwidth_payload()
    assert payload["down"]["backfill"] == 500.0, "worker + web, per class"
    assert payload["down"]["interactive"] == 7.0, "web-only class survives"
    assert payload["down"]["delta"] == 3.0, "worker-only class appears"
    assert payload["up"]["seeding"] == 1.0
    assert payload["worker"]["reporting"] is True
    assert payload["worker"]["age_seconds"] is not None


def test_the_payload_says_when_the_worker_is_silent(client, isolated_config,
                                                     monkeypatch):
    """The UI badge flips on this flag, so a reader is never left guessing
    whether zero means idle or means nobody asked the worker."""
    from app.transfer import manager as transfer_manager

    monkeypatch.setattr(transfer_manager.get_manager(), "rates",
                        lambda: {"down": {"backfill": 2.0}, "up": {},
                                  "limits": {}})
    payload = web._bandwidth_payload()
    assert payload["worker"]["reporting"] is False
    assert payload["down"]["backfill"] == 2.0, "web figures still reported"
    assert payload["worker"]["max_age_seconds"] == db.WORKER_RATES_MAX_AGE_S


def test_the_stream_endpoint_serves_the_merged_payload(client, isolated_config):
    db.publish_worker_rates(db.connect(), {"down": {"backfill": 42.0}, "up": {}})
    # the SSE generator never ends; the payload builder is the testable half
    payload = web._bandwidth_payload()
    assert payload["down"]["backfill"] >= 42.0
    r = client.get("/admin/network")
    assert r.status_code == 200
    assert "bw-scope" in r.text, "the badge the flag drives must exist"


def test_worker_rates_are_not_adopted_as_node_control(isolated_config):
    """Telemetry, not state: carrying one node-control owner's stale reading
    to the next would publish a lie for a round, and it is overwritten
    within seconds anyway."""
    assert db.WORKER_RATES_KEY not in db.NODE_CONTROL_KEYS
