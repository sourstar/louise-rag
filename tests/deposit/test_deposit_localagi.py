"""tests/deposit/test_deposit_localagi.py — the LocalAGI routes (07 §7.4;
migration step 15d).

Every request here is the one LocalAGI's HTTP client sends
(`pkg/localrag/client.go` at 7e0947d): the same method, path, body shape,
bearer header. Every answer is read the way that client reads it — the
status it demands, the `{success, data, error}` envelope, the upload's bare
`{status, filename, key}`, the Result fields it decodes case-insensitively.

And the other side: an open route never returns a deposit collection.
"""
import hashlib
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import api, config, db, collections as collections_mod
from app.hold import deposit
from tests.deposit.test_deposit_identity import ENCODER_URL, make, node  # noqa: F401
from tests.hold.conftest import PLATFORM

TOKEN = "deposit-" + "x" * 24
ADMIN = "admin-" + "y" * 24


@pytest.fixture
def client(node, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "DEPOSIT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", PLATFORM)
    (tmp_path / "config" / "platform.toml").write_text(
        f'[deposit]\nencoder = "{ENCODER_URL}"\n', encoding="utf-8")
    make(node, "seed")            # the first deposit collection on the encoder
    # the worker's deposit thread, beside the client, as on a node: the web
    # routes only queue a deposit write (app/jobs.py DEPOSIT)
    import threading
    from app import jobs
    stop = threading.Event()
    t = threading.Thread(target=jobs.serve_deposits, args=(stop,),
                         kwargs={"poll_s": 0.02, "log": lambda m: None}, daemon=True)
    t.start()
    yield TestClient(api.app)
    stop.set()
    t.join(5)


def H(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def envelope(r, status=200):
    """What `parseAPIError` and every decoder in the client read."""
    assert r.status_code == status, r.text
    body = r.json()
    assert body["success"] is True, body
    return body.get("data")


def memory_key(text: str) -> str:
    """The file name `WrappedClient.Store` gives a memory."""
    return f"2026-09-30-10-00-00-{hashlib.md5(text.encode()).hexdigest()}.txt"


def store(client, name, key, data: bytes):
    """`Client.Store`: multipart, field `file`, named with the PATH the
    client opened — `CreateFormFile("file", file.Name())`, a temp path."""
    return client.post(f"/api/collections/{name}/upload", headers=H(),
                       files={"file": (f"/tmp/localrag3141592/{key}", data,
                                       "application/octet-stream")})


# ---- the contract --------------------------------------------------------

def test_create_collection_answers_201(client):
    r = client.post("/api/collections", json={"name": "alice"}, headers=H())
    data = envelope(r, 201)
    assert data["collection_id"].startswith("sha256:")
    assert collections_mod.by_name("alice").codex.plugin == "deposit"


def test_create_again_is_refused_and_the_agent_carries_on(client):
    """`NewWrappedClient` calls CreateCollection on every agent start and
    ignores the error: the second call must fail, and fail cleanly."""
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    r = client.post("/api/collections", json={"name": "alice"}, headers=H())
    assert r.status_code == 409 and r.json()["success"] is False
    assert r.json()["error"]["message"]


def test_list_collections(client):
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    data = envelope(client.get("/api/collections", headers=H()))
    assert {"alice", "seed"} <= set(data["collections"])


def test_a_memory_is_stored_listed_read_searched_and_deleted(client):
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    text = "The user prefers espresso without sugar."
    key = memory_key(text)

    r = store(client, "alice", key, text.encode())
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "filename": key, "key": key}   # bare

    assert envelope(client.get("/api/collections/alice/entries", headers=H()))["keys"] == [key]

    got = envelope(client.get(f"/api/collections/alice/entries/{quote(key, safe='')}",
                              headers=H()))
    assert got["content"] == text and got["chunk_count"] >= 1

    data = envelope(client.post("/api/collections/alice/search", headers=H(),
                                json={"query": "espresso", "max_results": 5}))
    [res] = data["results"]
    lowered = {k.lower(): v for k, v in res.items()}          # Go decodes so
    assert "espresso" in lowered["content"]
    assert all(isinstance(v, str) for v in lowered["metadata"].values())
    assert isinstance(lowered["similarity"], float)
    assert lowered["id"].startswith("deposit:")

    r = client.request("DELETE", "/api/collections/alice/entry/delete", headers=H(),
                       json={"entry": key})
    assert envelope(r)["remaining_entries"] == []


