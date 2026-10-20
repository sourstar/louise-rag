"""tests/test_web_offer.py — the Library's "Offered by peers" and its two
routes (07 §7.4, amended 2026-10-20): `GET /api/share/offer/show` is
`share offer`; `POST /api/share/fetch` is `share fetch`, refused at once
for what cannot be taken and otherwise queued for the worker."""
import time

import pytest
from fastapi.testclient import TestClient

from app import api, collections as collections_mod, config, jobs
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


def test_the_encoder_is_filled_from_what_the_node_knows_not_asked(client, monkeypatch):
    """Operator, 2026-10-20: "this is something already set at a system
    level". The model server is the node's (VEGA_URL); the model names come
    from a held collection in the same space, else the server's list."""
    from types import SimpleNamespace
    codex = pubmed()
    announced(codex)
    # no model server named: nothing to fill, the note says so
    monkeypatch.setattr(config, "VEGA_URL", "")
    monkeypatch.setattr(api, "_models_at", lambda url: ["vega-medcpt-article", "vega-medcpt-query", "vega-bge-m3"])
    r = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id})
    assert r.status_code == 200 and r.json()["url"] == "" and "VEGA_URL" in r.json()["note"]
    # the node's server: filled in, with its model list to pick from
    monkeypatch.setattr(config, "VEGA_URL", "http://vega:30286/")
    r = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id})
    s = r.json()
    assert (s["url"], s["api"], s["known_from"]) == ("http://vega:30286", "vega", None)
    assert s["models"] == ["vega-medcpt-article", "vega-medcpt-query", "vega-bge-m3"] and "pick the model" in s["note"]
    assert s["weights"]["document"] == codex.space.encoder.weights
    # a held collection in the same space: its names, nothing to choose
    held = SimpleNamespace(name="pubmed-local", codex=codex, url="http://vega:30286", api="vega",
                           model="vega-medcpt-article", query_model="vega-medcpt-query")
    real_load_all = collections_mod.load_all
    monkeypatch.setattr(collections_mod, "load_all", lambda: [held])
    s = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id}).json()
    assert (s["known_from"], s["embed_model"], s["query_model"]) == ("pubmed-local", "vega-medcpt-article", "vega-medcpt-query")
    assert "nothing to choose" in s["note"]
    monkeypatch.setattr(collections_mod, "load_all", real_load_all)
    # without a collection: the server alone, for the create form
    s = client.get("/api/encoders/suggest").json()
    assert s["url"] == "http://vega:30286" and s["known_from"] is None and s["models"]
    # the dialog asks the node, and the create form takes the server
    page = client.get("/admin/library").text
    assert "/api/encoders/suggest" in page and "Model server (this node's" in page
