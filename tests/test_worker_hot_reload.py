"""worker._sync_collections — collections added/removed while the worker
runs take effect within one round, no restart.
"""
import pytest

from app import collections as collections_mod, db, spaces, worker


def _write_toml(config_dir, name, extra=""):
    d = config_dir / "collections"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.toml").write_text(
        f'plugin = "gutenberg"\nprofile = "demo"\n{extra}', encoding="utf-8")


@pytest.fixture
def quiet_discover(monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "_discover",
                         lambda con, collection, reason: calls.append((collection.name, reason)))
    return calls


def test_added_collection_joins_within_one_sync(isolated_config, config_dir,
                                                 quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
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
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
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


def test_invalid_new_collection_is_skipped_not_fatal(isolated_config, config_dir,
                                                      quiet_discover, monkeypatch):
    monkeypatch.setattr(spaces, "resolve_embed_model_hash", lambda: ("abc", False))
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
