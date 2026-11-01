"""Requests filled across documents (app/embeddings.Coalescer; the GPU plan's
step 1, lever 2).

München, 2026-11-01: wikidata averages 1.04 chunks a document and a request
carried one document's chunks, so the gated batch of 16 never filled — ~76
requests a second of ~1 text, the card computing 82 % of the time. The
texts of the documents in flight now share requests of up to the batch.
"""
from __future__ import annotations

import threading
import time

import pytest

from app import collections as collections_mod, config, embeddings, worker

RECIPE = collections_mod.EmbedRecipe(url="http://enc", api="openai", model="m",
                                     dtype="f16", ctx=512, batch=4)


class Encoder:
    """A fake `_embed_batch`: each text's vector is [len(text)], each request
    recorded; `slow` holds the first request so the others queue."""

    def __init__(self, slow: float = 0.0, refuse: str | None = None,
                 down: BaseException | None = None, run: str = "sha256:" + "a" * 64):
        self.requests: list[list[str]] = []
        self.slow = slow
        self.refuse = refuse
        self.down = down
        self.run = run
        self.lock = threading.Lock()

    def __call__(self, batch, timeout, recipe, *, raw=False, runs=None):
        with self.lock:
            self.requests.append(list(batch))
            first = len(self.requests) == 1
        if first and self.slow:
            time.sleep(self.slow)
        if self.down is not None:
            raise self.down
        if self.refuse is not None and self.refuse in batch:
            raise embeddings.InputRefused(f"refused: {self.refuse!r}")
        if runs is not None:
            runs.add(self.run)
        return [[float(len(t))] for t in batch]


@pytest.fixture
def encoder(monkeypatch):
    def make(**kw):
        enc = Encoder(**kw)
        monkeypatch.setattr(embeddings, "_embed_batch", enc)
        return enc
    return make


def _callers(co, texts_per_caller, results, errors, runs=None):
    threads = []
    for n, texts in enumerate(texts_per_caller):
        def run(n=n, texts=texts):
            try:
                results[n] = co.embed(texts, runs=None if runs is None else runs[n], timeout=5)
            except BaseException as e:   # noqa: BLE001
                errors[n] = e
        t = threading.Thread(target=run)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(10)


def test_one_text_documents_share_requests_of_the_gated_batch(encoder):
    enc = encoder(slow=0.3)
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    try:
        texts = [["a" * (n + 1)] for n in range(9)]
        results, errors = {}, {}
        _callers(co, texts, results, errors)
    finally:
        co.close()
    assert errors == {}
    assert all(results[n] == [[float(n + 1)]] for n in range(9)), "each its own vector"
    assert max(len(r) for r in enc.requests) == 4 and all(len(r) <= 4 for r in enc.requests)
    assert sum(len(r) for r in enc.requests) == 9 and len(enc.requests) <= 4
    assert (co.requests, co.texts) == (len(enc.requests), 9)


def test_a_long_documents_texts_come_back_in_order_across_requests(encoder):
    enc = encoder()
    co = embeddings.Coalescer(RECIPE, size=4, lanes=3)
    try:
        texts = ["x" * (i + 1) for i in range(11)]
        assert co.embed(texts) == [[float(i + 1)] for i in range(11)]
    finally:
        co.close()
    assert all(len(r) <= 4 for r in enc.requests) and len(enc.requests) == 3


def test_each_document_learns_the_run_that_answered_it(encoder):
    encoder(slow=0.2, run="sha256:" + "b" * 64)
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    runs = {0: set(), 1: set(), 2: set()}
    results, errors = {}, {}
    try:
        _callers(co, [["a"], ["bb"], ["ccc"]], results, errors, runs=runs)
    finally:
        co.close()
    assert errors == {} and all(r == {"sha256:" + "b" * 64} for r in runs.values())


def test_a_refused_text_is_its_own_documents_error_and_the_others_are_answered(encoder):
    enc = encoder(slow=0.2, refuse="bad")
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    results, errors = {}, {}
    try:
        _callers(co, [["first"], ["ok"], ["bad"], ["fine"]], results, errors)
    finally:
        co.close()
    assert set(errors) == {2} and isinstance(errors[2], embeddings.InputRefused)
    assert results[1] == [[2.0]] and results[3] == [[4.0]]
    assert ["bad"] in enc.requests, "sent again on its own: the error lands on its document"


def test_an_endpoint_fault_is_every_documents_deferral_never_one_request_each(encoder):
    enc = encoder(slow=0.2, down=embeddings.EndpointBusy("503"))
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    results, errors = {}, {}
    try:
        _callers(co, [["a"], ["b"], ["c"]], results, errors)
    finally:
        co.close()
    assert set(errors) == {0, 1, 2}
    assert all(isinstance(e, embeddings.EndpointBusy) for e in errors.values())
    assert len(enc.requests) <= 2, "not multiplied into one request per document"


def test_a_closed_stream_defers_what_still_waits_and_its_lanes_end(encoder):
    encoder(slow=0.5)
    co = embeddings.Coalescer(RECIPE, size=1, lanes=1)
    results, errors = {}, {}
    t = threading.Thread(target=_callers, args=(co, [["a"], ["b"], ["c"]], results, errors))
    t.start()
    time.sleep(0.1)
    co.close(wait_s=5)
    t.join(10)
    assert any(isinstance(e, embeddings.EndpointBusy) for e in errors.values()), "deferred"
    assert not any(th.is_alive() for th in co._threads)
    with pytest.raises(embeddings.EndpointBusy):
        co.embed(["late"])


