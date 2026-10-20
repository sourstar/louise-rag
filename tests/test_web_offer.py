"""tests/test_web_offer.py — the Library's "Offered by peers" and its two
routes (07 §7.4, amended 2026-10-20): `GET /api/share/offer/show` is
`share offer`; `POST /api/share/fetch` is `share fetch`, refused at once
for what cannot be taken and otherwise queued for the worker."""
import time

import pytest
from fastapi.testclient import TestClient

from app import api, config, jobs
from app.share import catalogue, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import pubmed
from tests.share.test_messages import PEER_KEY


@pytest.fixture
def client(node, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(api.app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def announced(codex):
    con = peers.connect()
    catalogue.record(con, [codex], announced_by=PEER_KEY, now=time.time())
    con.close()


def test_the_offer_route_and_the_library_section(client):
    codex = pubmed()
    announced(codex)
    r = client.get("/api/share/offer/show")
    assert r.status_code == 200
    (entry,) = r.json()["offered"]
    assert entry["collection_id"] == codex.collection_id and entry["plugin"] == "pubmed"
    page = client.get("/admin/library")
    assert page.status_code == 200
    assert "Offered by peers" in page.text and codex.collection_id in page.text
    assert "take…" in page.text and "louise share offer" in page.text


def test_taking_from_the_offer_queues_share_fetch_with_the_choice(client, monkeypatch):
    codex = pubmed()
    announced(codex)
    queued = []
    monkeypatch.setattr(jobs, "submit", lambda kind, args: queued.append((kind, args)) or 7)
    r = client.post("/api/share/fetch", json={
        "collection_id": codex.collection_id, "name": "pubmed-here",
        "slices": ["year=2024", "year=2025"], "kinds": ["records", "vectors"],
        "encoder": "http://nas:30437", "api": "openai", "embed_model": "m", "query_model": "q"})
    assert r.status_code == 202, r.text
    assert r.json()["id"] == 7
    assert "--slice year=2024 year=2025" in r.json()["cli"] and "--kinds records,vectors" in r.json()["cli"]
    (kind, args), = queued
    assert kind == jobs.SHARE_FETCH
    assert args == {"collection_id": codex.collection_id, "name": "pubmed-here",
                    "slices": ["year=2024", "year=2025"], "kinds": ["records", "vectors"],
                    "encoder": "http://nas:30437", "api": "openai", "embed_model": "m",
                    "query_model": "q", "snapshot": "", "embed": "off"}
    # the defaults: every slice, records, nothing spent on embedding
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id,
                                              "encoder": "http://nas:30437", "embed_model": "m"})
    assert r.status_code == 202 and queued[1][1]["slices"] == "all" and queued[1][1]["kinds"] == ["records"]
    assert queued[1][1]["embed"] == "off"
    # the embed mode is the taker's (07 §7.8; operator 2026-10-20: "what about the p2p embedding?")
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id, "embed": "p2p",
                                              "encoder": "http://nas:30437", "embed_model": "m"})
    assert r.status_code == 202 and queued[2][1]["embed"] == "p2p"
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id, "embed": "sometimes",
                                              "encoder": "http://nas:30437", "embed_model": "m"})
    assert r.status_code == 409 and "embed is one of" in r.text
    # no encoder: the collection would be refused as non-compliant — refused here instead, queued nothing
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id})
    assert r.status_code == 409 and "no [encoder]" in r.text and len(queued) == 3


def test_what_cannot_be_taken_is_refused_at_once_and_queues_nothing(client, monkeypatch):
    codex = pubmed()
    announced(codex)
    monkeypatch.setattr(jobs, "submit", lambda kind, args: pytest.fail("nothing is queued"))
    assert client.post("/api/share/fetch", json={"collection_id": "sha256:" + "0" * 64}).status_code == 409
    assert "not in the offer" in client.post("/api/share/fetch",
                                             json={"collection_id": "sha256:" + "0" * 64}).json()["detail"]
    assert client.post("/api/share/fetch", json={"collection_id": "pubmed"}).status_code == 409
    assert client.post("/api/share/fetch", json={"collection_id": codex.collection_id, "kinds": []}).status_code == 409
    assert client.post("/api/share/fetch", json={"collection_id": codex.collection_id,
                                                 "slices": "year=2024"}).status_code == 409
    assert client.post("/api/share/fetch", json={"collection_id": codex.collection_id,
                                                 "api": "nonsense"}).status_code == 409
    # admin only
    anon = TestClient(api.app)
    assert anon.get("/api/share/offer/show").status_code in (401, 403)
    assert anon.post("/api/share/fetch", json={"collection_id": codex.collection_id}).status_code in (401, 403)
