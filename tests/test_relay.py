"""The Ask page's relay (app/relay.py; operator, 2026-10-28): a question is a
job that outlives the page's connection. A listener that comes back gets
what it missed; a job nobody listens to is cancelled after a grace; a job
that makes no progress is SAID to be stuck; a finished job is kept a while
for a page that reloads; a job is its owner's alone."""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import cancel, relay, web


@pytest.fixture(autouse=True)
def _clean():
    relay.reset()
    yield
    relay.reset()


def _wait(pred, timeout=5.0):
    t0 = time.monotonic()
    while not pred():
        assert time.monotonic() - t0 < timeout, "timed out"
        time.sleep(0.01)


def test_a_listener_that_comes_back_gets_what_it_missed():
    gate = threading.Event()

    def work(job):
        job.emit("result", n=1)
        gate.wait(5)
        job.emit("result", n=2)
    job = relay.start("search", "user:a", work)
    _wait(lambda: job.last_seq >= 1)
    first = job.since(0)
    assert [e.kind for e in first] == ["result"] and first[0].seq == 1
    gate.set()
    _wait(lambda: job.state == relay.DONE)
    later = job.since(first[-1].seq)          # the reconnect's Last-Event-ID
    assert [(e.kind, e.data.get("n", e.data.get("state"))) for e in later] == [("result", 2), ("end", "done")]


def test_a_job_nobody_listens_to_is_cancelled_after_the_grace(monkeypatch):
    """The rule of 2026-09-21 (no search runs for nobody), with room for a
    reconnect: cancelled through the token the searches already honour."""
    monkeypatch.setattr(relay, "GRACE_S", 30.0)

    def work(job):
        while True:
            cancel.checkpoint()
            time.sleep(0.01)
    job = relay.start("search", "user:a", work)
    job.attach()
    job.detach()                               # the browser dropped the connection
    relay.tick(now=time.monotonic() + 29)
    assert job.state == relay.RUNNING, "inside the grace: the page may come back"
    job.attach()                               # it came back
    relay.tick(now=time.monotonic() + 1000)
    assert job.state == relay.RUNNING, "a listener keeps it"
    job.detach()
    relay.tick(now=time.monotonic() + 31)
    _wait(lambda: job.state == relay.DROPPED)
    assert job.reason == "nobody listened to it for 30 s"
    assert job.since(0)[-1].kind == "end" and job.since(0)[-1].data["state"] == "dropped"


def test_a_job_without_progress_is_said_to_be_stuck_and_then_moving(monkeypatch):
    monkeypatch.setattr(relay, "STUCK_S", 60.0)
    gate = threading.Event()

    def work(job):
        cancel.checkpoint()                    # progress, where: work
        gate.wait(5)
        cancel.checkpoint()
    job = relay.start("search", "user:a", work)
    job.attach()
    _wait(lambda: job.token.where == "work")
    relay.tick(now=job.token.last_progress + 59)
    assert [e.kind for e in job.since(0)] == [], "slow is not stuck"
    relay.tick(now=job.token.last_progress + 61)
    stuck = job.since(0)[-1]
    assert stuck.kind == "stuck" and stuck.data["where"] == "work" and stuck.data["quiet_s"] >= 60
    relay.tick(now=job.token.last_progress + 90)
    assert len(job.since(0)) == 1, "said once"
    job.touch("answer")                        # progress again
    relay.tick()
    assert job.since(0)[-1].kind == "moving" and not job.stuck
    assert job.state == relay.RUNNING, "stuck is said, never cancelled"
    gate.set()


def test_a_finished_job_is_kept_for_a_reload_then_forgotten(monkeypatch):
    monkeypatch.setattr(relay, "KEEP_S", 600.0)
    job = relay.start("search", "user:a", lambda j: j.emit("result", n=1))
    _wait(lambda: job.state == relay.DONE)
    relay.tick(now=job.ended + 599)
    assert relay.get(job.id, "user:a") is job
    relay.tick(now=job.ended + 601)
    assert relay.get(job.id, "user:a") is None


def test_a_job_is_its_owners_and_a_new_question_replaces_the_old_one():
    gate = threading.Event()

    def work(job):
        while not gate.is_set():
            cancel.checkpoint()
            time.sleep(0.01)
    old = relay.start("search", "user:a", work)
    assert relay.get(old.id, "user:b") is None, "an id read off another screen opens nothing"
    assert not relay.cancel_job(old.id, "user:b", "x")
    new = relay.start("search", "user:a", lambda j: None, replaces=[old.id])
    _wait(lambda: old.state == relay.DROPPED)
    assert old.reason == "replaced by a new question"
    _wait(lambda: new.state == relay.DONE)
    gate.set()


