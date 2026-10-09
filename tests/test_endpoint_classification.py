"""A wrong or unavailable encoder ENDPOINT is never a document's error.

Live, 2026-10-09: gutenberg's `[encoder] url` named `…/v1/embeddings`, the
client appended `/v1/embeddings` again, LocalAI answered 404 "Resource not
found" to every request, and the worker wrote `embed: RuntimeError: embed
server HTTP 404` onto 4,295 documents — each one removed from every
selection until a human intervened. These tests drive the real client (a
fake server behind `httpc.post`) through the real worker paths.
"""
import dataclasses
import json
import threading

import httpx
import pytest

from app import collections as collections_mod, config, db, embeddings, retrieval, worker
from app.verify import backend_gate


class FakePlugin:
    source_id = "fake"

    def fetch(self, ref):
        from app.plugins.base import RawDoc
        return RawDoc(ref=ref, raw_hash="h", data=ref.native_id.encode())

    def parse(self, raw):
        from app.plugins.base import CanonicalDoc
        nid = raw.data.decode()
        return CanonicalDoc(
            uid=f"fake:{nid}", title=f"doc {nid}", creators=[], language="en",
            date=None, subjects=[], keys=[], extra={},
            sections=[("Abstract", f"abstract of {nid}")], raw_hash="h")


class Server:
    """An encoder whose answer the test sets: `status` for every request, or
    `refuse(texts) -> status | None` per request."""

    def __init__(self):
        self.status = 200
        self.refuse = None
        self.body = None
        self.sent: list[list[str]] = []
        self.lock = threading.Lock()

    def post(self, url, json=None, timeout=None, headers=None):
        texts = list(json["input"])
        with self.lock:
            self.sent.append(texts)
        request = httpx.Request("POST", url)
        status = self.refuse(texts) if self.refuse else None
        status = status or self.status
        if status != 200:
            return httpx.Response(status, request=request, text="Resource not found")
        if self.body is not None:
            return httpx.Response(200, request=request, text=self.body)
        return httpx.Response(200, request=request, json={"data": [
            {"index": i, "embedding": [1.0] + [0.0] * 7} for i in range(len(texts))]})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(worker, "_endpoint_paused", {})
    monkeypatch.setattr(config, "BACKEND_GATE", "off")    # the endpoint pause alone
    backend_gate.reset_cache()
    yield
    backend_gate.reset_cache()


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(embeddings.httpc, "post", s.post)
    return s


def _collection(tmp_path, monkeypatch, concurrency):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://localai:8080/v1/embeddings", api="openai", model="qwen",
            dtype="f16", ctx=512, dim=8, concurrency=concurrency))
    con = db.connect(c)
    for i in range(1, 5):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
            (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    return c


@pytest.fixture
def serial(isolated_config, tmp_path, monkeypatch):
    return _collection(tmp_path, monkeypatch, concurrency=1)


@pytest.fixture
def parallel(isolated_config, tmp_path, monkeypatch):
    return _collection(tmp_path, monkeypatch, concurrency=3)


def _untouched(con) -> bool:
    rows = con.execute("SELECT error, retries, embed_done FROM documents").fetchall()
    return all(r["error"] is None and r["retries"] == 0 and r["embed_done"] == 0
               for r in rows)


def _row(con, uid="fake:1"):
    return con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()


RECIPE = collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                     dtype="f16", ctx=512)


# ---- the client's verdict ------------------------------------------------------

@pytest.mark.parametrize("status", [404, 405, 401, 403, 410])
def test_an_answer_that_says_the_endpoint_is_wrong(server, status):
    server.status = status
    with pytest.raises(embeddings.EndpointMisconfigured) as e:
        embeddings.embed_calibration(RECIPE, ["a text"])
    assert e.value.status == status and f"HTTP {status}" in str(e.value)
    assert "appends /v1/embeddings" in str(e.value), "the likely mistake is named"
    assert worker.endpoint_unavailable(e.value) and worker.transient_failure(e.value)


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_an_answer_that_says_not_now(server, status):
    server.status = status
    with pytest.raises(embeddings.EndpointBusy):
        embeddings.embed_calibration(RECIPE, ["a text"])


