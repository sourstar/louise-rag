"""Every command is one route, named by the tree (07 §7.4; operator,
2026-10-25: "Http api, I choose A"): app/api_commands.py."""
from __future__ import annotations

import threading
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app import api_commands as ac, config, jobs


# ---- the tree -------------------------------------------------------------

def test_a_command_resolves_to_its_parser():
    path, p = ac.resolve(["hold", "collection", "show"])
    assert path == ("hold", "collection", "show") and "codex_id" in {a.dest for a in p._actions}


@pytest.mark.parametrize("words,why", [
    (["hold", "nothing"], "no command"),
    (["hold", "collection"], "not a whole command"),
    (["node", "start"], "not routed"),
    (["ask", "search"], "not routed"),
    (["user", "add"], "not routed"),
    (["dev", "carry"], "not routed"),
])
def test_what_is_not_a_route(words, why):
    with pytest.raises(ac.Refused, match=why) as e:
        ac.resolve(words)
    assert e.value.status == 404


def test_the_body_is_the_flags_and_the_positionals_by_name():
    path, p = ac.resolve(["hold", "vectors", "import"])
    argv = ac.argv_for(path, p, {"collection": "pubmed", "archive": "medcpt-pubmed",
                                 "shards": ["a", "b"], "strict": True, "require-t3": False})
    assert argv == ["hold", "vectors", "import", "--archive", "medcpt-pubmed",
                    "--shards", "a", "b", "--strict", "pubmed"]


def test_json_is_asked_of_a_command_that_takes_it():
    path, p = ac.resolve(["hold", "collection", "show"])
    assert ac.argv_for(path, p, {"codex_id": "x"})[-2:] == ["--json", "x"]


@pytest.mark.parametrize("values,match", [
    ({"collection": "c", "nonsense": 1}, "takes no nonsense"),
    ({"collection": "--yes"}, "may not begin with '-'"),
    ({"collection": "c", "dry_run": "yes"}, "true or false"),
    ({}, "required"),
])
def test_the_commands_own_parser_refuses(values, match):
    path, p = ac.resolve(["hold", "collection", "forget"])
    with pytest.raises(ac.Refused, match=match):
        ac.argv_for(path, p, values)


def test_the_exit_code_is_the_status():
    assert ac.STATUS == {0: 200, 1: 500, 2: 409, 3: 503}


# ---- over HTTP ------------------------------------------------------------

@pytest.fixture
def client(isolated_config, monkeypatch):
    from app.api import app
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_a_read_runs_the_cli_and_answers_its_exit(client):
    r = client.get("/api/hold/collection/list")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exit"] == 0 and body["cli"] == "louise hold collection list"


def test_a_read_that_fails_is_its_exit_codes_status(client, monkeypatch):
    monkeypatch.setattr(ac, "run", lambda argv, timeout=None, stop=None:
                        {"exit": 2, "status": 409, "output": None, "message": "refused"})
    r = client.get("/api/hold/collection/show", params={"codex_id": "nothing"})
    assert r.status_code == 409 and r.json()["message"] == "refused"


def test_a_write_is_queued_for_the_worker(client):
    r = client.post("/api/node/pause", json={"seed_only": True})
    assert r.status_code == 202, r.text
    job = jobs.get(r.json()["id"])
    assert job.kind == jobs.COMMAND and job.args["argv"] == ["node", "pause", "--seed-only"]
    assert client.get(r.json()["job"]).json()["state"] == jobs.QUEUED


def test_the_wrong_method_is_405(client):
    assert client.post("/api/hold/collection/list").status_code == 405
    assert client.get("/api/node/pause").status_code == 405


def test_an_explicit_route_answers_first(client):
    """`/api/node/show` has its own route: the generic one never sees it."""
    r = client.get("/api/node/show")
    assert r.status_code == 200 and "cli" not in r.json()


def test_without_the_admin_token_nothing_runs(client):
    del client.headers["X-Admin-Token"]
    assert client.get("/api/hold/collection/list").status_code == 401
    assert client.post("/api/node/pause", json={}).status_code == 401


# ---- the worker's command thread ------------------------------------------

def _serve_until(predicate, monkeypatch, run):
    monkeypatch.setattr(ac, "run", run)
    stop = threading.Event()
    t = threading.Thread(target=jobs.serve_commands, args=(stop,),
                         kwargs={"poll_s": 0.01, "log": lambda m: None})
    t.start()
    try:
        deadline = time.time() + 10
        while not predicate() and time.time() < deadline:
            time.sleep(0.02)
    finally:
        stop.set()
        t.join(timeout=10)
    return predicate()


def test_the_worker_runs_a_command_and_keeps_its_answer(isolated_config, monkeypatch):
    job_id = jobs.submit(jobs.COMMAND, {"argv": ["node", "resume"], "command": "node resume"})
    ok = {"exit": 0, "status": 200, "output": {"node": "running"}, "message": ""}
    assert _serve_until(lambda: jobs.get(job_id).state == jobs.DONE, monkeypatch,
                        lambda argv, timeout=None, stop=None: ok)
    assert jobs.get(job_id).result == ok


def test_a_failed_command_keeps_its_exit_and_message(isolated_config, monkeypatch):
    job_id = jobs.submit(jobs.COMMAND, {"argv": ["hold", "check", "x"], "command": "hold check"})
    bad = {"exit": 2, "status": 409, "output": None, "message": "no collection x"}
    assert _serve_until(lambda: jobs.get(job_id).state == jobs.FAILED, monkeypatch,
                        lambda argv, timeout=None, stop=None: bad)
    job = jobs.get(job_id)
    assert job.result["exit"] == 2 and job.error == "no collection x"


def test_a_command_the_stop_interrupts_is_queued_again(isolated_config, monkeypatch):
    job_id = jobs.submit(jobs.COMMAND, {"argv": ["hold", "records", "build", "x"],
                                        "command": "hold records build"})
    seen = []

    def run(argv, timeout=None, stop=None):
        seen.append(argv)
        stop.wait(10)
        return {"exit": 130, "status": 500, "output": None, "message": "", "interrupted": True}

    _serve_until(lambda: bool(seen), monkeypatch, run)
    assert jobs.get(job_id).state == jobs.QUEUED


def test_the_child_is_the_cli_itself(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setenv("LOUISE_ROOT", str(tmp_path))
    got = ac.run(["hold", "collection", "nothing-here"], timeout=60)
    assert got["exit"] == 2 and got["status"] == 409 and "invalid choice" in got["message"]
