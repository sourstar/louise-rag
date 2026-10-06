"""tests/ask/test_ask_api.py — `ask` over HTTP (07 §7.4): the body's keys
are the flags, the exit code is the status with `exit` in the body, and
the MCP server answers JSON-RPC at /api/ask/mcp."""
import pytest
from fastapi.testclient import TestClient

from app import api
from app.ask import results
from tests.deposit.test_deposit_identity import node  # noqa: F401


@pytest.fixture
def client(node):  # noqa: F811
    return TestClient(api.app)


def test_search_answers_the_tool_s_object_and_a_refusal_carries_the_exit(client, monkeypatch):
    seen = {}

    def fake(query, **kw):
        seen.update(query=query, **kw)
        return {"lists": [], "facts": [], "unresolved": [], "withheld": [], "legs_withheld": [],
                "translations": {}}
    monkeypatch.setattr(results, "search", fake)
    r = client.post("/api/ask/search", json={"query": "truth", "limit": 5, "translate": True})
    assert r.status_code == 200 and r.json()["lists"] == []
    assert seen["query"] == "truth" and seen["limit"] == 5 and seen["translate"] is True and seen["rerank_depth"] == 50
    monkeypatch.setattr(results, "search", lambda *a, **k: (_ for _ in ()).throw(results.NotYet("as_of: not yet")))
    r = client.post("/api/ask/search", json={"query": "q", "as_of": "2026-01-01"})
    assert r.status_code == 409 and r.json() == {"exit": 2, "error": "as_of: not yet"}
    assert client.post("/api/ask/search", json={"query": "q", "limit": 0}).status_code == 422


def test_lookup_and_traverse_routes(client):
    r = client.post("/api/ask/lookup", json={"id": "nowhere:1"})
    assert r.status_code == 200 and r.json() == {"facts": [], "unresolved": ["nowhere:1"]}
    r = client.post("/api/ask/lookup", json={"id": "malformed"})
    assert r.status_code == 409 and r.json()["exit"] == 2
    r = client.post("/api/ask/traverse", json={"id": "pubmed:1", "depth": 2})
    assert r.status_code == 200 and r.json()["edges"] == [] and r.json()["depth"] == 2, "nothing held: no edges"
    assert client.post("/api/ask/traverse", json={"id": "no-colon"}).status_code == 409


def test_the_mcp_server_over_http(client):
    r = client.post("/api/ask/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200 and [t["name"] for t in r.json()["result"]["tools"]] == [
        "louise_search", "louise_lookup", "louise_traverse"]
    r = client.post("/api/ask/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 204
    r = client.post("/api/ask/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                          "params": {"name": "louise_lookup", "arguments": {"id": "nowhere:1"}}})
    assert r.status_code == 200 and r.json()["result"]["structuredContent"] == {"facts": [], "unresolved": ["nowhere:1"]}
    assert client.post("/api/ask/mcp", content=b"nope", headers={"content-type": "application/json"}).status_code == 400
