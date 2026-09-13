"""Node-level control state lives in the NODE's file, not a collection's.

History (docs/notes/STATUS.md): the node's worker_state lived in the control
table of whichever collection sorted first. Wiping that collection's derived
data — a legitimate repair — emptied its control table and every collection
on the node stopped with no log line; removing it did the same; and with the
first-sorting collection refused at boot, the web wrote the operator's pause
to one manifest while the worker read another. docs/07 §7.8 puts runtime
overrides in `state/node.sqlite`: app/node_state.py is that file, and these
tests pin the move out of the manifests, the one-home rule, and the move
failing CLOSED when it cannot see every manifest.
"""
import json
import sqlite3

from fastapi.testclient import TestClient

from app import collections as collections_mod, config, db, node_state, worker


def _collection(tmp_path, name):
    base = tmp_path / name
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=base / "corpus", manifest_path=base / "manifest.db",
        fts_dir=base / "fts", lance_dir=base / "lancedb")


class _Locked:
    """A manifest that raises on every read — busy past its timeout."""

    def execute(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def _events(collection):
    return [r["message"] for r in db.connect(collection).execute("SELECT message FROM events")]


def test_the_store_is_the_nodes_file_beside_collections(isolated_config):
    """07 §7.8's layout, and the same file hold's admission ledger opens:
    `hold` commands take `<root>/collections` and keep node state beside it."""
    from app.hold import admission
    assert node_state.path() == config.louise_root() / "state" / "node.sqlite"
    assert admission.state_dir(config.louise_root() / "collections") \
        / "node.sqlite" == node_state.path()


def test_a_fresh_node_reads_paused_and_the_state_round_trips(isolated_config):
    assert node_state.worker_state() == "paused", "a fresh node starts paused"
    for state in db.WORKER_STATES:
        node_state.set_worker_state(state)
        assert node_state.worker_state() == state


def test_wiping_a_collection_no_longer_touches_the_node_state(isolated_config,
                                                              tmp_path):
    """The live incident, replayed: the first collection's control table
    emptied by a repair. It holds nothing of the node's any more."""
    con = db.connect(_collection(tmp_path, "aaa"))
    node_state.set_worker_state("running")
    con.execute("DELETE FROM control")
    assert node_state.worker_state() == "running"


def test_the_move_takes_what_the_web_wrote_and_leaves_one_home(isolated_config,
                                                               tmp_path):
    con_a = db.connect(_collection(tmp_path, "aaa"))   # where the web wrote
    con_b = db.connect(_collection(tmp_path, "bbb"))
    db.set_worker_state(con_a, "acquire-only")
    db.set_worker_state(con_b, "running")               # a stale sibling copy
    db.set_bandwidth_override(con_b, 1000.0, ttl_seconds=3600)

    node = node_state.connect()
    move = node_state.adopt_legacy(node, [con_a, con_b])

    assert move.adopted[db.WORKER_STATE_KEY] == "acquire-only", \
        "the operator's last click, not the sibling's stale value"
    assert move.blocked == ()
    assert node_state.worker_state(node) == "acquire-only"
    assert node_state.bandwidth_override(node) == 1000.0, \
        "a key only a sibling held still moves"
    for con in (con_a, con_b):
        for key in db.NODE_CONTROL_KEYS:
            assert db.get_control(con, key) is None, \
                "one home (07 §7.8): no manifest copy survives to be read back"


def test_a_value_already_moved_is_never_overwritten(isolated_config, tmp_path):
    """An explicit pause in the node file is never undone by a stale
    manifest copy — which is deleted all the same."""
    node_state.set_worker_state("paused")
    con_a = db.connect(_collection(tmp_path, "aaa"))
    db.set_worker_state(con_a, "running")

    node = node_state.connect()
    assert node_state.adopt_legacy(node, [con_a]).adopted == {}
    assert node_state.worker_state(node) == "paused"
    assert db.get_control(con_a, db.WORKER_STATE_KEY) is None


def test_per_collection_keys_never_move(isolated_config, tmp_path):
    """scope (embed_mode) and the gate verdict belong to their collection:
    moving them would spend another collection's GPU budget."""
    from app.verify import backend_gate
    con_a = db.connect(_collection(tmp_path, "aaa"))
    scope = db.get_scope(con_a)
    scope["embed_mode"] = "all"
    db.set_scope(con_a, scope)
    db.set_control(con_a, backend_gate.GATE_STATE_KEY,
                   json.dumps({"allowed": False, "reason": "x", "ts": 1}))

    node_state.adopt_legacy(node_state.connect(), [con_a])

    assert db.get_scope(con_a)["embed_mode"] == "all"
    assert backend_gate.gate_state(con_a) is not None
    assert db.BANDWIDTH_OVERRIDE_KEY in db.NODE_CONTROL_KEYS
    assert "scope" not in db.NODE_CONTROL_KEYS


def test_nothing_to_move_is_not_an_error_and_moving_twice_is_a_no_op(
        isolated_config, tmp_path):
    node = node_state.connect()
    con_a = db.connect(_collection(tmp_path, "aaa"))
    assert node_state.adopt_legacy(node, [con_a]).adopted == {}
    db.set_worker_state(con_a, "running")
    assert node_state.adopt_legacy(node, [con_a]).adopted == {db.WORKER_STATE_KEY: "running"}
    assert node_state.adopt_legacy(node, [con_a]).adopted == {}


# ---------------- failing closed ----------------

def test_an_unreadable_manifest_ahead_of_every_copy_blocks_the_move(isolated_config,
                                                                    tmp_path):
    """Reproduced by the review: the web's manifest (first) locked once at
    boot, a sibling's STALE 'running' adopted instead, and on the next boot
    the operator's 'paused' deleted — the node downloading against an
    explicit pause. Now nothing moves, nothing is deleted, and the node
    reads 'paused' until it can see every manifest."""
    con_b = db.connect(_collection(tmp_path, "bbb"))
    db.set_worker_state(con_b, "running")

    move = node_state.adopt_legacy(node_state.connect(), [_Locked(), con_b])

    assert move.adopted == {}
    assert set(move.blocked) == set(db.NODE_CONTROL_KEYS)
    assert node_state.worker_state() == "paused", "stopping is the safe direction"
    assert db.get_control(con_b, db.WORKER_STATE_KEY) == "running", \
        "nothing is deleted while the order is broken"


def test_an_unreadable_manifest_behind_the_value_does_not_block_it(isolated_config,
                                                                   tmp_path):
    con_a = db.connect(_collection(tmp_path, "aaa"))
    db.set_worker_state(con_a, "seed-only")

    move = node_state.adopt_legacy(node_state.connect(), [con_a, None])

    assert move.adopted == {db.WORKER_STATE_KEY: "seed-only"}
    assert move.blocked == (db.BANDWIDTH_OVERRIDE_KEY,), \
        "the override no readable manifest held stays unresolved"
    assert db.get_control(con_a, db.WORKER_STATE_KEY) == "seed-only", \
        "and no copy is deleted until a boot resolves every key"


def test_a_manifest_that_fails_to_open_once_does_not_cost_the_operators_value(
        isolated_config, tmp_path, monkeypatch):
    """The review's reproduction, through the worker's own boot step: boot
    one cannot open the first manifest; boot two can, and must move ITS
    value, not the sibling's."""
    a, b = _collection(tmp_path, "aaa"), _collection(tmp_path, "bbb")
    db.set_worker_state(db.connect(a), "paused")
    db.set_worker_state(db.connect(b), "running")
    real_connect, failed = db.connect, []

    def flaky(collection=None):
        if collection is not None and collection.name == "aaa" and not failed:
            failed.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real_connect(collection)

    monkeypatch.setattr(db, "connect", flaky)
    worker._move_node_control(node_state.connect(), [a, b])
    assert node_state.worker_state() == "paused"
    assert any("NOT moved" in m for m in _events(b)), _events(b)

    worker._move_node_control(node_state.connect(), [a, b])
    assert node_state.worker_state() == "paused"
    assert db.get_control(node_state.connect(), db.WORKER_STATE_KEY) == "paused", \
        "boot two moved the operator's value, not the sibling's 'running'"
    assert db.get_control(real_connect(b), db.WORKER_STATE_KEY) is None


# ---------------- the worker and the API ----------------

def test_the_worker_boot_moves_the_state_from_the_first_collection(
        isolated_config, tmp_path):
    """`_move_node_control` offers the manifests in the order the web wrote
    to — the unfiltered first collection first — and says what it did."""
    a, b = _collection(tmp_path, "aaa"), _collection(tmp_path, "bbb")
    db.set_worker_state(db.connect(a), "seed-only")
    db.set_worker_state(db.connect(b), "running")

    worker._move_node_control(node_state.connect(), [a, b])

    assert node_state.worker_state() == "seed-only"
    assert any("node control moved to state/node.sqlite" in m for m in _events(a)), \
        _events(a)


def test_a_node_with_no_state_says_it_is_paused(isolated_config, tmp_path):
    a = _collection(tmp_path, "aaa")
    worker._move_node_control(node_state.connect(), [a])
    assert any("PAUSED by default" in m for m in _events(a)), _events(a)


def test_a_reader_that_must_create_nothing_can_peek(isolated_config):
    """`louise health` is a check: it reads the mode and creates nothing."""
    assert node_state.peek_worker_state() == "paused", "absent reads as the worker reads it"
    assert not node_state.path().exists()
    node_state.set_worker_state("seed-only")
    assert node_state.peek_worker_state() == "seed-only"


def test_the_mode_survives_a_restart(isolated_config, tmp_path):
    """Operator decision, 2026-09-13 (docs/07 §7.8, invariant 10): the
    node's mode is the one runtime override a restart keeps. The boot move
    runs on every start, and must never reset it — whatever stale copy a
    manifest still holds."""
    a = _collection(tmp_path, "aaa")
    node_state.set_worker_state("acquire-only")
    db.set_worker_state(db.connect(a), "running")      # a stale manifest copy
    for _ in range(2):                                  # two restarts
        worker._move_node_control(node_state.connect(), [a])
        assert node_state.worker_state() == "acquire-only"


def _listing(d):
    return sorted(p.name for p in d.iterdir())


def test_peeking_a_closed_store_creates_not_even_its_sidecars(isolated_config):
    """Review, reproduced: `mode=ro` alone made `-wal` and `-shm` beside a
    closed WAL database — the state after every deploy and every click."""
    node_state.set_worker_state("acquire-only")          # closes: no sidecars
    before = _listing(node_state.path().parent)
    assert node_state.peek_worker_state() == "acquire-only"
    assert _listing(node_state.path().parent) == before


def test_a_store_only_the_admission_ledger_made_reads_paused(isolated_config):
    """hold's admission ledger opens the same file with only its own
    tables. The worker reads that as 'paused' (it creates `control` and
    finds no key); a peek that said 'unknown' held nothing, and health
    reported a node that runs nothing as running."""
    from app.hold import admission
    admission.connect(config.louise_root() / "collections").close()
    assert node_state.peek_worker_state() == "paused"


def test_a_root_with_uri_characters_is_read_not_cut_short(isolated_config, tmp_path,
                                                          monkeypatch):
    """A raw `file:{path}` URI cut at `#` or `?`, dropped mode=ro, and
    CREATED a stray database beside the real one."""
    for odd in ("a#b", "c?d", "e%20f"):
        home = tmp_path / "roots"
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("LOUISE_ROOT", str(home / odd))
        node_state.set_worker_state("seed-only")
        before = _listing(home)
        assert node_state.peek_worker_state() == "seed-only", odd
        assert _listing(home) == before, odd
        # and the other branch: a worker holding the store open, sidecars
        # present, so the peek reads through the live WAL with mode=ro
        held = node_state.connect()
        try:
            db.set_worker_state(held, "running")
            before = _listing(home)
            assert node_state.peek_worker_state() == "running", odd
            assert _listing(home) == before, odd
        finally:
            held.close()


def test_the_read_only_manifest_reader_is_not_cut_short_either(isolated_config, tmp_path):
    c = _collection(tmp_path / "x#y", "odd")
    db.set_worker_state(db.connect(c), "running")
    before = _listing(tmp_path)
    ro = db.read_only_connect(c)
    assert db.worker_state(ro) == "running"
    ro.close()
    # the raw URI was cut at '#', so its stray database landed at tmp/x —
    # beside the directory, not inside it (the verification pass caught an
    # assertion that listed the wrong one)
    assert not (tmp_path / "x").exists(), "no stray database where the URI was cut"
    assert _listing(tmp_path) == before


def test_the_api_writes_the_row_the_worker_reads(isolated_config, monkeypatch):
    """The control split, closed: whatever collection sorts first — or is
    refused — the API and the worker's beat read and write one row."""
    from app.api import app
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    with TestClient(app) as client:
        client.headers["X-Admin-Token"] = "sekrit"
        assert client.post("/api/control", json={"state": "seed-only"}).status_code == 200
        assert client.get("/api/status").json()["worker_state"] == "seed-only"
    try:
        worker._main_loop_live.set()
        assert worker._control_state_for_beat(node_state.connect()) == "seed-only"
    finally:
        worker._main_loop_live.clear()
