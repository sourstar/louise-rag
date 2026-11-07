"""tests/test_collection_freeze.py — FREEZE, the node's side (operator,
2026-11-07: "no updated are fetched and it stays as it is (you can still
embed locally)"; docs/notes/collection-freeze.md; 07 §7.3 `node freeze`).

A frozen collection keeps the version it holds: its fetch stage does
nothing, its origin is not scanned — no document re-opened or added because
a file changed on disk — and everything else runs on that version. The
freeze is recorded with who and when, survives a restart, and its lifting
catches everything up. The share side is tests/share/test_frozen.py."""
import dataclasses
import json
import shutil
import threading
import types
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, louise, node_state, slice_scope, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


@pytest.fixture(autouse=True)
def frozen_state(monkeypatch):
    """The worker's frozen set, its events, its catch-ups and its throttles
    are process-wide: each test starts from none."""
    monkeypatch.setattr(worker, "_frozen_names", frozenset())
    monkeypatch.setattr(worker, "_frozen_events", {})
    monkeypatch.setattr(worker, "_catch_up_owed", set())
    monkeypatch.setattr(worker, "_said", {})
    worker._discover_requests.clear()
    worker._acquire_done.clear()
    yield
    worker._discover_requests.clear()
    worker._acquire_done.clear()
    worker._paused_aborts.clear()
    worker._take_owed_scans()


