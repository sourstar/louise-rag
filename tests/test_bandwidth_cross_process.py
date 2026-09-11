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


def test_the_polling_path_uses_a_cheap_reader(isolated_config, monkeypatch):
    """_bandwidth_payload runs every couple of seconds per subscriber.
    db.connect() re-runs the schema script and the ALTER TABLE migrations on
    every call — write-path work against a manifest the pipeline threads are
    already contending for."""
    db.publish_worker_rates(db.connect(), {"down": {"backfill": 1.0}, "up": {}})

    calls = {"connect": 0, "ro": 0}
    real_ro = db.read_only_connect
    monkeypatch.setattr(db, "connect",
                        lambda *a, **k: calls.__setitem__("connect", calls["connect"] + 1))
    monkeypatch.setattr(db, "read_only_connect",
                        lambda *a, **k: (calls.__setitem__("ro", calls["ro"] + 1),
                                          real_ro(*a, **k))[1])

    web._bandwidth_payload()
    assert calls["ro"] == 1
    assert calls["connect"] == 0, "the full connect() must not be on this path"


def test_the_reader_tolerates_a_manifest_that_does_not_exist_yet(isolated_config,
                                                                  monkeypatch):
    """A node whose first collection has never been written must still serve
    the Network page."""
    from app import collections as collections_mod
    c = collections_mod.default_collection()
    monkeypatch.setattr(c.manifest_path.__class__, "exists", lambda self: False)
    assert db.read_only_connect() is None
    payload = web._bandwidth_payload()
    assert payload["worker"]["reporting"] is False


def test_worker_rates_are_not_adopted_as_node_control(isolated_config):
    """Telemetry, not state: carrying one node-control owner's stale reading
    to the next would publish a lie for a round, and it is overwritten
    within seconds anyway."""
    assert db.WORKER_RATES_KEY not in db.NODE_CONTROL_KEYS


def test_the_page_gets_totals_and_a_download_split_by_collection(isolated_config,
                                                                 monkeypatch):
    """The Network page shows two figures a person can act on — download
    and upload totals — and the download split BY COLLECTION, derived from
    the worker's per-collection fetch recorder. The scheduler's priority
    classes ("interactive 0, delta 0, backfill 0, seeding 0") stay in the
    payload but off the page: they told nobody which corpus was
    downloading how fast. All of it rides the one cheap reader."""
    # The split is filtered against the collections this node actually
    # holds: the stats blob is keyed on NAME and nothing prunes it, so a
    # retired collection used to keep its row — named, at a frozen rate —
    # after it had left the Library. So the two whose stats are published
    # below have to be collections, not just names in a sample.
    from app import collections as collections_mod
    cfg = collections_mod.collections_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    for name in ("gutenberg", "wikipedia"):
        (cfg / f"{name}.toml").write_text(
            f'plugin = "{name}"\n', encoding="utf-8")

    con = db.connect()
    db.publish_worker_rates(con, {"down": {"backfill": 40.0, "delta": 2.0},
                                  "up": {"seeding": 5.0}})
    db.publish_worker_stats(con, {
        "gutenberg": {"fetch": {"totals": {"bytes": 6000}, "span_seconds": 60.0,
                                "busy_seconds": 30.0, "samples": 3, "age_seconds": 1}},
        "wikipedia": {"fetch": {"totals": {"bytes": 600}, "span_seconds": 60.0,
                                "busy_seconds": 60.0, "samples": 1, "age_seconds": 1}},
        "idle": {"embed": None},
    })
    calls = {"ro": 0}
    real_ro = db.read_only_connect
    monkeypatch.setattr(db, "read_only_connect",
                        lambda *a, **k: (calls.__setitem__("ro", calls["ro"] + 1),
                                          real_ro(*a, **k))[1])

    payload = web._bandwidth_payload()
    assert calls["ro"] == 1, "totals and the split must not open a second connection"
    assert payload["totals"]["down"] >= 42.0 and payload["totals"]["up"] >= 5.0
    split = [(d["collection"], d["bytes_per_s"]) for d in payload["downloads"]]
    assert split == [("gutenberg", 100.0), ("wikipedia", 10.0)], \
        "wall-clock bytes/s per collection, fastest first, idle ones absent"
