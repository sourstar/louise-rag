"""tests/test_node_show.py — `louise node show` (07 §7.3; app/node_show.py):
the mode and every runtime override in force, each collection's state and
stage, the last round as the worker logged it, the verbs running under the
node; read-only — a show on a node that never booted creates nothing."""
import json

from app import db, jobs, node_show, node_state
from app import collections as collections_mod


def test_a_show_on_a_node_that_never_booted_creates_nothing(isolated_config):
    out = node_show.show()
    assert not node_state.path().exists(), "peeked, never opened for writing"
    assert out["mode"] == "paused" and out["overrides"] == {"mode": "paused", "paused_collections": [],
                                                            "bandwidth_bps": None}
    (row,) = out["collections"]
    from app import copy_state
    c = collections_mod.by_name("gutenberg")
    assert row["name"] == "gutenberg" and row["collection_id"] == c.codex.collection_id
    assert row["state"] == copy_state.of(c).state, "the state is the copy's own (07 §7.8)"
    assert row["paused"] is False and row["integrity_violated"] == [] and out["jobs"] == []


def test_overrides_jobs_and_the_last_round_are_shown(isolated_config, capsys):
    node_state.set_worker_state("running")
    node_state.set_paused("gutenberg", True)
    node_state.set_bandwidth_override(1_000_000.0, 600)
    job = jobs.submit(jobs.CREATE, {"recipe": "x.toml"})
    c = collections_mod.load_all()[0]
    con = db.connect(c)
    db.log_event(con, "info", "sched", "an old round")
    con.execute("UPDATE events SET ts = ts - 3600")
    db.log_event(con, "info", "sched", "embed — gutenberg: 100% in 3 turns")
    db.log_event(con, "info", "sched", "parse pool — 4 in flight")
    db.log_event(con, "info", "embed", "not a round line")
    con.close()
    out = node_show.show()
    assert out["mode"] == "running"
    assert out["overrides"]["paused_collections"] == ["gutenberg"] and out["overrides"]["bandwidth_bps"] == 1e6
    assert [j["id"] for j in out["jobs"]] == [job] and out["jobs"][0]["state"] == "queued"
    assert [l["line"] for l in out["last_round"]] == ["embed — gutenberg: 100% in 3 turns",
                                                     "parse pool — 4 in flight"], "the newest round, in order"
    assert out["collections"][0]["paused"] is True
    from app import louise
    capsys.readouterr()                        # the log lines above went to stdout too
    assert louise.main(["node", "show"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "running" and printed["collections"][0]["name"] == "gutenberg"


def test_the_route_is_registered_behind_the_admin_token():
    from app import api
    route = next(r for r in api.app.routes if getattr(r, "path", "") == "/api/node/show")
    assert "GET" in route.methods and route.dependant.dependencies, "admin only"
