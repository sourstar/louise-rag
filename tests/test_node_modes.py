"""tests/test_node_modes.py — what each of the node's four modes runs.

Operator, 2026-09-19: "I want to redefine the behavior."

    pause                   nothing runs — no fetching, parsing, seeding,
                            indexing
    seeding only            seeding only
    download/seeding only   download and seeding only
    run everything          everything; embedding is set per collection by its
                            own drop-down, and no mode changes that setting

ONE TABLE says it (app/node_state.py: MODE_RUNS) and every gate reads it: the
worker's main loop, its scans, its index maintainer, the seeder, `louise
health`, the Library. Before this, "download/seeding only" went on scanning
what it downloaded into the manifest and kept the vector-index maintainer
running — parsing and indexing under a button that named neither — and a scan
already running ignored a pause until it ended.
"""
import shutil
import threading
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, health, node_state, worker
from app.acquire import seeding

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
F, S, P, I, E = (node_state.FETCH, node_state.SEED, node_state.PARSE,
                 node_state.INDEX, node_state.EMBED)


@pytest.fixture(autouse=True)
def flags():
    """The worker's mode flags are process-wide: leave them as found."""
    yield
    worker._paused.clear()
    worker._acquire_only.clear()
    worker._take_owed_scans()


@pytest.fixture
def collection(isolated_config, tmp_path):
    corpus = tmp_path / "corpus_a"
    shutil.copytree(FIXTURE_CORPUS, corpus)
    return collections_mod.Collection(
        name="alpha", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


# ---- the table ---------------------------------------------------------------------

def test_each_mode_runs_exactly_what_its_button_says():
    assert node_state.MODE_RUNS == {
        "paused": frozenset(),
        "seed-only": frozenset({S}),
        "acquire-only": frozenset({F, S}),
        "running": frozenset({F, S, P, I, E}),
    }
    assert set(node_state.MODE_RUNS) == set(db.WORKER_STATES), "four modes, four rows"
    assert dict(node_state.MODE_LABELS) == {
        "running": "run everything", "acquire-only": "download/seeding only",
        "seed-only": "seeding only", "paused": "pause"}
    assert set(node_state.MODE_SAYS) == set(db.WORKER_STATES)


def test_embedding_is_not_a_modes_business():
    """It runs only where everything runs, and THERE the collection's own
    drop-down sets it. No mode names it, changes it, or is changed by it."""
    assert [m for m in db.WORKER_STATES if node_state.runs(m, E)] == ["running"]
    for says in node_state.MODE_SAYS.values():
        assert "embed" in says or "nothing runs" in says


def test_a_mode_nobody_knows_runs_nothing():
    for activity in (F, S, P, I, E):
        assert node_state.runs("warp-speed", activity) is False


def test_mode_changes_leave_each_collections_embed_setting_alone(collection):
    con = db.connect(collection)
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    for state in db.WORKER_STATES:
        node_state.set_worker_state(state)
        assert db.get_scope(con)["embed_mode"] == "all", state


# ---- every gate reads the table --------------------------------------------------------

def test_the_seeder_seeds_exactly_where_the_table_says():
    assert set(seeding.SEEDING_STATES) == {"running", "acquire-only", "seed-only"}
    assert "paused" not in seeding.SEEDING_STATES, "pause: no seeding"


@pytest.mark.parametrize("state,allowed", [("running", True), ("acquire-only", False),
                                           ("seed-only", False), ("paused", False)])
def test_index_maintenance_runs_only_where_indexing_does(isolated_config, state, allowed):
    """It ran in "download/seeding only" until 2026-09-19: twelve Lance threads
    are neither downloading nor seeding."""
    node_state.set_worker_state(state)
    assert worker._maintenance_allowed() is allowed


def test_health_holds_back_what_the_mode_does_not_run():
    everything = ("rdf", "fts", "embed", "archive")
    for mode in ("paused", "seed-only", "acquire-only"):
        assert health._held_by_mode(mode) == everything, mode
        assert "this runs when" in health._held_reason(mode, "rdf")
    assert health._held_by_mode("running") == ()
    assert health._held_by_mode("unknown") == (), "never invent a reason work is not running"


# ---- download/seeding only: the bytes move, nothing reads them ------------------------------

def test_download_only_fetches_and_does_not_scan(collection, monkeypatch):
    con = db.connect(collection)
    fetched, scanned = [], []
    monkeypatch.setattr(worker, "_acquire", lambda c, coll, reason: fetched.append("corpus"))
    monkeypatch.setattr(worker, "_acquire_archive",
                        lambda c, coll, reason, force=False: fetched.append("archive"))
    monkeypatch.setattr(discovery, "sync_collection",
                        lambda c, coll, stop=None: scanned.append(coll.name) or {})

    worker._acquire_only.set()
    worker._discover(con, collection, "interval")
    assert fetched == ["corpus", "archive"], "the download goes on"
    assert scanned == [], "a scan is parsing, and this mode parses nothing"
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0

    # it is OWED, not dropped: scanned when everything runs again
    assert worker._take_owed_scans() == {"alpha"}
    worker._acquire_only.clear()
    worker._discover(con, collection, "landed while the node parsed nothing")
    assert scanned == ["alpha"]


@pytest.mark.parametrize("flag", ["_paused", "_acquire_only"])
def test_a_plain_scan_is_owed_in_every_mode_that_parses_nothing(collection, monkeypatch, flag):
    con = db.connect(collection)
    getattr(worker, flag).set()
    worker._scan_only(con, collection, "background fetch finished")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert worker._take_owed_scans() == {"alpha"}
    assert worker._take_owed_scans() == set(), "taken once"


def test_a_running_scan_stops_when_the_mode_changes_and_the_rest_is_owed(collection,
                                                                         monkeypatch):
    """A pause used to wait for a running scan to end — 1,083 s on wikivec.
    The scan is resumable by construction, so the mode is one of its stops."""
    con = db.connect(collection)
    asked = []

    def a_long_scan(c, coll, stop=None):
        asked.append(stop.is_set())           # running: nothing holds it
        worker._acquire_only.set()            # the operator presses the button
        asked.append(stop.is_set())           # ...and the scan sees it at its next ref
        return {"added": 1}

    monkeypatch.setattr(discovery, "sync_collection", a_long_scan)
    worker._scan_only(con, collection, "interval")
    assert asked == [False, True]
    assert worker._take_owed_scans() == {"alpha"}
    said = [r["message"] for r in con.execute("SELECT message FROM events")]
    assert any("runs no parsing now" in m for m in said)


def test_sigterm_still_stops_a_scan_and_owes_nothing(collection, monkeypatch):
    con = db.connect(collection)
    stop = threading.Event()

    def scan(c, coll, stop=None):
        stop_outer.set()
        assert stop.is_set()
        return {}

    stop_outer = stop
    monkeypatch.setattr(discovery, "sync_collection", scan)
    worker._scan_only(con, collection, "interval", stop=stop)
    assert worker._take_owed_scans() == set(), "a stopping node owes the next boot's scan, not this"


def test_the_real_scan_still_runs_where_everything_runs(collection):
    con = db.connect(collection)
    worker._scan_only(con, collection, "startup")
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    assert worker._take_owed_scans() == set()


# ---- embedding stops with the mode, at the unit SIGTERM already has ----------------------------

def test_a_document_in_flight_stops_at_its_next_slice_when_the_mode_changes(collection,
                                                                            monkeypatch):
    """Seen live the evening this landed: in "download/seeding only" the
    producers took no new document and the two in flight went on — 470
    embedding requests in the next 100 s, and the largest book is 5.3 hours.
    "Pause: nothing is running" cannot wait for that. The slice is the unit
    (invariant 2): what is written stays, the document is deferred untouched."""
    from app import config, embeddings
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 2)
    recipe = collections_mod.EmbedRecipe(url="http://enc", api="openai", model="m",
                                         dtype="f32", ctx=512, dim=4)
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c=None: recipe)
    slices = []

    def embed(texts, **kw):
        slices.append(len(texts))
        if len(slices) == 2:
            worker._acquire_only.set()          # the operator presses the button
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(embeddings, "_embed_texts", embed)
    chunks = [{"id": f"g:1:{i}", "seq": i, "chapter": "", "text": f"chunk {i}"}
              for i in range(8)]
    assert worker._embed_stop.is_set() is False
    with pytest.raises(embeddings.EmbedInterrupted):
        embeddings.upsert_book(1, "en", chunks, collection, stop=worker._embed_stop)
    assert slices == [2, 2], "it went on embedding after the mode stopped running it"

    # ...and it resumes from what is written when everything runs again
    worker._acquire_only.clear()
    assert embeddings.upsert_book(1, "en", chunks, collection, stop=worker._embed_stop) == 8
    assert slices == [2, 2, 2, 2], "the four chunks already written were embedded again"


