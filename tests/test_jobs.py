"""Work the web queues and the worker does (app/jobs.py): what needs the
node's private key — creating a collection, exporting a bundle (operator,
2026-09-29)."""
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, jobs, node_state
from app.bundles import export as bundle_export


@pytest.fixture
def con(isolated_config):
    with closing(jobs.connect()) as c:
        yield c


def quiet(msg):
    pass


def test_the_claim_walks_the_index(con):
    """Selection queries are plan-checked (CLAUDE.md)."""
    sql = jobs.CLAIM_SQL.format("?,?")
    plan = " ".join(str(r[-1]) for r in con.execute(
        "EXPLAIN QUERY PLAN " + sql, (jobs.QUEUED, jobs.CREATE, jobs.EXPORT)))
    assert "idx_jobs_state" in plan and "TEMP B-TREE" not in plan, plan


def test_a_create_waits_for_a_mode_that_embeds(con, monkeypatch):
    """It starts the encoder (operator, 2026-09-29): it stays queued, and
    says why, until everything runs."""
    ran = []
    monkeypatch.setattr(jobs, "_create", lambda args: ran.append(args) or {"ok": 1})
    job_id = jobs.submit(jobs.CREATE, {"plugin": "gutenberg"})
    for mode in ("paused", "seed-only", "acquire-only"):
        assert jobs.run_next(con, mode, log=quiet) is None
        assert "waiting" in jobs.waiting_because(jobs.get(job_id), mode)
    assert ran == []
    done = jobs.run_next(con, "running", log=quiet)
    assert done.id == job_id and done.state == jobs.DONE and done.result == {"ok": 1}


def test_an_export_runs_in_any_mode(con, monkeypatch):
    monkeypatch.setattr(jobs, "_export", lambda job_id, args: {"n": job_id})
    job_id = jobs.submit(jobs.EXPORT, {"collection": "gutenberg", "uids": []})
    assert jobs.run_next(con, "paused", log=quiet).state == jobs.DONE
    assert jobs.get(job_id).result == {"n": job_id}


def test_a_refusal_fails_the_job_and_names_it(con, monkeypatch):
    def refuse(args):
        raise ValueError("no platform image to pin")
    monkeypatch.setattr(jobs, "_create", refuse)
    jobs.submit(jobs.CREATE, {"plugin": "gutenberg"})
    done = jobs.run_next(con, "running", log=quiet)
    assert done.state == jobs.FAILED and "no platform image" in done.error


def test_a_busy_store_is_a_deferral_not_a_failure(con, monkeypatch):
    def busy(job_id, args):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(jobs, "_export", busy)
    job_id = jobs.submit(jobs.EXPORT, {"collection": "gutenberg", "uids": []})
    assert jobs.run_next(con, "running", log=quiet).state == jobs.QUEUED
    assert jobs.get(job_id).error is None


def test_a_job_the_last_worker_left_running_is_queued_again(con, isolated_config):
    """Neither kind writes anything durable before its end, so it runs again;
    an export's scratch — and only its own — is discarded."""
    c = collections_mod.default_collection()
    where = bundle_export.workspace(c)
    where.mkdir(parents=True, exist_ok=True)     # a Codex collection's state/, made on first use
    job_id = jobs.submit(jobs.EXPORT, {"collection": c.name, "uids": ["g:1"]})
    other = where / "bundle-export-job999-keep"
    other.mkdir()
    assert jobs.claim(con, jobs.KINDS).id == job_id
    mine = where / f"bundle-export-job{job_id}-abc"
    mine.mkdir()
    assert jobs.requeue_running(con) == [job_id]
    assert jobs.get(job_id).state == jobs.QUEUED
    assert not mine.exists() and other.exists()