@pytest.fixture
def collection(isolated_config, tmp_path):
    corpus = tmp_path / "corpus_a"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    return collections_mod.Collection(
        name="alpha", plugin="gutenberg", profile="demo", embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", acquire_enabled=True)


def freeze(name: str, frozen: bool = True, by: str = "test") -> None:
    """The operator's command, then the worker's next round reading it."""
    node_state.set_frozen(name, frozen, by=by)
    worker._sync_frozen()


def events(con) -> list[str]:
    return [r["message"] for r in con.execute("SELECT message FROM events")]


# ---- where it lives -------------------------------------------------------------------

def test_a_freeze_is_recorded_with_who_and_when_and_survives_a_restart(isolated_config):
    assert node_state.frozen_collections() == set() and node_state.frozen_since("alpha") is None
    assert node_state.set_frozen("alpha", True, by="louise node freeze alpha (from bash, user op)") == {"alpha"}
    node_state.set_frozen("beta", True)
    since = node_state.frozen_since("alpha")
    assert since["by"] == "louise node freeze alpha (from bash, user op)" and since["when"].endswith("UTC")
    assert node_state.frozen_since("beta")["by"] == "unnamed", "a write that names no caller says so"
    # A RESTART: nothing in memory, the one file read again — by a fresh
    # connection and by a worker booting with nothing frozen in its globals
    assert node_state.frozen_collections() == {"alpha", "beta"}
    assert worker._is_frozen("alpha") is False
    frozen, unfrozen = worker._sync_frozen()
    assert frozen == {"alpha", "beta"} and unfrozen == set()
    assert worker._is_frozen("alpha") and worker._frozen_event("alpha").is_set()
    assert worker._catch_up_owed == set(), "a boot is no unfreeze"
    # it is NOT the pause, and the pause is not it
    assert node_state.paused_collections() == set()
    node_state.set_paused("gamma", True)
    assert "gamma" not in node_state.frozen_collections()
    # lifted, recorded too
    assert node_state.set_frozen("alpha", False, by="louise node unfreeze alpha") == {"beta"}
    assert node_state.frozen_since("alpha") is None
    hist = [(h["collection"], h["frozen"], h["by"]) for h in node_state.freeze_history()]
    assert hist == [("alpha", True, "louise node freeze alpha (from bash, user op)"), ("beta", True, "unnamed"),
                    ("alpha", False, "louise node unfreeze alpha")]
    assert all(isinstance(h["at"], float) for h in node_state.freeze_history())


def test_frozen_since_reads_creating_nothing(isolated_config):
    """For the Library card and the status display: a read that makes no file."""
    store = node_state.path()
    assert not store.exists()
    assert node_state.frozen_since("alpha") is None
    assert not store.exists(), "a check that created the store would change what it reports on"


def test_the_commands_freeze_and_unfreeze_a_held_collection_and_refuse_a_name_not_held(isolated_config, capsys):
    assert louise.main(["node", "freeze", "alpha"]) == 2, "a typo would freeze nothing and say it had"
    assert "no collection 'alpha' is held here" in capsys.readouterr().err
    assert node_state.frozen_collections() == set()
    toml = collections_mod.config_toml_path("alpha")
    toml.parent.mkdir(parents=True, exist_ok=True)
    toml.write_text('codex = "sha256:' + "0" * 64 + '"\n', encoding="utf-8")
    assert louise.main(["node", "freeze", "alpha"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["frozen"] == ["alpha"] and "keeps the version it holds" in out["says"]
    assert node_state.frozen_since("alpha")["by"].startswith("louise node freeze alpha")
    assert louise.main(["node", "unfreeze", "alpha"]) == 0
    assert json.loads(capsys.readouterr().out)["frozen"] == []
    assert node_state.freeze_history()[-1]["by"].startswith("louise node unfreeze alpha")


# ---- the fetch stage does nothing --------------------------------------------------------

def test_a_frozen_collection_fetches_nothing_and_the_unfreeze_fetches_again(collection, monkeypatch):
    con = db.connect(collection)
    started: list[str] = []
    monkeypatch.setitem(worker.ACQUIRE_REGISTRY, "gutenberg", types.SimpleNamespace(profiles={"demo": None}))
    monkeypatch.setattr(worker, "_acquire_worker", lambda c, spec, profile, reason: started.append("corpus"))
    monkeypatch.setattr(worker, "_archive_worker", lambda c, reason: started.append("archive"))
    monkeypatch.setitem(worker.bundle_genesis.ARCHIVES, "remote-x", types.SimpleNamespace(local=False))
    archived = dataclasses.replace(collection, genesis_archive="remote-x")
    monkeypatch.setattr(worker, "_archive_last_kick", {})

    freeze("alpha")
    worker._acquire(con, collection, "interval")
    worker._acquire_archive(con, archived, "interval", force=True)
    for t in list(worker._acquire_threads.values()) + list(worker._archive_threads.values()):
        t.join(timeout=10)
    assert started == [], "frozen: no corpus fetch and no archive shard"
    assert any("alpha is frozen" in m and "nothing is fetched" in m for m in events(con)), \
        "a deferral, said in the collection's log"
    assert con.execute("SELECT COUNT(*) FROM documents WHERE error IS NOT NULL").fetchone()[0] == 0

    freeze("alpha", False)
    worker._acquire(con, collection, "interval")
    worker._acquire_archive(con, archived, "interval", force=True)
    for t in list(worker._acquire_threads.values()) + list(worker._archive_threads.values()):
        t.join(timeout=10)
    assert sorted(started) == ["archive", "corpus"], "unfrozen, both fetches start again"


def test_a_fetch_in_flight_ends_at_its_unit_when_the_collection_is_frozen(collection, monkeypatch):
    """The fetch's stop is the one SIGTERM and the pause use (the `.part`
    kept, the offset committed); a freeze is one of its events."""
    seen: dict[str, bool] = {}

    def a_long_fetch(con, coll, spec, profile, stop=None):
        seen["before"] = stop.is_set()
        freeze("alpha")                       # the operator freezes it mid-fetch
        seen["after"] = stop.is_set()
        return {"fetched": ["half.part"]}

    monkeypatch.setattr(worker.acquire_engine, "run_acquisition", a_long_fetch)
    worker._acquire_worker(collection, None, "demo", "interval")
    assert seen == {"before": False, "after": True}
    assert "alpha" not in worker._paused_aborts, "a freeze is no pause: its unfreeze re-arms the fetch"


# ---- discovery re-opens nothing -----------------------------------------------------------

def test_a_changed_or_new_origin_file_is_not_read_while_frozen_and_is_caught_up_on_unfreeze(collection, monkeypatch):
    con = db.connect(collection)
    worker._scan_only(con, collection, "startup")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1")
    con.commit()
    # the corpus, mounted from elsewhere, changes under Louise with no fetch
    book = collection.corpus_dir / "10001" / "pg10001.txt.utf8"
    book.write_text(book.read_text(encoding="utf-8") + "\nA paragraph added upstream.\n", encoding="utf-8")
    new = collection.corpus_dir / "10004"
    new.mkdir()
    for f in (collection.corpus_dir / "10003").iterdir():
        shutil.copy(f, new / f.name.replace("10003", "10004"))
    scoped: list[str] = []
    real_scope = slice_scope.apply
    monkeypatch.setattr(slice_scope, "apply", lambda con, c, **k: scoped.append(c.name) or real_scope(con, c, **k))

    freeze("alpha")
    worker._scan_only(con, collection, "interval")
    rows = {r["uid"]: dict(r) for r in con.execute("SELECT uid, rdf_done, fts_done FROM documents")}
    assert len(rows) == 3, "no document added"
    assert all(r["rdf_done"] == 1 and r["fts_done"] == 1 for r in rows.values()), "no document re-opened"
    assert scoped == ["alpha"], "the slice scope still runs over the manifest as it is"
    assert any("its origin is not scanned" in m for m in events(con))
    # a fetch that finished under the freeze reads nothing into the version held either
    worker._acquire_done["alpha"] = True
    worker._finish_completed_acquires({"alpha": con}, [collection], {}, pipeline=False)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    assert con.execute("SELECT COUNT(*) FROM documents WHERE rdf_done=0").fetchone()[0] == 0

    # UNFREEZE: the next round asks a discovery WITH its fetch, a stale
    # plain-scan request in the queue notwithstanding
    worker._request_discovery("alpha", "background fetch finished", acquire=False)
    freeze("alpha", False)
    assert worker._catch_up_owed == {"alpha"}
    last: dict[str, float] = {}
    assert worker._catch_up_unfrozen([collection], last, pipeline=True) == ["alpha"]
    assert worker._take_discovery_request("alpha") == ("unfrozen", True) and "alpha" in last
    assert worker._catch_up_unfrozen([collection], last, pipeline=True) == [], "once"
    worker._scan_only(con, collection, "unfrozen")
    rows = {r["uid"]: dict(r) for r in con.execute("SELECT uid, rdf_done FROM documents")}
    assert len(rows) == 4, "the book that appeared is added now"
    reopened = [u for u, r in rows.items() if r["rdf_done"] == 0]
    assert sorted(reopened) == sorted(u for u in rows if u.endswith(":10001") or u.endswith(":10004")), reopened


def test_a_scan_in_flight_stops_at_its_next_batch_when_the_collection_is_frozen(collection, monkeypatch):
    con = db.connect(collection)
    asked: list[bool] = []

    def a_long_scan(c, coll, stop=None):
        asked.append(stop.is_set())
        freeze("alpha")                       # the operator freezes it mid-scan
        asked.append(stop.is_set())           # ...and the scan sees it at its next batch
        return {"added": 1, "interrupted": True}

    monkeypatch.setattr(discovery, "sync_collection", a_long_scan)
    worker._scan_only(con, collection, "interval")
    assert asked == [False, True]
    assert any("the collection is frozen" in m for m in events(con))
    assert worker._take_owed_scans() == set(), "owed to the unfreeze, not to the mode"


def test_a_freeze_never_stops_a_scan_of_another_collection(collection, monkeypatch):
    con = db.connect(collection)
    asked: list[bool] = []
    monkeypatch.setattr(discovery, "sync_collection",
                        lambda c, coll, stop=None: asked.append(stop.is_set()) or {})
    freeze("beta")
    worker._scan_only(con, collection, "interval")
    assert asked == [False]


# ---- local embedding still runs ------------------------------------------------------------

def test_a_frozen_collection_embeds_every_pending_document_itself_even_under_p2p(collection, monkeypatch):
    """"You can still embed locally": a frozen copy takes no bundle in, so a
    part it left to a leader would never get vectors here — the rendezvous
    partition is lifted (04 §4.5, amended 2026-11-07)."""
    from app.share import leading
    con = db.connect(collection)
    worker._scan_only(con, collection, "startup")
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, in_scope=1, embed_done=0")
    con.commit()
    monkeypatch.setattr(leading, "skip_test", lambda c, con, **k: (lambda row: True))   # all led elsewhere
    monkeypatch.setattr(leading, "widen", lambda c: None)
    monkeypatch.setattr(worker, "_lead_cursor", {})
    assert worker._backfill_rows(con, collection, 10, mode="p2p") == [], \
        "not frozen: every part is another peer's under p2p"
    freeze("alpha")
    rows = worker._backfill_rows(con, collection, 10, mode="p2p")
    assert len(rows) == 3, "frozen: every pending document is this node's to embed"
    assert len(worker._backfill_rows(con, collection, 10, mode="all")) == 3


# ---- pause and freeze together ---------------------------------------------------------------

def test_pause_and_freeze_compose_by_the_stricter(collection, monkeypatch):
    con = db.connect(collection)
    try:
        freeze("alpha")
        assert [c.name for c in worker._hold_locks([collection], set(), {"alpha": con})] == ["alpha"], \
            "frozen alone: the node still runs it"
        node_state.set_paused("alpha", True)
        paused = node_state.paused_collections()
        assert worker._hold_locks([collection], paused, {"alpha": con}) == [], "paused and frozen: nothing runs"
        # unfrozen while paused: owed, not asked — nothing runs it yet
        freeze("alpha", False)
        assert worker._catch_up_unfrozen([], {}, pipeline=True) == []
        assert worker._catch_up_owed == {"alpha"} and worker._take_discovery_request("alpha") is None
        # resumed: it runs, and catches up
        node_state.set_paused("alpha", False)
        active = worker._hold_locks([collection], node_state.paused_collections(), {"alpha": con})
        assert [c.name for c in active] == ["alpha"]
        assert worker._catch_up_unfrozen(active, {}, pipeline=True) == ["alpha"]
        assert worker._take_discovery_request("alpha") == ("unfrozen", True)
        # frozen again while paused, then resumed: still frozen — neither lifts the other
        node_state.set_paused("alpha", True)
        freeze("alpha")
        node_state.set_paused("alpha", False)
        assert worker._is_frozen("alpha") and node_state.frozen_collections() == {"alpha"}
        assert worker._catch_up_owed == set()
    finally:
        worker._release_locks()


def test_every_fetch_stop_carries_the_freeze():
    """The corpus fetch and the archive fetch both end at their unit on a
    freeze — the omission that let seed-only download for ten hours
    (2026-09-13) was a stop that did not carry the pause."""
    import inspect
    for fn in (worker._acquire_worker, worker._archive_worker):
        assert "_frozen_event(collection.name)" in inspect.getsource(fn), fn.__name__


def test_the_main_loop_reads_the_freeze_each_round_and_catches_up():
    import inspect
    source = inspect.getsource(worker.run_forever)
    assert source.count("_sync_frozen(node_con)") == 2, "at boot and every round"
    assert "_catch_up_unfrozen(active, last_discover, pipeline, cons=cons)" in source


def test_threads_wait_on_nothing_the_freeze_adds(collection):
    """A pipeline thread NEVER waits without a bound (CLAUDE.md): the gates
    are a set lookup and an event, and a frozen-set read that fails keeps
    the last set instead of raising or lifting it."""
    freeze("alpha")
    broken = types.SimpleNamespace(execute=lambda *a, **k: (_ for _ in ()).throw(Exception("locked")))
    frozen, unfrozen = worker._sync_frozen(broken)
    assert frozen == {"alpha"} and unfrozen == set() and worker._is_frozen("alpha")
    t = threading.Thread(target=lambda: worker._is_frozen("alpha"))
    t.start()
    t.join(timeout=1)
    assert not t.is_alive()