def test_both_embed_paths_hand_the_mode_aware_stop_to_the_write():
    import inspect
    for fn in (worker.stage_embed, worker._embed_parallel):
        source = inspect.getsource(fn)
        assert "stop=_embed_stop" in source, fn.__name__
        assert "stop=_stop" not in source, fn.__name__


@pytest.mark.parametrize("flag", ["_paused", "_acquire_only"])
def test_the_embed_stop_follows_every_mode_that_embeds_nothing(flag):
    assert worker._embed_stop.is_set() is False
    getattr(worker, flag).set()
    assert worker._embed_stop.is_set() is True


def test_an_archive_pass_in_flight_ends_at_its_next_row_when_the_mode_changes(collection,
                                                                              monkeypatch):
    """A pass asks its stop at every row and was only given SIGTERM, so one in
    flight when the node was paused ran to the end of its budget, up to 300 s."""
    from dataclasses import replace
    archived = replace(collection, genesis_archive="some-archive")
    seen = {}

    def drain(con, coll, *, stop=None, eager=True):
        seen["before"] = stop.is_set()
        worker._paused.set()                  # the operator presses pause
        seen["after"] = stop.is_set()
        return None

    monkeypatch.setattr(worker.bundle_genesis, "drain_next_shard", drain)
    monkeypatch.setattr(worker, "_acquire_archive", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_vector_writes_allowed", lambda c: True)
    monkeypatch.setattr(worker, "_license_allows", lambda *a: True)
    monkeypatch.setattr(worker, "_quarantine_allows", lambda *a: True)
    monkeypatch.setattr(worker, "_ingest_pending", lambda con: False)
    worker._drain_remote_archive(db.connect(collection), archived, threading.Event())
    assert seen == {"before": False, "after": True}