def test_a_failure_is_an_end_that_says_why():
    def work(job):
        raise RuntimeError("the reranker answered 500")
    job = relay.start("search", "user:a", work)
    _wait(lambda: job.state == relay.FAILED)
    end = job.since(0)[-1]
    assert end.kind == "end" and end.data["state"] == "failed"
    assert end.data["reason"] == "RuntimeError: the reranker answered 500"


# ---- the routes ---------------------------------------------------------------

def _events(text: str) -> list[tuple[str | None, str, dict]]:
    out = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines()
                      if ": " in line and not line.startswith(":"))
        if "event" in fields:
            out.append((fields.get("id"), fields["event"], json.loads(fields["data"])))
    return out


def test_the_page_starts_a_search_and_reads_it_back_after_a_drop(isolated_config, monkeypatch):
    from app.api import app
    monkeypatch.setattr(web, "_ask_result", lambda body: {"hits": [], "timings": {"fts": 1}})
    c = TestClient(app)
    r = c.post("/ask/relay", json={"kind": "search", "search": {"query": "whales"}})
    assert r.status_code == 200
    jid = r.json()["id"]
    job = relay.get(jid, "guest:None")
    _wait(lambda: job.state == relay.DONE)
    got = _events(c.get(f"/ask/relay/{jid}/events").text)
    assert [k for _i, k, _d in got] == ["status", "result", "end"]
    assert got[0][0] is None, "the status is not replayed: it has no id"
    result = got[1][2]
    assert "search_id" in result and web.kept_search(result["search_id"], "whales") is not None, \
        "the answer can be written from this search (one question, one search)"
    # the browser's reconnect names the last event it saw: only what follows
    again = _events(c.get(f"/ask/relay/{jid}/events", headers={"Last-Event-ID": "1"}).text)
    assert [(i, k) for i, k, _d in again] == [(None, "status"), ("2", "end")]
    assert c.get(f"/ask/relay/{jid}").json()["state"] == "done"


def test_an_unknown_job_is_a_404_that_says_why(isolated_config):
    from app.api import app
    c = TestClient(app)
    r = c.get("/ask/relay/nope/events")
    assert r.status_code == 404 and "restarted" in r.json()["detail"]


def test_the_reader_cancels_a_question(isolated_config, monkeypatch):
    from app.api import app
    gate = threading.Event()

    def slow(body):
        while not gate.is_set():
            cancel.checkpoint()
            time.sleep(0.01)
        return {"hits": [], "timings": {}}
    monkeypatch.setattr(web, "_ask_result", slow)
    c = TestClient(app)
    jid = c.post("/ask/relay", json={"kind": "search", "search": {"query": "q"}}).json()["id"]
    assert c.delete(f"/ask/relay/{jid}").json() == {"cancelled": True}
    job = relay.get(jid, "guest:None")
    _wait(lambda: job.state == relay.DROPPED)
    assert job.reason == "cancelled by the reader"
    gate.set()


def test_an_answer_job_relays_the_models_words(isolated_config, monkeypatch):
    """The answer through the same `answer_parts` as /v1/chat/completions."""
    from app import api, llm
    from app.api import app

    class End:
        whole = True
    def stream(messages, temperature=0.3):
        yield "The whale "
        yield "rose [1]."
        return End()
    monkeypatch.setattr(llm, "chat_stream", stream)
    monkeypatch.setattr(web, "_ask_result", lambda body: {"hits": [{"chunk_id": "gutenberg:1:0:1", "book_id": 1, "uid": "u1", "text": "w",
                                                                  "source_collection": "gutenberg"}],
                                                         "timings": {"total": 1.0}})
    monkeypatch.setattr(api.retrieval, "build_answer_messages",
                        lambda q, r, answer_lang=None: [{"role": "system", "content": "s"},
                                                        {"role": "user", "content": q}])
    c = TestClient(app)
    sid = c.post("/ask/relay", json={"kind": "search", "search": {"query": "whales"}}).json()["id"]
    _wait(lambda: relay.get(sid, "guest:None").state == relay.DONE)
    search_id = relay.get(sid, "guest:None").since(0)[0].data["search_id"]
    aid = c.post("/ask/relay", json={"kind": "answer", "answer": {
        "messages": [{"role": "user", "content": "whales"}], "search_id": search_id}}).json()["id"]
    job = relay.get(aid, "guest:None")
    _wait(lambda: job.state == relay.DONE)
    got = _events(c.get(f"/ask/relay/{aid}/events").text)
    assert "".join(d["text"] for _i, k, d in got if k == "delta") == "The whale rose [1]."
    assert got[-1][1] == "end" and got[-1][2]["state"] == "done"
