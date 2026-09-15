"""An unreachable encoder must not be recorded as a broken document.

Why it matters more than it looks: the backfill query requires
`error IS NULL`, so an errored document drops out of embedding even with
retries left, and only returns when a human presses "clear errors & retry".
Observed live — three documents accumulated across one morning of restarts,
each one a connection refused against an encoder that was simply being
restarted, and none of them would ever have retried on their own.

The acquire path already draws this line: a manifest busy past its timeout
is a deferral, logged and retried, never a failure.
"""
import errno

import httpx
import pytest

from app import collections as collections_mod, config, db, worker


@pytest.fixture
def collection(isolated_config):
    return collections_mod.default_collection()


def _doc(con, uid="g:1", **cols):
    fields = {"uid": uid, "source": "g", "native_id": uid.split(":")[1],
              "path": "/x", "in_scope": 1, "rdf_done": 1, "fts_done": 1,
              "embed_done": 0, "chunks": 3, **cols}
    keys = ",".join(fields)
    marks = ",".join("?" * len(fields))
    con.execute(f"INSERT INTO documents({keys}) VALUES({marks})",
                tuple(fields.values()))
    return con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()


# ---------------- the predicate ----------------

@pytest.mark.parametrize("exc", [
    httpx.ConnectError("[Errno 111] Connection refused"),
    httpx.ConnectTimeout("timed out establishing a connection"),
    ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
    OSError(errno.EHOSTUNREACH, "No route to host"),
    OSError(errno.ENETUNREACH, "Network is unreachable"),
])
def test_connection_failures_are_the_endpoints_fault(exc):
    assert worker.endpoint_unavailable(exc) is True


@pytest.mark.parametrize("exc", [
    RuntimeError("embed server HTTP 500: internal error"),
    ValueError("chunk is not text"),
    httpx.ReadTimeout("timed out reading the response"),
    OSError(errno.ENOSPC, "No space left on device"),
])
def test_everything_else_is_still_the_documents_problem(exc):
    """Deliberately narrow. A READ timeout is excluded because an oversized
    document can genuinely cause one, and calling that a deferral would
    retry it forever instead of surfacing it."""
    assert worker.endpoint_unavailable(exc) is False


def _wrapped(status):
    """The embed client's shape: the server's answer wrapped in a
    RuntimeError (`raise … from e`)."""
    req = httpx.Request("POST", "http://gpu:30443/v1/embeddings")
    try:
        try:
            raise httpx.HTTPStatusError("answer", request=req,
                                        response=httpx.Response(status, request=req))
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"embed server HTTP {status}: ...") from e
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_a_server_that_says_not_now_is_the_endpoints_fault(status):
    """llama.cpp answers 503 "Loading model" while it starts: 41 pubmed
    documents were blocked by it across two app restarts (2026-09-15)."""
    assert worker.endpoint_unavailable(_wrapped(status)) is True


def test_a_500_is_still_the_documents_problem():
    assert worker.endpoint_unavailable(_wrapped(500)) is False


def test_a_vector_lock_timeout_is_contention_not_the_document():
    from app.bundles.writer_lock import VectorWriteBusy
    e = VectorWriteBusy("could not acquire the vector write lock: database is locked")
    assert worker.manifest_contended(e) is True
    assert worker.transient_failure(e) is True


# ---------------- the serial path ----------------

def test_a_dead_encoder_leaves_the_document_untouched(collection, monkeypatch):
    con = db.connect(collection)
    row = _doc(con)

    def refuse(*a, **k):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setitem(worker.STAGES, "embed", refuse)
    assert worker._execute_step(con, collection, "embed", row) is False, \
        "a deferral is not work"

    after = con.execute("SELECT * FROM documents WHERE uid='g:1'").fetchone()
    assert after["error"] is None, "no error may be written"
    assert after["retries"] == 0, "no retry may be burned"
    assert after["embed_done"] == 0

    events = [r["message"] for r in
              con.execute("SELECT message FROM events WHERE level='warn'")]
    assert any("deferred" in m for m in events), events


def test_a_real_document_failure_is_still_recorded(collection, monkeypatch):
    """The distinction has to cut both ways, or a genuinely broken document
    would retry forever and never be surfaced."""
    con = db.connect(collection)
    row = _doc(con)

    def broken(*a, **k):
        raise ValueError("this document is malformed")

    monkeypatch.setitem(worker.STAGES, "embed", broken)
    assert worker._execute_step(con, collection, "embed", row) is True

    after = con.execute("SELECT * FROM documents WHERE uid='g:1'").fetchone()
    assert after["retries"] == 1
    assert after["error"].startswith("embed: ")


def test_a_deferral_stops_the_round_rather_than_walking_the_corpus(
        collection, monkeypatch):
    """Without this the loop marches through every remaining document
    against a dead encoder, deferring each one."""
    con = db.connect(collection)
    db.set_scope(con, {"embed_mode": "all"})
    for i in range(5):
        _doc(con, uid=f"g:{i}")

    monkeypatch.setitem(
        worker.STAGES, "embed",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    assert worker._run_one_step(con, collection, {}, pipeline_active=True) is False


# ---------------- the parallel path ----------------

def test_the_parallel_path_defers_too(collection, monkeypatch):
    """Every CPU encoder takes this path, and it records errors in its own
    code — so the distinction has to exist in both places."""
    con = db.connect(collection)
    db.set_scope(con, {"embed_mode": "all"})
    for i in range(4):
        _doc(con, uid=f"g:{i}")

    recipe = collections_mod.EmbedRecipe(
        url="http://down", api="openai", model="m", dtype="f16", ctx=512,
        concurrency=2)
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, c: [{"text": "x", "tokens": 1}])
    monkeypatch.setattr(
        worker.embeddings, "upsert_book",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))

    assert worker._embed_parallel(con, collection, recipe) is False, \
        "nothing embedded and everything deferred is not work"
    rows = con.execute("SELECT error, retries FROM documents").fetchall()
    assert all(r["error"] is None and r["retries"] == 0 for r in rows), \
        [dict(r) for r in rows]


def test_the_parallel_path_still_records_a_real_failure(collection, monkeypatch):
    con = db.connect(collection)
    db.set_scope(con, {"embed_mode": "all"})
    for i in range(4):
        _doc(con, uid=f"g:{i}")

    recipe = collections_mod.EmbedRecipe(
        url="http://x", api="openai", model="m", dtype="f16", ctx=512,
        concurrency=2)
    monkeypatch.setattr(worker, "_load_chunks",
                        lambda row, c: [{"text": "x", "tokens": 1}])
    monkeypatch.setattr(
        worker.embeddings, "upsert_book",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("malformed")))

    worker._embed_parallel(con, collection, recipe)
    rows = con.execute("SELECT error, retries FROM documents").fetchall()
    assert all(r["error"] and r["retries"] == 1 for r in rows), [dict(r) for r in rows]


def test_a_deferred_document_stays_eligible_for_backfill(collection, monkeypatch):
    """The consequence that made this worth fixing: the backfill query
    requires error IS NULL, so a recorded error removes the document from
    embedding entirely until a human intervenes."""
    con = db.connect(collection)
    db.set_scope(con, {"embed_mode": "all"})
    row = _doc(con)

    monkeypatch.setitem(
        worker.STAGES, "embed",
        lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    worker._execute_step(con, collection, "embed", row)

    stage, picked = worker._next_embed(con, db.get_scope(con))
    assert stage == "embed" and picked["uid"] == "g:1", \
        "a deferred document must still be selectable next round"
