"""tests/hold/test_write_lock.py — the one writer per collection (07 §7.7):
`state/lock`, taken by the node for every collection it runs and by a
`hold` command that writes, or exit 3 naming the holder — 2 for the four
whose later retry is not the answer; `node pause <collection>` makes the
worker release it; a crash leaves no stale lock."""
import io
import json
import os
import threading

import pytest

from app import collections as collections_mod, louise, node_state, worker
from app.hold import deposit, lock as lock_mod
from tests.deposit.test_deposit_identity import ENCODER_URL, make, node  # noqa: F401


# ---- the lock itself --------------------------------------------------------

def test_a_second_taker_is_refused_and_told_who_holds_it(tmp_path):
    state = tmp_path / "c" / "state"
    held = lock_mod.take(state, "c", "hold records build")
    assert held.held and lock_mod.lock_path(state).exists()
    with pytest.raises(lock_mod.Held) as e:
        lock_mod.take(state, "c", "hold vectors build")
    assert e.value.exit_code == 3 and e.value.holder["what"] == "hold records build"
    assert e.value.holder["pid"] == os.getpid() and "retry when it is done" in str(e.value)
    assert lock_mod.holder(state)["what"] == "hold records build"
    held.release()
    assert not held.held and lock_mod.holder(state) is None
    with lock_mod.take(state, "c", "hold vectors build") as again:
        assert again.held
    assert lock_mod.holder(state) is None


def test_the_four_exit_2_and_say_what_releases_the_node(tmp_path):
    state = tmp_path / "c" / "state"
    with lock_mod.take(state, "c", "the node (worker)"):
        with pytest.raises(lock_mod.Held) as e:
            lock_mod.take(state, "c", "hold collection forget", exit_code=2)
    assert e.value.exit_code == 2 and "node pause <collection>" in str(e.value)


def test_a_holder_that_dies_leaves_no_stale_lock(tmp_path):
    """The kernel drops a flock with its holder's descriptor: a crashed
    command cannot leave a collection locked, only its last words."""
    state = tmp_path / "c" / "state"
    crashed = lock_mod.take(state, "c", "hold records build")
    crashed._f.close()                      # the process died mid-write
    with lock_mod.take(state, "c", "hold vectors build") as taken:
        assert taken.held


# ---- the node holds the lock of every collection it runs ----------------------

def test_the_worker_takes_the_lock_of_a_collection_it_runs_and_lets_go_when_paused(node, monkeypatch):  # noqa: F811
    make(node, "alice")
    c = collections_mod.by_name("alice")
    monkeypatch.setattr(worker, "_locks", {})
    monkeypatch.setattr(worker, "_lock_nagged", {})
    state = lock_mod.state_dir(node, "alice")
    active = worker._hold_locks([c], set())
    assert [x.name for x in active] == ["alice"]
    assert lock_mod.holder(state)["what"] == "the node (worker)"
    with pytest.raises(lock_mod.Held):
        lock_mod.take(state, "alice", "hold records build")
    # paused by the operator: released after the unit, not run this round
    retired: list[str] = []
    monkeypatch.setattr(worker, "retire_collection",
                        lambda name, timeout=60.0: retired.append(name))
    assert worker._hold_locks([c], {"alice"}) == [] and retired == ["alice"]
    assert lock_mod.holder(state) is None
    # resumed: taken again
    assert [x.name for x in worker._hold_locks([c], set())] == ["alice"]
    worker._release_locks()
    assert lock_mod.holder(state) is None


def test_a_collection_another_writer_holds_is_not_run_this_round(node, monkeypatch):  # noqa: F811
    make(node, "alice")
    c = collections_mod.by_name("alice")
    monkeypatch.setattr(worker, "_locks", {})
    monkeypatch.setattr(worker, "_lock_nagged", {})
    with lock_mod.take(lock_mod.state_dir(node, "alice"), "alice", "hold records build"):
        assert worker._hold_locks([c], set()) == []
    assert [x.name for x in worker._hold_locks([c], set())] == ["alice"]
    worker._release_locks()


def test_retire_collection_releases_the_lock_too(node, monkeypatch):  # noqa: F811
    make(node, "alice")
    c = collections_mod.by_name("alice")
    monkeypatch.setattr(worker, "_locks", {})
    worker._hold_locks([c], set())
    worker.retire_collection("alice", timeout=1)
    assert lock_mod.holder(lock_mod.state_dir(node, "alice")) is None


# ---- node pause <collection> ----------------------------------------------------

def test_node_pause_and_resume_one_collection_persist_in_the_node_state(node):  # noqa: F811
    assert node_state.paused_collections() == set()
    assert louise.main(["node", "pause", "alice"]) == 0
    assert node_state.paused_collections() == {"alice"}
    assert louise.main(["node", "pause", "bob"]) == 0
    assert node_state.paused_collections() == {"alice", "bob"}
    assert louise.main(["node", "resume", "alice"]) == 0
    assert node_state.paused_collections() == {"bob"}


def test_node_pause_without_a_collection_is_the_mode(node):  # noqa: F811
    assert louise.main(["node", "pause"]) == 0
    assert node_state.worker_state() == "paused"
    assert louise.main(["node", "pause", "--seed-only"]) == 0
    assert node_state.worker_state() == "seed-only"
    assert louise.main(["node", "resume"]) == 0
    assert node_state.worker_state() == "running"


# ---- the commands -------------------------------------------------------------------

def test_a_writing_command_exits_3_naming_the_holder_and_forget_exits_2(node, monkeypatch, capsys):  # noqa: F811
    make(node, "alice")
    state = lock_mod.state_dir(node, "alice")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with lock_mod.take(state, "alice", "the node (worker)"):
        assert louise.main(["hold", "records", "build", "alice", "--root", str(node)]) == 3
        assert "held by the node (worker)" in capsys.readouterr().err
        assert louise.main(["hold", "collection", "forget", "alice", "--root", str(node),
                            "--here", "--yes"]) == 2
        assert "node pause <collection>" in capsys.readouterr().err
        assert deposit.held_file(node, "alice").exists(), "nothing was removed"
    # the node released it: forget runs, and releases its own lock at the end
    assert louise.main(["hold", "collection", "forget", "alice", "--root", str(node),
                        "--here", "--yes"]) == 0
    assert not (node / "alice").exists()


def test_a_command_releases_its_lock_when_it_ends(node, monkeypatch):  # noqa: F811
    make(node, "alice")
    state = lock_mod.state_dir(node, "alice")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    # dry run takes nothing; a real --here forget takes it and `main` releases
    louise.main(["hold", "collection", "forget", "alice", "--root", str(node), "--dry-run"])
    assert lock_mod.holder(state) is None and louise._cli_locks == []
    assert louise._cli_locks == []


def test_show_takes_no_lock(node, capsys):  # noqa: F811
    make(node, "alice")
    with lock_mod.take(lock_mod.state_dir(node, "alice"), "alice", "the node (worker)"):
        assert louise.main(["hold", "collection", "show", "alice", "--root", str(node)]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["collection_id"].startswith("sha256:")
