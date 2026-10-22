"""Users: who may search (07 §7.4, amended 2026-10-22; app/users.py).

Operator, 2026-10-22: "a search-only gui (aka user gui). A user token giving
only access to the ask tab", with add, remove, role, expiry, reissue and
revoke; Ask stays LAN-open until the first user exists; the same token is
the API key of `/v1` and MCP (Open WebUI, agents); a token is never stored."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app import config, users
from app.api import app
from app.louise import main as louise_main


@pytest.fixture
def client(isolated_config, monkeypatch):
    from tests.test_web_pages import _patch_hash, _patch_health_pings
    _patch_hash(monkeypatch)
    _patch_health_pings(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        yield c


# ---- the file ---------------------------------------------------------------

def test_a_token_is_printed_once_and_only_its_hash_is_kept(isolated_config):
    u, token = users.add("ada")
    raw = users.path().read_text()
    assert token.startswith("lu_") and token not in raw and users.hash_token(token) in raw
    assert users.resolve(token).name == "ada" and users.resolve("lu_nope") is None
    assert users.resolve("sekrit") is None, "never the admin token"
    assert "token_sha256" not in users.listing()[0]


def test_role_expiry_reissue_revoke_remove(isolated_config):
    _, t1 = users.add("bob", role="user", expires="2030-01-31")
    assert users.load()[0].expires == "2030-02-01T00:00:00Z", "valid through the day named, UTC"
    assert users.set_role("bob", "admin").role == "admin"
    assert users.set_expiry("bob", "never").expires is None
    u, t2 = users.reissue("bob")
    assert users.resolve(t1) is None and users.resolve(t2).name == "bob", "the old token dies at once"
    assert u.role == "admin", "a reissue keeps the role"
    users.revoke("bob")
    assert users.resolve(t2) is None and users.listing()[0]["state"] == "revoked"
    _, t3 = users.reissue("bob")
    assert users.resolve(t3) is not None, "a reissue restores a revoked user"
    users.remove("bob")
    assert users.load() == []
    with pytest.raises(users.UserError):
        users.remove("bob")


def test_an_expired_token_stops_working(isolated_config):
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, token = users.add("eve", expires=past)
    assert users.resolve(token) is None and users.listing()[0]["state"] == "expired"
    assert not users.any_active(), "an expired user does not close Ask"


@pytest.mark.parametrize("bad", ["Ada", "", "a b", "../x", "x" * 65])
def test_a_name_is_a_plain_local_name(isolated_config, bad):
    with pytest.raises(users.UserError):
        users.add(bad)


def test_the_expiry_forms(isolated_config):
    now = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
    assert users.parse_expiry("never", now=now) is None and users.parse_expiry(None, now=now) is None
    assert users.parse_expiry("90d", now=now) == "2027-01-20T12:00:00Z"
    assert users.parse_expiry("2026-12-31", now=now) == "2027-01-01T00:00:00Z"
    with pytest.raises(users.UserError):
        users.parse_expiry("soon", now=now)


def test_the_cli_verbs(isolated_config, capsys):
    assert louise_main(["user", "add", "ada", "--expires", "30d"]) == 0
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert users.resolve(token).name == "ada"
    assert louise_main(["user", "role", "ada", "admin"]) == 0
    assert louise_main(["user", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    assert listed[0]["role"] == "admin" and token not in json.dumps(listed)
    assert louise_main(["user", "revoke", "ada"]) == 0 and users.resolve(token) is None
    assert louise_main(["user", "add", "ada"]) == 2, "a name taken is refused, never overwritten"


# ---- the gate ----------------------------------------------------------------

def test_ask_is_open_until_the_first_user_then_needs_a_token(client):
    assert client.get("/ask").status_code == 200
    assert client.get("/v1/models").status_code == 200
    _, token = users.add("ada")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models").headers["www-authenticate"] == "Bearer"
    r = client.get("/ask", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/ask"
    bearer = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/models", headers=bearer).status_code == 200
    assert client.post("/api/ask/mcp", headers=bearer,
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200
    assert client.post("/api/ask/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert client.get("/help").status_code == 200 and client.get("/login").status_code == 200, "open"
    assert client.get("/healthz").status_code == 200


def test_a_user_sees_ask_and_never_the_librarys_routes(client):
    _, token = users.add("ada")
    assert client.post("/login", json={"token": token}).json()["role"] == "user"
    page = client.get("/ask")
    assert page.status_code == 200
    assert "/admin/library" not in page.text and "/connect" in page.text and "ada" in page.text
    assert client.get("/admin/library", headers={"accept": "application/json"}).status_code == 401
    assert client.get("/admin/api/users", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    client.post("/logout")
    assert client.get("/v1/models").status_code == 401


def test_an_admin_role_user_is_an_admin(client):
    _, token = users.add("root-two", role="admin")
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/admin/api/users", headers=h).status_code == 200
    assert client.get("/admin/library", headers=h).status_code == 200


def test_the_admin_api(client):
    h = {"X-Admin-Token": "sekrit"}
    r = client.post("/admin/api/users", headers=h, json={"name": "ada", "expires": "90d"})
    assert r.status_code == 200 and r.json()["token"].startswith("lu_")
    first = r.json()["token"]
    assert client.post("/admin/api/users", headers=h, json={"name": "ada"}).status_code == 409
    assert client.patch("/admin/api/users/ada", headers=h, json={"role": "admin", "expires": "never"}).json() \
        | {"last_seen": None} == {**client.get("/admin/api/users", headers=h).json()["users"][0], "last_seen": None}
    again = client.post("/admin/api/users/ada/reissue", headers=h).json()["token"]
    assert users.resolve(first) is None and users.resolve(again) is not None
    assert client.post("/admin/api/users/ada/revoke", headers=h).json()["state"] == "revoked"
    assert client.delete("/admin/api/users/ada", headers=h).json() == {"ok": True, "removed": "ada"}
    assert client.delete("/admin/api/users/ada", headers=h).status_code == 404
    listed = client.get("/admin/api/users", headers=h).json()
    assert listed == {"users": []}


def test_me_says_who_is_asking(client):
    assert client.get("/api/me").json()["role"] == "guest"
    _, token = users.add("ada", expires="2030-01-01")
    assert client.get("/api/me").json() == {"name": None, "role": None, "login_required": True}
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert (me["name"], me["role"], me["expires"]) == ("ada", "user", "2030-01-02T00:00:00Z")


# ---- what a client reaches -----------------------------------------------------

def test_models_name_every_collection_and_a_model_chooses_what_is_searched(client):
    from app import api
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert ids[0] == "louise" and all(i.startswith("louise/") for i in ids[1:]) and len(ids) >= 2
    assert api.collections_for_model("louise", None) is None
    assert api.collections_for_model("gutenberg-rag", None) is None, "the old name still answers"
    assert api.collections_for_model("louise/gutenberg", None) == ["gutenberg"]
    assert api.collections_for_model("louise/gutenberg", ["x"]) == ["x"], "the body's list wins"
    with pytest.raises(Exception):
        api.collections_for_model("gpt-4", None)


def test_mcp_speaks_the_streamable_http_transport(client):
    r = client.post("/api/ask/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    assert client.get("/api/ask/mcp").status_code == 405
    batch = client.post("/api/ask/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                              {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert batch.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_connect_is_filled_in_for_the_viewer(client):
    page = client.get("/connect", headers={"host": "louise.lan:30042"})
    assert page.status_code == 200
    assert "http://louise.lan:30042/v1" in page.text and "http://louise.lan:30042/api/ask/mcp" in page.text
    assert "No key is needed" in page.text
    _, token = users.add("ada")
    client.post("/login", json={"token": token})
    page = client.get("/connect", headers={"x-forwarded-proto": "https", "x-forwarded-host": "search.example"})
    assert "https://search.example/v1" in page.text and token in page.text, "their own key, behind show"
    client.post("/logout")
    client.post("/login", json={"token": "sekrit"})
    page = client.get("/connect")
    assert "sekrit" not in page.text and "louise user add" in page.text, "the admin token is never shown"


def test_a_users_file_that_does_not_read_closes_ask_and_keeps_the_admin_token(client):
    users.path().write_text("[[user]]\nname = ")
    assert client.get("/v1/models").status_code == 401, "closed, never open"
    assert client.get("/v1/models", headers={"X-Admin-Token": "sekrit"}).status_code == 200
    assert client.get("/admin/api/users", headers={"X-Admin-Token": "sekrit"}).status_code == 409