def test_reset(client):
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    store(client, "alice", "a.txt", b"one")
    store(client, "alice", "b.txt", b"two")
    envelope(client.post("/api/collections/alice/reset", headers=H()))
    assert envelope(client.get("/api/collections/alice/entries", headers=H()))["keys"] == []


def test_refusals_come_in_the_envelope(client):
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    r = store(client, "alice", "tool.exe", b"MZ")
    assert r.status_code == 409
    assert r.json()["success"] is False and ".pdf" in r.json()["error"]["message"]
    r = client.get("/api/collections/alice/entries/missing.txt", headers=H())
    assert r.status_code == 404 and r.json()["success"] is False


def test_sources_through_localagis_routes(client, tmp_path):
    """AddSource refused until the node accepts the disclaimer (04 §4.4);
    then recorded, fetched, listed with its last fetch, and removed with its
    deposit — LocalAGI's AddSource/ListSources/RemoveSource, as it sends them."""
    from app import licensing
    from app.hold import sources
    from tests.deposit.test_deposit_sources import _Site
    www = tmp_path / "www"
    www.mkdir()
    (www / "page.html").write_text("<p>La ricetta del pesto.</p>", encoding="utf-8")
    site = _Site(www)
    try:
        client.post("/api/collections", json={"name": "alice"}, headers=H())
        url = f"{site.base}/page.html"
        body = {"url": url, "update_interval": 60}
        r = client.post("/api/collections/alice/sources", headers=H(), json=body)
        assert r.status_code == 409 and "disclaimer" in r.json()["error"]["message"]
        assert site.hits == []

        licensing.accept_web_sources()
        from app import node_state
        node_state.set_worker_state("running")
        envelope(client.post("/api/collections/alice/sources", headers=H(), json=body))
        sources.tick()                         # what the worker's sources thread does
        [row] = envelope(client.get("/api/collections/alice/sources", headers=H()))["sources"]
        assert row["url"] == url and row["update_interval"] == 60 and row["last_update"]
        key = sources.source_key("alice", url)
        assert key in envelope(client.get("/api/collections/alice/entries", headers=H()))["keys"]

        r = client.request("DELETE", "/api/collections/alice/sources", headers=H(),
                           json={"url": url})
        envelope(r)
        assert envelope(client.get("/api/collections/alice/sources", headers=H()))["sources"] == []
        assert key not in envelope(client.get("/api/collections/alice/entries",
                                              headers=H()))["keys"]
    finally:
        site.server.shutdown()


# ---- curated collections: searchable, never written (D2) ------------------

def test_a_curated_collection_is_read_only_here(client, tmp_path):
    from tests.hold.conftest import KEY, NOW, fake_embed, fake_query_embed
    from tests.hold.test_template import recipe
    from app.hold import collection as col
    root = collections_mod.louise_root() / "collections"
    codex = col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM,
                                   embed=fake_embed, embed_query=fake_query_embed,
                                   name="gutenberg", key=KEY, now=NOW)
    (tmp_path / "config" / "collections" / "gutenberg.toml").write_text(
        f'codex = "{codex.collection_id}"\n', encoding="utf-8")
    assert "gutenberg" in envelope(client.get("/api/collections", headers=H()))["collections"]
    for r in (store(client, "gutenberg", "a.txt", b"x"),
              client.post("/api/collections/gutenberg/reset", headers=H()),
              client.get("/api/collections/gutenberg/entries", headers=H())):
        assert r.status_code == 409 and r.json()["error"]["code"] == "NOT_A_DEPOSIT_COLLECTION"


# ---- who may call ---------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, H("wrong"), {"Authorization": TOKEN}])
def test_every_route_needs_the_token(client, headers):
    assert client.get("/api/collections", headers=headers).status_code == 401
    assert client.post("/api/collections/seed/search", headers=headers,
                       json={"query": "x"}).status_code == 401


