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


def test_taking_from_the_offer_is_plug_and_play_the_node_decides_the_rest(client, monkeypatch):
    """RULE 2 (operator, 2026-10-20): the user chooses the local name; the
    node decides the encoder (its model server, the names a held collection
    in the same space uses), takes the whole collection, texts and peers'
    vectors, and embeds p2p where it can — off, with the reason, where it
    cannot. Explicit fields still work for the command line's callers."""
    from types import SimpleNamespace
    codex = pubmed()
    announced(codex)
    queued = []
    monkeypatch.setattr(jobs, "submit", lambda kind, args: queued.append((kind, args)) or 7)
    monkeypatch.setattr(api, "_models_at", lambda url: [])
    # this node holds a collection in the same space: everything decided
    held = SimpleNamespace(name="pubmed-local", codex=codex, url="http://localai:30286", api="vega",
                           model="vega-medcpt-article", query_model="vega-medcpt-query")

    def holding(*items):
        # `load_all(skip_unreadable=True)` is the worker's shape, (served, unreadable)
        return lambda **kw: (list(items), []) if kw.get("skip_unreadable") else list(items)
    monkeypatch.setattr(collections_mod, "load_all", holding(held))
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id, "name": "pubmed-here"})
    assert r.status_code == 202, r.text
    (kind, args), = queued
    assert kind == jobs.SHARE_FETCH
    assert args == {"collection_id": codex.collection_id, "name": "pubmed-here", "slices": "all",
                    "kinds": ["records", "vectors"], "encoder": "http://localai:30286", "api": "vega",
                    "embed_model": "vega-medcpt-article", "query_model": "vega-medcpt-query",
                    "snapshot": "", "embed": "p2p"}
    assert r.json()["missing"] == "" and "--embed p2p" in r.json()["cli"]
    # no collection with this ENCODER here (another's weights): held without an encoder,
    # embed off, the reason said — the server is still known from the other file
    other_weights = SimpleNamespace(space=SimpleNamespace(encoder=SimpleNamespace(weights="sha256:" + "ee" * 32)))
    monkeypatch.setattr(collections_mod, "load_all", holding(SimpleNamespace(
        name="other", codex=other_weights, url="http://localai:30286", api="vega",
        model="vega-x", query_model="vega-x")))
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id})
    assert r.status_code == 202
    assert queued[1][1]["encoder"] == "" and queued[1][1]["embed"] == "off"
    assert "no model on this node's server (http://localai:30286)" in r.json()["missing"]
    # the command line's caller may say everything itself
    r = client.post("/api/share/fetch", json={
        "collection_id": codex.collection_id, "name": "pubmed-cli",
        "slices": ["year=2024", "year=2025"], "kinds": ["records"],
        "encoder": "http://nas:30437", "api": "openai", "embed_model": "m", "query_model": "q", "embed": "all"})
    assert r.status_code == 202
    assert queued[2][1]["slices"] == ["year=2024", "year=2025"] and queued[2][1]["embed"] == "all"
    assert "--slice year=2024 year=2025" in r.json()["cli"] and "--kinds records" in r.json()["cli"]
    r = client.post("/api/share/fetch", json={"collection_id": codex.collection_id, "embed": "sometimes",
                                              "encoder": "http://nas:30437", "embed_model": "m"})
    assert r.status_code == 409 and "embed is one of" in r.text and len(queued) == 3


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


def test_the_encoder_is_decided_from_what_the_node_knows(client, monkeypatch):
    """Operator, 2026-10-20: "this is something already set at a system
    level". The model server is the address the held files share (LocalAI
    on the node; VEGA_URL is the receipts service, which answers no model);
    the names come from a held collection in the same space. Where the node
    knows nothing, it says what is missing — it never asks a form."""
    from types import SimpleNamespace
    from app import platform_config
    codex = pubmed()
    announced(codex)
    monkeypatch.setattr(api, "_models_at", lambda url: ["vega-medcpt-article", "vega-medcpt-query", "vega-bge-m3"])
    # nothing held, no platform encoder: the node knows no server
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([], []) if kw.get("skip_unreadable") else [])
    monkeypatch.setattr(platform_config, "load", lambda: SimpleNamespace(deposit=SimpleNamespace(encoder=None, api="openai")))
    s = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id}).json()
    assert s["url"] == "" and not s["resolved"] and "knows no model server" in s["missing"]
    # the platform's deposit encoder names the server; no collection in the space: the model is missing
    monkeypatch.setattr(platform_config, "load", lambda: SimpleNamespace(deposit=SimpleNamespace(encoder="http://localai:30286/", api="vega")))
    s = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id}).json()
    assert (s["url"], s["api"], s["resolved"]) == ("http://localai:30286", "vega", False)
    assert s["models"] == ["vega-medcpt-article", "vega-medcpt-query", "vega-bge-m3"]
    assert "install it there" in s["missing"] and s["weights"]["document"] == codex.space.encoder.weights
    # a held collection in the same space: the server is its, the names are its, nothing to choose
    held = SimpleNamespace(name="pubmed-local", codex=codex, url="http://localai:30286", api="vega",
                           model="vega-medcpt-article", query_model="vega-medcpt-query")
    monkeypatch.setattr(collections_mod, "load_all", lambda **kw: ([held], []) if kw.get("skip_unreadable") else [held])
    s = client.get("/api/encoders/suggest", params={"collection_id": codex.collection_id}).json()
    assert (s["resolved"], s["known_from"], s["embed_model"], s["query_model"]) == (
        True, "pubmed-local", "vega-medcpt-article", "vega-medcpt-query")
    assert s["missing"] == "" and "embeds and searches it" in s["note"]
    # without a collection: the server alone, for the create form
    s = client.get("/api/encoders/suggest").json()
    assert s["url"] == "http://localai:30286" and not s["resolved"] and s["models"]
