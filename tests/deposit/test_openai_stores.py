"""OpenAI's vector stores and files API over Louise (app/openai_stores.py;
07 §7.4, amended 2026-10-22: "add the OpenAI missing interface calls").

The requests are the ones OpenAI's SDK sends (paths, bodies, multipart);
the answers are read as it reads them. A deposit collection is the writable
store; a collection built from its origin is read-only; deposits stay
invisible to anyone without the deposit token or an admin."""
import pytest

from app import collections as collections_mod
from tests.deposit.test_deposit_localagi import ADMIN, TOKEN, H, client, node  # noqa: F401


def up(client, name: str, data: bytes, headers=None):
    return client.post("/v1/files", headers=headers or H(), data={"purpose": "assistants"},
                       files={"file": (name, data, "text/plain")})


def test_a_store_is_created_filled_searched_read_and_deleted(client):
    s = client.post("/v1/vector_stores", headers=H(), json={"name": "alice", "metadata": {"team": "a"}})
    assert s.status_code == 200, s.text
    assert s.json()["object"] == "vector_store" and s.json()["id"] == "alice"
    assert collections_mod.by_name("alice").codex.plugin == "deposit"

    f = up(client, "espresso.txt", b"The user prefers espresso without sugar.").json()
    assert f["object"] == "file" and f["filename"] == "espresso.txt" and f["id"].startswith("file-")
    vf = client.post("/v1/vector_stores/alice/files", headers=H(),
                     json={"file_id": f["id"], "attributes": {"topic": "coffee"}}).json()
    assert vf == {**vf, "id": f["id"], "object": "vector_store.file", "status": "completed",
                  "vector_store_id": "alice", "attributes": {"topic": "coffee"}}

    store = client.get("/v1/vector_stores/alice", headers=H()).json()
    assert store["file_counts"]["total"] == 1 and store["metadata"] == {"team": "a"}
    listed = client.get("/v1/vector_stores/alice/files", headers=H()).json()
    assert [x["id"] for x in listed["data"]] == [f["id"]] and listed["object"] == "list"

    page = client.post("/v1/vector_stores/alice/search", headers=H(),
                       json={"query": "espresso", "max_num_results": 5}).json()
    assert page["object"] == "vector_store.search_results.page" and page["search_query"] == "espresso"
    [hit] = page["data"]
    assert hit["file_id"] == f["id"], "the file id the client attached, not an internal uid"
    assert "espresso" in hit["content"][0]["text"] and isinstance(hit["score"], float)
    assert hit["attributes"]["collection"] == "alice"

    content = client.get(f"/v1/vector_stores/alice/files/{f['id']}/content", headers=H()).json()
    assert content["data"][0]["text"] == "The user prefers espresso without sugar."

    gone = client.delete(f"/v1/vector_stores/alice/files/{f['id']}", headers=H()).json()
    assert gone == {"id": f["id"], "object": "vector_store.file.deleted", "deleted": True}
    assert client.get("/v1/vector_stores/alice/files", headers=H()).json()["data"] == []
    assert client.get(f"/v1/files/{f['id']}", headers=H()).status_code == 200, "the upload stays"

    d = client.delete("/v1/vector_stores/alice", headers=H()).json()
    assert d == {"id": "alice", "object": "vector_store.deleted", "deleted": True}
    assert client.get("/v1/vector_stores/alice", headers=H()).status_code == 404


def test_a_batch_attaches_several_files(client):
    client.post("/v1/vector_stores", headers=H(), json={"name": "bob"})
    ids = [up(client, f"{n}.txt", f"note {n}".encode()).json()["id"] for n in ("one", "two")]
    b = client.post("/v1/vector_stores/bob/file_batches", headers=H(), json={"file_ids": ids}).json()
    assert b["object"] == "vector_store.files_batch" and b["status"] == "completed"
    assert b["file_counts"]["completed"] == 2
    assert client.get(f"/v1/vector_stores/bob/file_batches/{b['id']}", headers=H()).json()["id"] == b["id"]
    files = client.get(f"/v1/vector_stores/bob/file_batches/{b['id']}/files", headers=H()).json()
    assert sorted(x["id"] for x in files["data"]) == sorted(ids)


def test_a_deposit_made_elsewhere_has_a_stable_id(client):
    client.post("/api/collections", json={"name": "carol"}, headers=H())
    client.post("/api/collections/carol/upload", headers=H(),
                files={"file": ("memo.txt", b"a memo", "text/plain")})
    [x] = client.get("/v1/vector_stores/carol/files", headers=H()).json()["data"]
    again = client.get("/v1/vector_stores/carol/files", headers=H()).json()["data"][0]
    assert x["id"] == again["id"] and x["id"].startswith("file-dep-")
    assert client.get(f"/v1/vector_stores/carol/files/{x['id']}/content",
                      headers=H()).json()["data"][0]["text"] == "a memo"


def test_errors_come_in_openais_shape(client):
    r = client.get("/v1/vector_stores/nope", headers=H())
    assert r.status_code == 404 and set(r.json()["error"]) == {"message", "type", "param", "code"}
    client.post("/v1/vector_stores", headers=H(), json={"name": "dave"})
    r = client.post("/v1/vector_stores/dave/files", headers=H(), json={"file_id": "file-none"})
    assert r.status_code == 404 and r.json()["error"]["param"] == "file_id"
    r = client.post("/v1/vector_stores/dave/search", headers=H(),
                    json={"query": "x", "filters": {"type": "gt", "key": "year", "value": 3}})
    assert r.status_code == 400 and "eq, ne, in" in r.json()["error"]["message"]


def test_deposits_stay_hidden_and_writes_need_the_deposit_token(client):
    client.post("/v1/vector_stores", headers=H(), json={"name": "erin"})
    assert "erin" not in [s["id"] for s in client.get("/v1/vector_stores").json()["data"]]
    assert client.get("/v1/vector_stores/erin").status_code == 404, "not even named"
    assert client.post("/v1/vector_stores", json={"name": "frank"}).status_code == 401
    assert client.post("/v1/files", files={"file": ("a.txt", b"x")}).status_code == 401
    assert client.get("/v1/vector_stores", headers=H(ADMIN)).status_code == 200


def test_the_filters_map_to_facets():
    from app.openai_stores import facet_filters
    assert facet_filters({"type": "and", "filters": [
        {"type": "eq", "key": "lang", "value": "en"},
        {"type": "ne", "key": "year", "value": "1900"},
        {"type": "in", "key": "subject", "value": ["a", "b"]}]}) == {
        "lang": "en", "year": {"not": "1900"}, "subject": ["a", "b"]}
    assert facet_filters(None) is None


def test_openais_own_sdk_reads_the_answers(client):
    """The SDK itself, where it is installed (it is not a dependency): its
    models parse every answer above."""
    openai = pytest.importorskip("openai")
    sdk = openai.OpenAI(base_url="http://testserver/v1", api_key=TOKEN, http_client=client)
    vs = sdk.vector_stores.create(name="gina")
    f = sdk.files.create(file=("tea.txt", b"Green tea, no milk."), purpose="assistants")
    sdk.vector_stores.files.create(vector_store_id=vs.id, file_id=f.id)
    hits = list(sdk.vector_stores.search(vector_store_id=vs.id, query="tea"))
    assert hits and hits[0].file_id == f.id
    assert [x.id for x in sdk.vector_stores.files.list(vector_store_id=vs.id)] == [f.id]
    assert sdk.vector_stores.delete(vs.id).deleted
