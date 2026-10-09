"""An encoder READ TIMEOUT is the endpoint's, never a document's — until one
document alone keeps timing out while the endpoint answers others.

Live, 2026-10-09: a LocalAI backend crashed under a GPU overcommit and came
back hung; requests read-timed-out and the worker wrote `ReadTimeout` onto
gutenberg:76722, 34379 and 22283, removing them from every selection. These
tests drive the real client (a fake server behind `httpc.post`) through the
real worker paths.
"""
import httpx
import pytest

from app import collections as collections_mod, config, db, embeddings, encoder_timeouts
from app import retrieval, worker
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
    """An encoder that answers, or takes the request and never answers:
    `hung` for every request, `slow(texts)` per request."""

    def __init__(self):
        self.hung = False
        self.slow = None
        self.sent: list[list[str]] = []

    def post(self, url, json=None, timeout=None, headers=None):
        texts = list(json["input"])
        self.sent.append(texts)
        request = httpx.Request("POST", url)
        if self.hung or (self.slow and self.slow(texts)):
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, request=request, json={"data": [
            {"index": i, "embedding": [1.0] + [0.0] * 7} for i in range(len(texts))]})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(worker, "_endpoint_paused", {})
    monkeypatch.setattr(config, "BACKEND_GATE", "off")    # the endpoint alone
    backend_gate.reset_cache()
    encoder_timeouts.reset()
    yield
    backend_gate.reset_cache()
    encoder_timeouts.reset()


@pytest.fixture
def server(monkeypatch):
    s = Server()
    monkeypatch.setattr(embeddings.httpc, "post", s.post)
    return s


def _collection(tmp_path, monkeypatch, concurrency, docs=4):
    monkeypatch.setitem(collections_mod.PLUGIN_REGISTRY, "fake", FakePlugin())
    c = collections_mod.Collection(
        name="fake", plugin="fake", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(
            url="http://localai:8080", api="openai", model="qwen",
            dtype="f16", ctx=512, dim=8, concurrency=concurrency))
    con = db.connect(c)
    for i in range(1, docs + 1):
        _add(con, i)
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    return c


def _add(con, i):
    con.execute(
        "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
        "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,1,0)",
        (f"fake:{i}", "fake", str(i), f"/x/{i}", "en"))


@pytest.fixture
def serial(isolated_config, tmp_path, monkeypatch):
    return _collection(tmp_path, monkeypatch, concurrency=1)


@pytest.fixture
def parallel(isolated_config, tmp_path, monkeypatch):
    return _collection(tmp_path, monkeypatch, concurrency=3)


def _row(con, uid):
    return con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()


def _errors(con) -> dict:
    return {r["uid"]: r["error"] for r in
            con.execute("SELECT uid, error FROM documents WHERE error IS NOT NULL "
                        "OR retries > 0")}


def _endpoint(c):
    return encoder_timeouts.endpoint_of(c.embed)


def _release(c):
    """The hold's bound has passed."""
    encoder_timeouts._holds[_endpoint(c)] = (0.0, encoder_timeouts._holds.get(
        _endpoint(c), (0.0, 0))[1])


def _poisoned(texts):
    return any("abstract of 2" in t for t in texts)


# ---- the client's verdict ------------------------------------------------------

@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.WriteTimeout])
def test_a_timeout_is_the_endpoint_busy(server, monkeypatch, failure):
    def post(url, json=None, timeout=None, headers=None):
        raise failure("timed out", request=httpx.Request("POST", url))
    monkeypatch.setattr(embeddings.httpc, "post", post)
    recipe = collections_mod.EmbedRecipe(url="http://enc:1", api="openai", model="m",
                                         dtype="f16", ctx=512)
    with pytest.raises(embeddings.EndpointTimedOut) as e:
        embeddings.embed_calibration(recipe, ["a text"])
    assert isinstance(e.value, embeddings.EndpointBusy)
    assert worker.transient_failure(e.value) and worker.endpoint_timed_out(e.value)
    assert "did not answer within" in str(e.value)


# ---- a hung endpoint: nobody is blamed ----------------------------------------

