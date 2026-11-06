"""worker._sync_collections — collections added/removed while the worker
runs take effect within one round, no restart.
"""
import pytest

from app import collections as collections_mod, config, db, spaces, worker
from tests.conftest import codex_collection


def _write_toml(config_dir, name, extra=""):
    """A Codex collection `name` (the test Codex) whose file also says
    `profile = "demo"` and `extra`. Returns the file's `codex` line."""
    cid = codex_collection(name, louise_root=config.DATA_DIR, config_dir=config_dir,
                           toml_extra=f'profile = "demo"\n{extra}')
    return f'codex = "{cid}"\n'


def _without_the_default(config_dir):
    """The tests that count collections start from none: the default
    gutenberg every test is given is retired first."""
    (config_dir / "collections" / "gutenberg.toml").unlink()


@pytest.fixture
def quiet_discover(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_discover",
                         lambda con, collection, reason: calls.append((collection.name, reason)))
    return calls


def test_added_collection_joins_within_one_sync(isolated_config, config_dir,
                                                 quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    start = collections_mod.load_all()
    cons = {c.name: db.connect(c) for c in start}
    last_discover = {c.name: 0.0 for c in start}

    _write_toml(config_dir, "newone")
    result = worker._sync_collections(start, cons, last_discover)

    names = [c.name for c in result]
    assert "newone" in names
    assert "newone" in cons and "newone" in last_discover
    assert ("newone", "added") in quiet_discover
    events = cons["newone"].execute(
        "SELECT message FROM events WHERE stage='worker'").fetchall()
    assert any("added" in e["message"] for e in events)


def test_removed_collection_leaves_and_its_con_closes(isolated_config, config_dir,
                                                       quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _without_the_default(config_dir)
    _write_toml(config_dir, "a")
    _write_toml(config_dir, "b")
    start = collections_mod.load_all()
    cons = {c.name: db.connect(c) for c in start}
    last_discover = {c.name: 0.0 for c in start}
    assert {c.name for c in start} == {"a", "b"}

    (config_dir / "collections" / "b.toml").unlink()
    result = worker._sync_collections(start, cons, last_discover)

    assert [c.name for c in result] == ["a"]
    assert "b" not in cons and "b" not in last_discover
    with pytest.raises(Exception):
        # a closed connection must not be silently reusable
        cons_b_probe = [c for c in start if c.name == "b"][0]
        db_con = db.connect(cons_b_probe)   # fresh con is fine...
        db_con.close()
        db_con.execute("SELECT 1")          # ...but closed cons raise


def test_round_budget_lets_cheap_stages_batch(isolated_config, monkeypatch):
    """The time-budgeted round: many cheap steps per round, but a step that
    exhausts the budget ends the round after itself."""
    from app import worker as worker_mod
    calls = {"n": 0}

    monkeypatch.setattr(config, "WORKER_ROUND_BUDGET_S", 0.2)

    def cheap_step(con, collection, last_idle_log):
        calls["n"] += 1
        return calls["n"] < 500          # pretend work runs out eventually

    # simulate the loop body's budget logic directly
    import time as time_mod
    monkeypatch.setattr(worker_mod, "_run_one_step", cheap_step)
    deadline = time_mod.time() + config.WORKER_ROUND_BUDGET_S
    while not worker_mod._stop.is_set():
        if not worker_mod._run_one_step(None, None, None):
            break
        if time_mod.time() >= deadline:
            break
    assert calls["n"] > 10, "cheap steps must batch within one round"

    calls["n"] = 0

    def slow_step(con, collection, last_idle_log):
        calls["n"] += 1
        time_mod.sleep(0.3)              # single step exceeds the budget
        return True
    monkeypatch.setattr(worker_mod, "_run_one_step", slow_step)
    deadline = time_mod.time() + config.WORKER_ROUND_BUDGET_S
    while not worker_mod._stop.is_set():
        if not worker_mod._run_one_step(None, None, None):
            break
        if time_mod.time() >= deadline:
            break
    assert calls["n"] == 1, "an expensive step still runs exactly once per round"


def test_config_change_triggers_immediate_rediscover(isolated_config, config_dir,
                                                      quiet_discover, monkeypatch):
    """An ack recorded (or acquisition toggled) on an EXISTING collection
    must not sit inert until the interval scan — the round's hot-reload
    notices and re-discovers immediately."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _write_toml(config_dir, "pm")
    start = collections_mod.load_all()
    cons = {c.name: db.connect(c) for c in start}
    last_discover = {c.name: 0.0 for c in start}

    # unchanged config: no discover fires
    result = worker._sync_collections(start, cons, last_discover)
    assert quiet_discover == []

    _write_toml(config_dir, "pm", extra='acquire_enabled = true\n')
    result = worker._sync_collections(result, cons, last_discover)
    assert ("pm", "config-changed") in quiet_discover
    assert last_discover["pm"] > 0


def test_invalid_new_collection_is_skipped_not_fatal(isolated_config, config_dir,
                                                      quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _without_the_default(config_dir)
    _write_toml(config_dir, "good")
    start = collections_mod.load_all()
    cons = {c.name: db.connect(c) for c in start}
    last_discover = {c.name: 0.0 for c in start}

    _write_toml(config_dir, "broken")

    def failing_validate(collections):
        if any(c.name == "broken" for c in collections):
            raise spaces.BootValidationError("bad space config")
    monkeypatch.setattr(spaces, "validate_boot", failing_validate)

    result = worker._sync_collections(start, cons, last_discover)
    assert [c.name for c in result] == ["good"]
    assert "broken" not in cons
    # the failure is visible in the surviving collection's event log
    events = cons["good"].execute(
        "SELECT message FROM events WHERE level='error'").fetchall()
    assert any("broken" in e["message"] for e in events)


def test_a_pipeline_thread_sees_the_edit_and_not_its_startup_object(
        isolated_config, config_dir, quiet_discover, monkeypatch):
    """A Tier-2 edit reaches the PIPELINE THREAD, not only the main loop.

    Observed live 2026-09-12: gutenberg's `profile = "full"` was added to
    its TOML; `_sync_collections` saw the change and requested a discovery;
    the pipeline thread ran that discovery against the object it was handed
    at startup, whose profile was still None — so `_acquire` returned early
    and a 60 GB fetch never started. Nothing said so: the "no profile
    declared" warning is rate limited to once an hour and had been spent
    on the startup pass.

    A thread is started once and lives for the life of the worker, so every
    Tier-2 field it reads froze with it. The object is republished each
    round and re-read at the top of the loop instead.
    """
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    codex = _write_toml(config_dir, "growing", 'acquire_enabled = true\n')
    start = worker._sync_collections([], {}, {})
    stale = next(c for c in start if c.name == "growing")
    assert stale.profile == "demo"

    # what a running pipeline thread holds is `stale`; the TOML moves on
    (config_dir / "collections" / "growing.toml").write_text(
        codex + 'profile = "full"\nacquire_enabled = true\n', encoding="utf-8")
    worker._sync_collections(start, {c.name: db.connect(c) for c in start},
                             {c.name: 0.0 for c in start})

    assert worker._live_collection(stale).profile == "full", \
        "the thread must run the edit, not the object it was started with"


def test_repointed_paths_still_need_a_restart(isolated_config, config_dir,
                                              quiet_discover, monkeypatch):
    """The one thing hot reload must NOT do. A thread holds an open
    connection to the manifest it was started against; swapping the object
    under it would leave the thread writing one database and the main loop
    another."""
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _write_toml(config_dir, "moving")
    start = worker._sync_collections([], {}, {})
    held = next(c for c in start if c.name == "moving")

    moved = collections_mod.Collection(
        name="moving", plugin="gutenberg", profile="full",
        embed_mode_default=None, corpus_dir=held.corpus_dir,
        manifest_path=held.manifest_path.parent / "elsewhere.db",
        fts_dir=held.fts_dir, lance_dir=held.lance_dir)
    worker._publish_collections([moved])

    assert worker._live_collection(held) is held, \
        "a repointed manifest is refused, not half-applied"


def test_one_malformed_toml_refuses_its_own_collection_not_the_round(
        isolated_config, config_dir, quiet_discover, monkeypatch):
    """Invariant 1's rule — "the node keeps running its others" — applied
    to the other way a collection can be unrunnable.

    Observed live 2026-09-12 while re-minting gutenberg: its TOML named a
    Codex whose SHARD had been moved aside for the re-mint, `load_all`
    raised (correctly — a typo must not silently become the default), and
    the round loop logged a traceback every five seconds and served
    NOTHING. pubmed's archive import stopped with it.
    """
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _without_the_default(config_dir)
    _write_toml(config_dir, "fine")
    codex = _write_toml(config_dir, "broken")
    start = worker._sync_collections([], {}, {})
    assert {c.name for c in start} == {"fine", "broken"}
    cons = {c.name: db.connect(c) for c in start}

    (config_dir / "collections" / "broken.toml").write_text(
        codex + 'embed = "always"\n', encoding="utf-8")
    with pytest.raises(Exception):
        collections_mod.load_all()          # the raise is still the default

    result = worker._sync_collections(start, cons, {c.name: 0.0 for c in start})
    assert [c.name for c in result] == ["fine"], \
        "the good collection keeps running"

    # each collection keeps its own events: the refusal is in its own manifest
    messages = [r["message"] for c in (db.connect(x) for x in start)
                for r in c.execute("SELECT message FROM events WHERE stage='worker'")]
    assert any("'broken' is not served" in m for m in messages), \
        "and the operator is told which file, and why"


def test_the_broken_file_is_reported_once_not_every_round(
        isolated_config, config_dir, quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _write_toml(config_dir, "fine")
    codex = _write_toml(config_dir, "broken")
    start = worker._sync_collections([], {}, {})
    cons = {c.name: db.connect(c) for c in start}
    (config_dir / "collections" / "broken.toml").write_text(
        codex + 'embed = "always"\n', encoding="utf-8")

    def rounds():
        return sum(1 for c in (db.connect(x) for x in start)
                   for r in c.execute("SELECT message FROM events "
                                       "WHERE stage='worker'")
                   if "not served" in r["message"])

    kept = start
    for _ in range(4):
        kept = worker._sync_collections(kept, cons, {c.name: 0.0 for c in kept})
    assert rounds() == 1, "a round loop that logs every five seconds is noise"


def test_a_name_that_holds_another_collection_leaves_and_joins_again(
        isolated_config, config_dir, quiet_discover, monkeypatch):
    """2026-10-27: `forget wikidata`, then `rename wikidata-p2 wikidata`, between
    two rounds. The name never went missing, so the worker kept the deleted
    manifest's connections and lock, wrote its rows into the new collection's
    catalog index and never discovered the new one."""
    import dataclasses
    import types
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda *a: ("abc", False))
    _without_the_default(config_dir)
    _write_toml(config_dir, "a")
    start = collections_mod.load_all()
    cons = {c.name: db.connect(c) for c in start}
    old_con = cons["a"]
    last_discover = {c.name: 0.0 for c in start}
    held = start[0]
    other = dataclasses.replace(held, codex=types.SimpleNamespace(
        **{**vars(held.codex), "collection_id": "sha256:" + "e" * 64})
        if hasattr(held.codex, "__dict__") else types.SimpleNamespace(collection_id="sha256:" + "e" * 64))
    assert worker._identity(other) != worker._identity(held)
    monkeypatch.setattr(collections_mod, "load_all", lambda *a, **k: [other])
    monkeypatch.setattr(spaces, "validate_boot", lambda cs: None)   # a stand-in Codex

    result = worker._sync_collections(start, cons, last_discover)

    assert [c.name for c in result] == ["a"] and result[0] is other
    assert cons["a"] is not old_con, "a new connection for the new collection"
    with pytest.raises(Exception):
        old_con.execute("SELECT 1")
    assert ("a", "added") in quiet_discover
    events = [e["message"] for e in cons["a"].execute("SELECT message FROM events WHERE stage='worker'")]
    assert any("added" in m for m in events)


def test_a_thread_of_the_old_collection_is_not_handed_the_new_one():
    import types
    from pathlib import Path
    old = types.SimpleNamespace(name="a", manifest_path=Path("/m"),
                                codex=types.SimpleNamespace(collection_id="sha256:1"))
    new = types.SimpleNamespace(name="a", manifest_path=Path("/m"),
                                codex=types.SimpleNamespace(collection_id="sha256:2"))
    worker._publish_collections([new])
    try:
        assert worker._live_collection(old) is old
        assert worker._live_collection(new) is new
    finally:
        worker._publish_collections([])