def test_a_caller_waits_a_bounded_time_and_its_texts_leave_the_queue(encoder, monkeypatch):
    gate = threading.Event()

    def stuck(batch, timeout, recipe, *, raw=False, runs=None):
        gate.wait(5)
        return [[1.0] for _ in batch]
    monkeypatch.setattr(embeddings, "_embed_batch", stuck)
    co = embeddings.Coalescer(RECIPE, size=1, lanes=1)
    try:
        threading.Thread(target=lambda: co.embed(["holds the lane"], timeout=5), daemon=True).start()
        time.sleep(0.05)
        with pytest.raises(embeddings.EndpointTimedOut):
            co.embed(["waits"], timeout=0.05)
        assert co._waiting == [], "a deferred caller's texts are not sent after it"
    finally:
        gate.set()
        co.close()


def test_only_its_own_encoders_document_side_goes_through_it(encoder):
    enc = encoder()
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    other = collections_mod.EmbedRecipe(url="http://other", api="openai", model="m",
                                        dtype="f16", ctx=512, batch=4)
    try:
        with embeddings.coalescing(co):
            embeddings._embed_texts(["doc"], recipe=RECIPE)
            embeddings._embed_texts(["query"], recipe=RECIPE, query=True)
            embeddings._embed_texts(["elsewhere"], recipe=other)
    finally:
        co.close()
    assert co.texts == 1, "a query and another encoder are sent as before"
    assert len(enc.requests) == 3


# ---------------------------------------------------------------- the feeder

def test_a_streams_short_documents_share_requests(tmp_path, monkeypatch, encoder):
    from tests.test_embed_producers import _collection, _feed_once
    from dataclasses import replace
    c, con = _collection(tmp_path)
    rows = _feed_once(monkeypatch, con)
    enc = encoder(slow=0.3)
    recipe = replace(RECIPE, batch=16)
    monkeypatch.setattr(config, "EMBED_PLUGIN_SLOTS", 1)
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": row["uid"]}])
    inside = {"n": 0, "max": 0}
    lock = threading.Lock()

    def upsert(book_id, lang, chunks, collection, lock_con=None, stop=None, **kw):
        with lock:
            inside["n"] += 1
            inside["max"] = max(inside["max"], inside["n"])
        try:
            embeddings.embed_chunks(collection, chunks, recipe=recipe)
        finally:
            with lock:
                inside["n"] -= 1
        return len(chunks)
    monkeypatch.setattr(embeddings, "upsert_book", upsert)
    assert worker._embed_parallel(con, c, recipe, slots=1)
    assert inside["max"] == len(rows), "every short document submitted at once (up to 16 here)"
    assert max(len(r) for r in enc.requests) >= 2, "a request carried several documents' texts"
    assert sum(len(r) for r in enc.requests) == len(rows)
    assert con.execute("SELECT COUNT(*) FROM documents WHERE embed_done=1").fetchone()[0] == len(rows)
    logged = [m for (m,) in con.execute("SELECT message FROM events WHERE stage='embed'")]
    assert any(m.startswith("requests filled across documents:") for m in logged)


def test_a_batch_of_one_changes_nothing(tmp_path, monkeypatch, encoder):
    from tests.test_embed_producers import RECIPE as ONE, _collection, _feed_once
    c, con = _collection(tmp_path)
    _feed_once(monkeypatch, con)
    made = []
    monkeypatch.setattr(embeddings, "Coalescer", lambda *a, **k: made.append(1))
    monkeypatch.setattr(worker, "_load_chunks", lambda row, collection: [{"text": "x"}])
    monkeypatch.setattr(embeddings, "upsert_book", lambda *a, **k: 1)
    worker._embed_parallel(con, c, ONE, slots=2)
    assert made == []


def test_a_shared_timeout_is_proved_on_the_slow_text_alone_when_the_endpoint_answers(monkeypatch):
    """encoder_timeouts' rule: a timeout is the endpoint's until one document
    proves it its own. Shared, it would mark every batch-mate, and after
    BLAME_AFTER put an error on innocent documents."""
    sent = []
    gate = threading.Event()

    def enc(batch, timeout, recipe, *, raw=False, runs=None):
        sent.append(list(batch))
        if len(sent) == 1:
            gate.wait(0.3)                   # the others queue behind the first
        if "slow" in batch:
            raise embeddings.EndpointTimedOut("read timeout")
        return [[float(len(t))] for t in batch]
    monkeypatch.setattr(embeddings, "_embed_batch", enc)
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    results, errors = {}, {}
    try:
        _callers(co, [["first"], ["ok"], ["slow"], ["fine"]], results, errors)
    finally:
        co.close()
    assert set(errors) == {2} and isinstance(errors[2], embeddings.EndpointTimedOut)
    assert results[1] == [[2.0]] and results[3] == [[4.0]]
    assert [embeddings.ALIVE_PROBE] in sent, "asked whether the endpoint answers at all"


def test_a_hung_endpoints_timeout_is_everyones_and_is_not_multiplied(monkeypatch):
    sent = []

    def hung(batch, timeout, recipe, *, raw=False, runs=None):
        sent.append(list(batch))
        if len(sent) == 1:
            time.sleep(0.3)
        raise embeddings.EndpointTimedOut("read timeout")
    monkeypatch.setattr(embeddings, "_embed_batch", hung)
    co = embeddings.Coalescer(RECIPE, size=4, lanes=1)
    results, errors = {}, {}
    try:
        _callers(co, [["a"], ["b"], ["c"]], results, errors)
    finally:
        co.close()
    assert set(errors) == {0, 1, 2}
    assert all(isinstance(e, embeddings.EndpointTimedOut) for e in errors.values())
    assert len(sent) <= 4, "the batches and one probe each, never one request per document"