def test_a_hung_endpoint_errors_no_document_on_the_serial_path(serial, server):
    con = db.connect(serial)
    server.hung = True
    for _ in range(4):                          # far past BLAME_AFTER, every document
        for i in range(1, 5):
            _release(serial)
            assert worker._execute_step(con, serial, "embed", _row(con, f"fake:{i}")) is False
    assert _errors(con) == {}, "a hung endpoint blames no one"
    assert all(r["embed_done"] == 0 for r in con.execute("SELECT embed_done FROM documents"))
    events = [r["message"] for r in con.execute("SELECT message FROM events")]
    assert any("held as busy" in m for m in events), events


def test_a_hung_endpoint_is_held_and_sent_nothing_meanwhile(serial, server):
    con = db.connect(serial)
    server.hung = True
    worker._execute_step(con, serial, "embed", _row(con, "fake:1"))
    assert encoder_timeouts.held(_endpoint(serial)) == pytest.approx(encoder_timeouts.HOLD_S, abs=1)
    assert worker._embedding_allowed(con, serial) is False, "backed off"
    sent = len(server.sent)
    for _ in range(20):                         # rounds: no request, no hot loop
        assert worker._run_one_step(con, serial, {}, pipeline_active=True) is False
    assert len(server.sent) == sent

    _release(serial)                            # the bound has passed: asked again
    assert worker._embedding_allowed(con, serial) is True
    worker._execute_step(con, serial, "embed", _row(con, "fake:2"))
    assert encoder_timeouts.held(_endpoint(serial)) == pytest.approx(
        2 * encoder_timeouts.HOLD_S, abs=1), "doubling while it answers nobody"

    server.hung = False                         # it answers again: the hold is over
    _release(serial)
    assert worker._execute_step(con, serial, "embed", _row(con, "fake:3")) is True
    assert encoder_timeouts.held(_endpoint(serial)) == 0
    assert encoder_timeouts._holds == {}


def test_a_hung_endpoint_errors_no_document_on_the_parallel_path(parallel, server):
    con = db.connect(parallel)
    server.hung = True
    recipe = collections_mod.recipe_for(parallel)
    assert worker._embed_parallel(con, parallel, recipe) is False
    assert worker._embedding_allowed(con, parallel) is False, "held as busy"
    for _ in range(6):
        _release(parallel)
        assert worker._embed_parallel(con, parallel, recipe) is False
    assert _errors(con) == {}
    assert len(server.sent) == 4, "each document asked once: no hammering a hung backend"
    events = [r["message"] for r in con.execute("SELECT message FROM events")]
    assert any("held as busy" in m for m in events), events


def test_a_timed_out_document_is_passed_over_until_the_endpoint_answers(serial, server):
    con = db.connect(serial)
    scope = db.get_scope(con)
    server.hung = True
    worker._execute_step(con, serial, "embed", _row(con, "fake:1"))
    uids = [r["uid"] for r in worker._embed_candidates(con, scope, 4, serial)]
    assert uids == ["fake:2", "fake:3", "fake:4"], "the others are asked first"

    server.hung = False
    _release(serial)
    assert worker._execute_step(con, serial, "embed", _row(con, "fake:2")) is True
    uids = [r["uid"] for r in worker._embed_candidates(con, scope, 4, serial)]
    assert uids[0] == "fake:1", "the endpoint answered someone: asked again"


def test_a_lone_timed_out_document_is_asked_again_after_its_bound(serial, server, monkeypatch):
    con = db.connect(serial)
    con.execute("UPDATE documents SET embed_done=1, embed_version=? WHERE uid<>'fake:1'",
                (config.EMBED_VERSION,))
    server.hung = True
    worker._execute_step(con, serial, "embed", _row(con, "fake:1"))
    assert worker._embed_candidates(con, db.get_scope(con), 4, serial) == []
    monkeypatch.setattr(encoder_timeouts, "COOL_S", 0.0)
    assert [r["uid"] for r in worker._embed_candidates(con, db.get_scope(con), 4, serial)] \
        == ["fake:1"]


# ---- one poison document: blamed after N, the others untouched -----------------