def test_a_2xx_that_is_not_an_embeddings_answer_is_the_wrong_endpoint(server):
    server.body = "<html>LocalAI web UI</html>"
    with pytest.raises(embeddings.EndpointMisconfigured, match="not an embeddings response"):
        embeddings.embed_calibration(RECIPE, ["a text"])


def test_a_dropped_connection_is_the_endpoints(server):
    assert worker.endpoint_unavailable(httpx.RemoteProtocolError("Server disconnected"))
    assert worker.endpoint_unavailable(httpx.ReadError("connection reset"))
    assert not worker.endpoint_unavailable(httpx.ReadTimeout("read timed out"))


def test_an_input_refusal_is_split_down_to_the_one_input(server, monkeypatch):
    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    server.refuse = lambda texts: 422 if any("BAD" in t for t in texts) else None
    with pytest.raises(embeddings.InputRefused, match="HTTP 422"):
        embeddings.embed_calibration(RECIPE, ["a", "b", "BAD", "c"])
    assert server.sent == [["a", "b", "BAD", "c"], ["a", "b"], ["BAD", "c"], ["BAD"]]


def test_a_batch_too_large_as_a_whole_heals_by_splitting_never_by_cutting(
        server, monkeypatch):
    """Before, a 413 for the batch cut EVERY input in it to a shorter
    budget: an input's vector depended on its neighbours."""
    monkeypatch.setattr(config, "EMBED_BATCH", 4)
    server.refuse = lambda texts: 413 if len(texts) > 2 else None
    texts = ["x" * 1500, "y" * 1500, "z" * 1500, "w" * 1500]
    assert len(embeddings.embed_calibration(RECIPE, texts)) == 4
    assert server.sent[1:] == [texts[:2], texts[2:]], "split whole, nothing cut"


def test_an_input_still_too_long_after_every_cut_says_so(server, monkeypatch):
    server.status = 400
    with pytest.raises(embeddings.InputRefused, match="still refused"):
        embeddings.embed_calibration(
            dataclasses.replace(RECIPE, ctx=8192), ["a text"])


# ---- the serial path -----------------------------------------------------------

def test_a_404_pauses_embedding_and_errors_no_document(serial, server):
    con = db.connect(serial)
    server.status = 404
    assert worker._execute_step(con, serial, "embed", _row(con)) is False
    assert _untouched(con), "the endpoint's fault is never written onto a document"

    events = [r["message"] for r in con.execute("SELECT message FROM events")]
    assert any("embedding PAUSED" in m and "HTTP 404" in m for m in events), events
    state = backend_gate.gate_state(con)
    assert state["allowed"] is False and "HTTP 404" in state["reason"]
    assert worker._embedding_allowed(con, serial) is False, "paused, on a bound"
    sent = len(server.sent)
    for _ in range(50):                      # rounds: no request, no hot loop
        assert worker._run_one_step(con, serial, {}, pipeline_active=True) is False
    assert len(server.sent) == sent


def test_the_pause_lifts_when_the_encoder_recipe_is_fixed(serial, server):
    con = db.connect(serial)
    server.status = 404
    worker._execute_step(con, serial, "embed", _row(con))
    assert worker._embedding_allowed(con, serial) is False

    fixed = dataclasses.replace(serial, embed=dataclasses.replace(
        serial.embed, url="http://localai:8080"))
    server.status = 200
    assert worker._embedding_allowed(con, fixed) is True
    assert backend_gate.gate_state(con)["allowed"] is True
    assert worker._execute_step(con, fixed, "embed", _row(con)) is True
    assert _row(con)["embed_done"] == 1


