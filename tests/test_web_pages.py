"""P0.6 acceptance: Ask/Library/Network/System pages render from fixture
manifests over real (non-mocked) code paths; admin routes are gated by a
single ADMIN_TOKEN (header or /admin/login cookie); reader-facing routes
stay open with no token at all — "Ask is LAN-open read-only"
(architecture-v2.md §15).
"""
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, fts, spaces, web
from app.api import app


def _patch_hash(monkeypatch, value="abc123", weak=False):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: (value, weak))


def _patch_health_pings(monkeypatch):
    # web._ping(), not httpx.Client.get — patching the latter globally would
    # also break TestClient itself, since it's httpx-based.
    monkeypatch.setattr(web, "_ping", lambda url: True)


def _chunk(uid, native_id, seq, chapter, text):
    return {"id": f"{uid}:{seq}:1", "book_id": native_id, "seq": seq,
            "chapter": chapter, "text": text, "tokens": max(1, len(text) // 4)}


def _seed_one_book():
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    uid = "gutenberg:1"
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,title,authors,rdf_done,fts_done,"
        "updated_at) VALUES(?,?,?,?,?,?,?,1,1,?)",
        (uid, "gutenberg", "1", "/fake/1", "en", "The Lighthouse Keeper", "A. Author",
         int(time.time())))
    fts.index_book("en", 1, [_chunk(uid, 1, 0, "Chapter I",
                    "the lighthouse keeper lit the lamp every single night")], collection)


@pytest.fixture
def client(isolated_config, monkeypatch):
    _patch_hash(monkeypatch)
    _patch_health_pings(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        yield c


def test_ask_page_renders_open_without_auth(client):
    r = client.get("/ask")
    assert r.status_code == 200
    assert "Ask" in r.text


def test_static_assets_the_new_pages_reference_are_actually_served(client):
    # base.html links these; index.html never needed a static mount at all
    # (fully self-contained inline CSS/JS) so this was a real gap until it
    # was added alongside the new pages.
    r = client.get("/static/admin.css")
    assert r.status_code == 200 and "nav{" in r.text
    r = client.get("/static/vendor/htmx.min.js")
    assert r.status_code == 200 and len(r.content) > 1000


def test_ask_query_shows_hit_with_local_verification_badge(client):
    _seed_one_book()
    r = client.post("/ask/query", json={"query": "lighthouse keeper", "mode": "fts"})
    assert r.status_code == 200
    assert "local ✓" in r.text
    assert "lighthouse" in r.text.lower()


def test_admin_routes_require_token(client):
    r = client.get("/admin/library")
    assert r.status_code == 401

    r = client.get("/admin/library", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401

    r = client.get("/admin/library", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200


def test_admin_token_unset_locks_out_entirely(client, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    r = client.get("/admin/library", headers={"X-Admin-Token": "anything"})
    assert r.status_code == 401


def test_admin_login_cookie_authorizes_subsequent_navigation(client):
    r = client.post("/admin/login", json={"token": "wrong"})
    assert r.status_code == 401

    r = client.post("/admin/login", json={"token": "sekrit"})
    assert r.status_code == 200
    assert "admin_token" in r.cookies

    r = client.get("/admin/library")   # TestClient persists cookies across calls
    assert r.status_code == 200


def test_reader_routes_stay_open_with_no_token(client):
    _seed_one_book()
    checks = [
        ("get", "/api/status", {}),
        ("get", "/api/stats", {}),
        ("get", "/api/events", {}),
        ("get", "/api/books", {}),
        ("post", "/api/search", {"json": {"query": "lighthouse", "mode": "fts"}}),
    ]
    for method, path, kwargs in checks:
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code != 401, f"{path} must stay open (no admin token supplied)"


def test_mutating_routes_are_gated(client):
    checks = [
        ("post", "/api/control", {"json": {"state": "running"}}),
        ("post", "/api/scope", {"json": {}}),
        ("post", "/api/discover", {}),
        ("post", "/api/reset", {"json": {"stage": "errors"}}),
    ]
    for method, path, kwargs in checks:
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 401, f"{path} must require admin token"


def test_library_page_renders_shelf_card(client):
    r = client.get("/admin/library", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    # the default fixture collection has acquire_enabled=False, so the
    # "fetched" stage bar is honestly absent — its n/a explanation stands in
    assert "fetch stage: n/a" in r.text
    assert "parsed (rdf)" in r.text
    assert "indexed (lexical)" in r.text and "embedded (dense)" in r.text
    assert "grow profile" in r.text


def test_network_page_honest_empty_state(client):
    r = client.get("/admin/network", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "not yet available" in r.text


def test_bandwidth_payload_reports_real_per_class_rates():
    # Not driving the live /admin/network/bandwidth/stream endpoint here: its
    # generator runs forever (by design — a real SSE feed), and a sync
    # generator blocked in time.sleep() inside a worker thread can't be
    # reliably cancelled mid-sleep by anyio, making an in-process end-to-end
    # consumption a hang risk. _bandwidth_payload() is the same payload the
    # endpoint yields every 2s.
    payload = web._bandwidth_payload()
    for direction in ("down", "up"):
        assert set(payload[direction]) == {"interactive", "delta", "backfill", "seeding"}
    assert "schedule" in payload["limits"]


def test_bandwidth_override_is_tier3_and_expires(client):
    r = client.post("/admin/network/bandwidth/override",
                     json={"rate": "10Mbit", "hours": 6},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert r.json()["rate_bps"] == 1_250_000

    con = db.connect()
    assert db.get_bandwidth_override(con) == 1_250_000
    # never written to platform.toml — the file always wins on restart
    assert not (Path(os.environ["CONFIG_DIR"]) / "platform.toml").exists()

    r = client.post("/admin/network/bandwidth/override", json={"rate": None},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert db.get_bandwidth_override(db.connect()) is None


def test_bandwidth_override_rejects_a_malformed_rate(client):
    r = client.post("/admin/network/bandwidth/override", json={"rate": "400Furlongs"},
                     headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 400


def test_bandwidth_override_requires_admin(client):
    r = client.post("/admin/network/bandwidth/override", json={"rate": "1Mbit"})
    assert r.status_code == 401


def test_system_page_shows_platform_toml_tier2_rows(client):
    r = client.get("/admin/system", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "platform.toml" in r.text
    assert "bandwidth.schedule" in r.text
    assert "transfer.workers" in r.text


def test_system_page_renders_effective_config_and_boot_report(client):
    r = client.get("/admin/system", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    assert "Tier 1" in r.text and "Tier 2" in r.text and "Tier 3" in r.text
    assert "Boot-validation report" in r.text


def test_system_log_fragment_renders(client):
    r = client.get("/admin/system/log", headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