def test_one_document_that_alone_keeps_timing_out_is_blamed_on_the_serial_path(
        serial, server):
    con = db.connect(serial)
    server.slow = _poisoned
    for other in ("fake:1", "fake:3"):
        assert worker._execute_step(con, serial, "embed", _row(con, "fake:2")) is False
        assert _errors(con) == {}, "not yet"
        assert worker._execute_step(con, serial, "embed", _row(con, other)) is True
    assert worker._execute_step(con, serial, "embed", _row(con, "fake:2")) is True
    errors = _errors(con)
    assert list(errors) == ["fake:2"]
    assert errors["fake:2"].startswith(
        f"embed: EndpointTimedOut: timed out {encoder_timeouts.BLAME_AFTER} times "
        f"while the endpoint answered other documents")
    assert _row(con, "fake:1")["embed_done"] == 1 and _row(con, "fake:3")["embed_done"] == 1


def test_one_document_that_alone_keeps_timing_out_is_blamed_on_the_parallel_path(
        parallel, server):
    con = db.connect(parallel)
    server.slow = _poisoned
    recipe = collections_mod.recipe_for(parallel)
    for n in range(5, 15):                      # a new document each round: others answered
        _release(parallel)
        worker._embed_parallel(con, parallel, recipe)
        if _errors(con):
            break
        _add(con, n)
    errors = _errors(con)
    assert list(errors) == ["fake:2"], errors
    assert "EndpointTimedOut: timed out 3 times" in errors["fake:2"]
    assert sum(1 for t in server.sent if _poisoned(t)) == encoder_timeouts.BLAME_AFTER


def test_a_timeout_then_a_success_starts_the_count_again(serial, server):
    con = db.connect(serial)
    server.slow = _poisoned
    worker._execute_step(con, serial, "embed", _row(con, "fake:2"))
    worker._execute_step(con, serial, "embed", _row(con, "fake:1"))
    worker._execute_step(con, serial, "embed", _row(con, "fake:2"))     # two against it
    server.slow = None
    worker._execute_step(con, serial, "embed", _row(con, "fake:2"))     # it answers
    assert _row(con, "fake:2")["embed_done"] == 1
    assert (str(serial.manifest_path), "fake:2") not in encoder_timeouts._marks

    con.execute("UPDATE documents SET embed_done=0 WHERE uid='fake:2'")
    server.slow = _poisoned
    for other in ("fake:3", "fake:4"):          # two more, answers between: still not three
        worker._execute_step(con, serial, "embed", _row(con, "fake:2"))
        worker._execute_step(con, serial, "embed", _row(con, other))
    assert _errors(con) == {}


def test_only_timeouts_with_answers_between_count():
    ep, place = ("http://enc:1", "m"), "/m.db"
    assert [encoder_timeouts.timed_out(ep, place, "d") for _ in range(10)] == [1] * 10
    encoder_timeouts.answered(ep, place, "other")
    assert encoder_timeouts.timed_out(ep, place, "d") == 2
    assert encoder_timeouts.timed_out(ep, place, "d") == 2
    encoder_timeouts.answered(("http://enc:1", "another-model"), place, "other")
    assert encoder_timeouts.timed_out(ep, place, "d") == 2, "another model's answer is not this one's"
    encoder_timeouts.answered(ep, place, "other")
    assert encoder_timeouts.timed_out(ep, place, "d") == encoder_timeouts.BLAME_AFTER
    assert (place, "d") not in encoder_timeouts._marks


def test_the_marks_are_bounded(monkeypatch):
    monkeypatch.setattr(encoder_timeouts, "MAX_MARKS", 5)
    for i in range(20):
        encoder_timeouts.timed_out(("u", "m"), "/m.db", f"d{i}")
    assert list(encoder_timeouts._marks) == [("/m.db", f"d{i}") for i in range(15, 20)]


# ---- the query path ------------------------------------------------------------

def test_a_query_timeout_withholds_the_vector_leg_with_its_reason(serial, server):
    con = db.connect(serial)
    worker._execute_step(con, serial, "embed", _row(con, "fake:1"))   # a vector table exists
    server.hung = True
    result = retrieval.search(con, "abstract", mode="dense", translate=False,
                              collection=serial)
    leg, = result["legs_withheld"]
    assert leg["leg"] == "vector" and "did not answer in time" in leg["reason"]
