"""Node-level control state follows the NODE, not sort order.

Observed live: the node's worker_state lives in the control table of
whichever collection resolves first (alphabetically). Wiping that
collection's derived data — a legitimate repair — emptied its control
table, worker_state fell back to its default 'paused', and every
collection on the node stopped with no log line and nothing obviously
wrong. Removing that collection does the same thing.
"""
import json

import pytest

from app import collections as collections_mod, db


def _collection(tmp_path, name):
    base = tmp_path / name
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=base / "corpus", manifest_path=base / "manifest.db",
        fts_dir=base / "fts", lance_dir=base / "lancedb")


def test_wiped_control_collection_adopts_the_nodes_state(isolated_config, tmp_path):
    first = _collection(tmp_path, "aaa")     # sorts first: owns node control
    second = _collection(tmp_path, "bbb")
    con_a, con_b = db.connect(first), db.connect(second)

    # the node was running, and the state happens to live in bbb (it used to
    # be the control collection, or aaa was just wiped)
    db.set_worker_state(con_b, "running")
    db.set_bandwidth_override(con_b, 10_000_000, ttl_seconds=3600)
    assert db.worker_state(con_a) == "paused", "fresh manifest defaults paused"

    adopted = db.adopt_node_control(con_a, [con_b])

    assert set(adopted) == {"worker_state", db.BANDWIDTH_OVERRIDE_KEY}
    assert db.worker_state(con_a) == "running", \
        "a wiped control collection must not silently pause the node"
    assert db.get_bandwidth_override(con_a) == 10_000_000


def test_an_explicit_pause_is_never_overridden(isolated_config, tmp_path):
    """The control collection's own value always wins — otherwise a stale
    sibling would un-pause a node the operator deliberately paused."""
    first, second = _collection(tmp_path, "aaa"), _collection(tmp_path, "bbb")
    con_a, con_b = db.connect(first), db.connect(second)
    db.set_worker_state(con_a, "paused")
    db.set_worker_state(con_b, "running")

    adopted = db.adopt_node_control(con_a, [con_b])
    assert adopted == {}
    assert db.worker_state(con_a) == "paused"


def test_per_collection_keys_are_never_adopted(isolated_config, tmp_path):
    """scope (embed_mode) and the gate verdict belong to their collection:
    adopting them would spend another collection's GPU budget."""
    from app.verify import backend_gate
    first, second = _collection(tmp_path, "aaa"), _collection(tmp_path, "bbb")
    con_a, con_b = db.connect(first), db.connect(second)
    scope = db.get_scope(con_b)
    scope["embed_mode"] = "all"
    db.set_scope(con_b, scope)
    db.set_control(con_b, backend_gate.GATE_STATE_KEY,
                   json.dumps({"allowed": False, "reason": "x", "ts": 1}))

    db.adopt_node_control(con_a, [con_b])

    assert db.get_scope(con_a)["embed_mode"] == "lazy", "embed_mode is per-collection"
    assert backend_gate.gate_state(con_a) is None, "gate verdict is per-collection"
    assert db.BANDWIDTH_OVERRIDE_KEY in db.NODE_CONTROL_KEYS
    assert "scope" not in db.NODE_CONTROL_KEYS


def test_nothing_to_adopt_is_not_an_error(isolated_config, tmp_path):
    first, second = _collection(tmp_path, "aaa"), _collection(tmp_path, "bbb")
    assert db.adopt_node_control(db.connect(first), [db.connect(second)]) == {}