def test_the_sweep_expires_exports_and_forgets_old_rows(con, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    old = time.time() - jobs.EXPORT_KEEP_S - 10
    exp = jobs.submit(jobs.EXPORT, {"collection": "g", "uids": []})
    con.execute("UPDATE jobs SET state=?, result=?, finished=? WHERE id=?",
                (jobs.DONE, f'{{"scratch": "{scratch}"}}', old, exp))
    gone = jobs.submit(jobs.CREATE, {"plugin": "g"})
    con.execute("UPDATE jobs SET state=?, finished=? WHERE id=?",
                (jobs.FAILED, time.time() - jobs.KEEP_ROWS_S - 10, gone))
    fresh = jobs.submit(jobs.CREATE, {"plugin": "g"})
    jobs.sweep(con)
    assert jobs.get(exp).state == jobs.EXPIRED and not scratch.exists()
    assert jobs.get(gone) is None
    assert jobs.get(fresh).state == jobs.QUEUED


def test_an_exports_file_outside_its_workspace_is_not_served(tmp_path):
    """The row lives in state/; a path read from it is not trusted."""
    outside = tmp_path / "elsewhere.tar.gz"
    outside.write_bytes(b"x")
    ws = tmp_path / "ws"
    ws.mkdir()
    job = jobs.Job(id=1, kind=jobs.EXPORT, args={}, state=jobs.DONE,
                   result={"file": str(outside)}, error=None,
                   created=0.0, started=0.0, finished=0.0)
    assert jobs.export_file(job, ws) is None
    inside = ws / "b.tar.gz"
    inside.write_bytes(b"x")
    assert jobs.export_file(jobs.Job(**{**job.__dict__, "result": {"file": str(inside)}}),
                            ws) == inside


@pytest.fixture
def client(isolated_config, monkeypatch):
    from app.api import app
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def test_the_librarys_export_is_built_by_the_worker_and_served_once(client, monkeypatch,
                                                                    tmp_path):
    """Operator, 2026-09-21: "export bundle should be done in tank only" —
    still: the worker builds it under EXPORT_DIR."""
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", bulk)
    seen = {}

    def fake_export(con, collection, uids, dest, **kw):
        seen["dest"] = Path(dest)
        Path(dest).mkdir(parents=True)
        (Path(dest) / "bundle.parquet").write_bytes(b"x")
        return {"chunks": 1, "uids": len(uids), "bundle_id": "ab" * 32}
    monkeypatch.setattr(bundle_export, "export_bundle", fake_export)
    r = client.post("/admin/library/gutenberg/bundle/export", json={"uids": ["gutenberg:1"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    early = client.get(f"/admin/library/gutenberg/bundle/export/{job_id}")
    assert early.status_code == 409, "nothing to download before the worker ran it"

    with closing(jobs.connect()) as con:
        assert jobs.run_next(con, node_state.worker_state(con), log=quiet).state == jobs.DONE
    assert seen["dest"].is_relative_to(bulk / "gutenberg")
    view = client.get(f"/api/jobs/{job_id}").json()
    assert view["state"] == "done" and "file" not in view["result"] \
        and "scratch" not in view["result"], "where it was built is not the page's"

    got = client.get(f"/admin/library/gutenberg/bundle/export/{job_id}")
    assert got.status_code == 200 and got.headers["content-type"] == "application/gzip"
    assert list((bulk / "gutenberg").iterdir()) == [], "served once, then removed"
    assert jobs.get(job_id).state == jobs.EXPIRED
    assert client.get(f"/admin/library/gutenberg/bundle/export/{job_id}").status_code == 409


def test_the_jobs_route_needs_the_admin_token(client):
    job_id = jobs.submit(jobs.CREATE, {"plugin": "g"})
    assert client.get(f"/api/jobs/{job_id}").status_code == 200
    assert client.get("/api/jobs/999999").status_code == 404
    del client.headers["X-Admin-Token"]
    assert client.get(f"/api/jobs/{job_id}").status_code == 401
    assert client.get(f"/admin/library/gutenberg/bundle/export/{job_id}").status_code == 401
