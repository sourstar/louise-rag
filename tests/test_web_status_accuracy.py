"""Status surfaces must describe the state the operator is actually in.

Reported live: a genome collection was embedding at full CPU while its
Library card read "lazy-embed queue: 0 pending" — true (backfill never
touches that queue) and useless. The same family: blocked documents that
no stage will ever retry, a gate pause that lives only in the worker's
memory, and a System log that read one collection's events out of five.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, spaces, web
from app.api import app
from app.verify import backend_gate


@pytest.fixture
def client(isolated_config, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc123", False))
    monkeypatch.setattr(web, "_ping", lambda url: True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as c:
        c.headers["X-Admin-Token"] = "sekrit"
        yield c


def _seed(con, n=5, *, fts_done=1, error_rows=0):
    for i in range(1, n + 1):
        con.execute(
            "INSERT INTO documents(uid,source,native_id,path,lang,in_scope,"
            "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,?,0)",
            (f"gutenberg:{i}", "gutenberg", str(i), f"/x/{i}", "en", fts_done))
    for i in range(1, error_rows + 1):
        con.execute("UPDATE documents SET error='boom' WHERE uid=?",
                    (f"gutenberg:{i}",))


def _set_mode(con, mode):
    scope = db.get_scope(con)
    scope["embed_mode"] = mode
    db.set_scope(con, scope)


def test_card_reports_backfill_not_the_empty_queue_under_all(client):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 5)
    _set_mode(con, "all")

    page = client.get("/admin/library").text
    assert "dense backfill: 5 documents awaiting the encoder" in page
    assert "lazy-embed queue" not in page, \
        "the queue count is always 0 under 'all' and misdescribes progress"


def test_card_keeps_the_queue_under_lazy(client):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 3)
    _set_mode(con, "lazy")
    con.execute("INSERT INTO embed_queue(uid,priority,ts) VALUES('gutenberg:1',1,1)")

    page = client.get("/admin/library").text
    assert "lazy-embed queue: 1 pending" in page


def test_card_names_blocked_documents(client):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 5, error_rows=2)

    page = client.get("/admin/library").text
    assert "2 blocked" in page
    assert "exhausted their retries" in page
    # and they are excluded from the embeddable backlog
    assert "3 documents awaiting the encoder" in page or "lazy-embed queue" in page


def test_card_surfaces_a_gate_pause(client):
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    _seed(con, 2)
    db.set_control(con, backend_gate.GATE_STATE_KEY, json.dumps(
        {"allowed": False, "reason": "probe cosine 0.41 < tau", "ts": 1}))

    page = client.get("/admin/library").text
    assert "embedding paused" in page
    assert "probe cosine 0.41" in page


def test_gate_records_its_verdict_for_the_web_process(isolated_config, monkeypatch):
    """The verdict lives in a worker-process cache the UI cannot read, so a
    pause looked exactly like a stalled progress bar."""
    collection = collections_mod.default_collection()
    con = db.connect(collection)
    backend_gate.reset_cache()
    monkeypatch.setattr(config, "BACKEND_GATE", "on")

    def down(texts):
        raise ConnectionError("endpoint down")
    assert backend_gate.embed_allowed(con, collection, embed_fn=down) is False
    state = backend_gate.gate_state(con)
    assert state and state["allowed"] is False and state["reason"]


def test_system_log_merges_every_collection(client, isolated_config, tmp_path):
    """One collection's events out of five made the System page look calm
    while another collection was stuck."""
    first = collections_mod.default_collection()
    db.log_event(db.connect(first), "info", "worker", "hello from gutenberg")

    def _declare(name: str, base):
        path = collections_mod.config_toml_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'plugin = "gutenberg"\n'
            f'corpus_dir = "{base.as_posix()}"\n\n[paths]\n'
            f'manifest = "{(base / "manifest.db").as_posix()}"\n'
            f'fts = "{(base / "fts").as_posix()}"\n'
            f'lancedb = "{(base / "lancedb").as_posix()}"\n',
            encoding="utf-8")

    # declaring ANY toml hides the synthesized default, so materialize the
    # first collection alongside — the same rule the create flow follows
    _declare("gutenberg", first.manifest_path.parent)
    _declare("second", tmp_path / "c2")
    other = [c for c in collections_mod.load_all() if c.name == "second"][0]
    db.log_event(db.connect(other), "error", "genesis", "shard REJECTED here")

    fragment = client.get("/admin/system/log").text
    assert "hello from gutenberg" in fragment
    assert "shard REJECTED here" in fragment, \
        "events from every collection must reach the System log"
    assert "second" in fragment      # tagged with its collection