def test_the_admin_token_is_accepted_too(client):
    assert client.get("/api/collections", headers=H(ADMIN)).status_code == 200


def test_with_no_deposit_token_configured_the_routes_are_closed(client, monkeypatch):
    monkeypatch.setattr(config, "DEPOSIT_TOKEN", "")
    assert client.get("/api/collections", headers=H(ADMIN)).status_code == 401


# ---- the open routes never return a deposit collection -------------------

def test_open_search_leaves_deposits_out_and_does_not_name_them(client):
    deposit.add("seed", "secret.txt", b"the private espresso preference")
    r = client.post("/api/search", json={"query": "espresso", "mode": "fts",
                                         "translate": False})
    assert r.status_code == 200
    body = r.json()
    assert body.get("hits", []) == []
    assert "seed" not in json.dumps(body.get("withheld", []))
    r = client.post("/api/search", json={"query": "espresso", "collections": ["seed"],
                                         "mode": "fts", "translate": False})
    assert r.status_code == 400 and "unknown" in r.text    # as if it did not exist


def test_the_ask_page_and_the_reader_do_not_know_them(client):
    got = deposit.add("seed", "secret.txt", b"the private espresso preference")
    assert "seed" not in client.get("/ask").text
    assert client.get(f"/read/seed/{got['native_id']}").status_code == 404


def test_a_localagi_search_is_not_written_to_the_query_log(client):
    deposit.add("seed", "note.txt", b"a memory about tea")
    envelope(client.post("/api/collections/seed/search", headers=H(),
                         json={"query": "tea ceremony secret", "max_results": 3}))
    con = db.connect()
    logged = [r[0] for r in con.execute("SELECT query FROM query_log")]
    assert not any("tea ceremony secret" in q for q in logged)


def test_the_key_is_the_base_name_of_what_the_client_sent(client):
    """Go's server reads a part's name through filepath.Base, and LocalAGI
    lists and deletes by that — never by the temp path it uploaded from."""
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    r = store(client, "alice", "città-appunti.md", "# Nota\n\ncaffè".encode())
    assert r.json()["key"] == "città-appunti.md"
    assert envelope(client.get("/api/collections/alice/entries", headers=H()))["keys"] \
        == ["città-appunti.md"]


# ---- the web process writes nothing itself (app/jobs.py DEPOSIT) ----------

def test_with_no_worker_a_write_is_queued_and_nothing_is_written(node, tmp_path,  # noqa: F811
                                                                   monkeypatch):
    """tantivy allows one writer per index and the worker keeps its own, so
    the web process only queues. With no worker to take the job: 503 after
    the wait, and not a byte in the collection's origin or index."""
    from app import localagi
    monkeypatch.setattr(config, "DEPOSIT_TOKEN", TOKEN)
    monkeypatch.setattr(localagi, "WORKER_WAIT_S", 0.3)
    make(node, "seed")
    c = collections_mod.by_name("seed")
    before = sorted(p.name for p in c.fts_dir.rglob("*")) if c.fts_dir.exists() else []
    r = store(TestClient(api.app), "seed", "a.txt", b"queued, not written")
    assert r.status_code == 503 and r.json()["error"]["code"] == "QUEUED"
    assert not c.corpus_dir.exists() or not any(c.corpus_dir.iterdir())
    after = sorted(p.name for p in c.fts_dir.rglob("*")) if c.fts_dir.exists() else []
    assert after == before


def test_a_finished_deposit_job_leaves_no_key_in_the_node_state(client):
    client.post("/api/collections", json={"name": "alice"}, headers=H())
    key = "remember-" + "q" * 20 + ".txt"
    store(client, "alice", key, b"x")
    client.request("DELETE", "/api/collections/alice/entry/delete", headers=H(),
                   json={"entry": key})
    state = collections_mod.louise_root() / "state"
    for f in state.rglob("*"):
        if f.is_file():
            assert key.encode() not in f.read_bytes(), f
    incoming = collections_mod.louise_root() / "collections" / "alice" / "state" / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())