def test_the_pause_asks_again_on_its_bound_and_lifts_when_the_endpoint_answers(
        serial, server):
    con = db.connect(serial)
    server.status = 404
    worker._execute_step(con, serial, "embed", _row(con))
    place = str(serial.manifest_path)
    recipe, reason, _ = worker._endpoint_paused[place]

    worker._endpoint_paused[place] = (recipe, reason, 0.0)        # the bound has passed
    assert worker._embedding_allowed(con, serial) is True, "one attempt"
    assert worker._embedding_allowed(con, serial) is False, "and only one"

    worker._endpoint_paused[place] = (recipe, reason, 0.0)
    server.status = 200                                            # fixed on the server
    assert worker._embedding_allowed(con, serial) is True
    assert worker._execute_step(con, serial, "embed", _row(con)) is True
    assert place not in worker._endpoint_paused
    assert backend_gate.gate_state(con)["allowed"] is True
    assert worker._embedding_allowed(con, serial) is True


def test_a_503_defers_without_a_pause(serial, server):
    con = db.connect(serial)
    server.status = 503
    assert worker._execute_step(con, serial, "embed", _row(con)) is False
    assert _untouched(con)
    assert worker._endpoint_paused == {}, "a busy endpoint heals by waiting"
    assert worker._embedding_allowed(con, serial) is True


def test_a_gate_pause_is_not_lifted_by_the_endpoint_pause(serial, server):
    """Only a pause of ours is lifted by us: the run check's or the
    behavioural gate's verdict stays where it is."""
    con = db.connect(serial)
    db.set_control(con, backend_gate.GATE_STATE_KEY,
                   json.dumps({"allowed": False, "reason": "baseline", "ts": 1}))
    worker._endpoint_answered(con, serial)
    worker._endpoint_paused[str(serial.manifest_path)] = (serial.embed, "x", 0.0)
    worker._endpoint_answered(con, serial)
    assert backend_gate.gate_state(con)["reason"] == "baseline"


# ---- the parallel path ---------------------------------------------------------

def test_the_parallel_path_pauses_on_a_404_too(parallel, server):
    con = db.connect(parallel)
    server.status = 404
    recipe = collections_mod.recipe_for(parallel)
    assert worker._embed_parallel(con, parallel, recipe) is False
    assert _untouched(con)
    assert worker._embedding_allowed(con, parallel) is False
    assert "HTTP 404" in backend_gate.gate_state(con)["reason"]


def test_the_parallel_path_defers_a_503(parallel, server):
    con = db.connect(parallel)
    server.status = 503
    assert worker._embed_parallel(con, parallel, collections_mod.recipe_for(parallel)) is False
    assert _untouched(con)
    assert worker._endpoint_paused == {}


def test_a_400_for_one_input_errors_only_its_document(parallel, server):
    con = db.connect(parallel)
    server.refuse = lambda texts: 400 if any("abstract of 2" in t for t in texts) else None
    worker._embed_parallel(con, parallel, collections_mod.recipe_for(parallel))
    rows = {r["uid"]: r for r in con.execute("SELECT * FROM documents")}
    assert rows["fake:2"]["error"].startswith("embed: InputRefused: embed server HTTP 400")
    assert all(rows[u]["embed_done"] == 1 and rows[u]["error"] is None
               for u in ("fake:1", "fake:3", "fake:4"))
    assert worker._endpoint_paused == {}


# ---- the query path ------------------------------------------------------------

def test_a_404_on_the_query_withholds_the_vector_leg_with_its_reason(serial, server):
    con = db.connect(serial)
    worker._execute_step(con, serial, "embed", _row(con))     # a vector table exists
    server.status = 404
    result = retrieval.search(con, "abstract", mode="dense", translate=False,
                              collection=serial)
    leg, = result["legs_withheld"]
    assert leg["leg"] == "vector" and "HTTP 404" in leg["reason"]


def test_a_busy_query_encoder_still_degrades_quietly(serial, server):
    con = db.connect(serial)
    worker._execute_step(con, serial, "embed", _row(con))
    server.status = 503
    result = retrieval.search(con, "abstract", mode="dense", translate=False,
                              collection=serial)
    assert "legs_withheld" not in result
