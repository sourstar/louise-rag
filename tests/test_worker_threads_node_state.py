"""The worker's own threads read the node state from state/node.sqlite —
driven, not assumed.

The review of 2026-09-13 reverted four lines — the stats beat's seeding
gate, its pause read, the `_log_shares` fix and the index maintainer's read
— and the WHOLE suite stayed green: every beat test called
`_control_state_for_beat` by hand, and nothing drove `_index_maintainer` at
all. Each revert silently pauses the seeder or the index maintainer on
every node, because the boot move deletes the manifest copies they would
read. These run the real threads, against a first collection whose
manifest holds no worker_state — the state after the move.
"""
import threading
import time

from app import collections as collections_mod, db, node_state, sched, worker
from app.acquire import seeding


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _halt(thread) -> None:
    worker._stop.set()
    thread.join(timeout=10)
    worker._stop.clear()


def test_the_beat_gates_seeding_and_sees_pauses_from_the_node_state(isolated_config,
                                                                   monkeypatch):
    monkeypatch.setattr(worker, "STATS_PUBLISH_INTERVAL_S", 0.05)
    gate: list[str] = []
    monkeypatch.setattr(seeding, "tick",
                        lambda collections, cons, control_con, state: gate.append(state))
    assert db.get_control(db.connect(), db.WORKER_STATE_KEY) is None, \
        "the first manifest holds no state, as after the boot move"
    node_state.set_worker_state("running")
    worker._main_loop_live.set()
    beat = threading.Thread(target=worker._stats_publisher, daemon=True)
    beat.start()
    try:
        assert _wait(lambda: len(gate) >= 3), gate
        assert set(gate) == {"running"}, \
            "the seeding gate reads the node, never a manifest (which reads 'paused')"
        assert not worker._paused.is_set(), "the beat never pauses a running node"

        node_state.set_worker_state("seed-only")
        assert _wait(worker._paused.is_set), "a pause reaches the beat within beats"
        assert _wait(lambda: gate[-1] == "seed-only"), gate[-3:]
    finally:
        _halt(beat)
        worker._paused.clear()
        worker._main_loop_live.clear()


def test_the_beat_logs_the_fair_shares(isolated_config, monkeypatch):
    """`_stats_publisher` passed an unbound `all_collections` to
    `_log_shares`: a NameError every beat, swallowed, so the fair-share
    lines — the first benchmark scheduling.md reads — never reached the log."""
    monkeypatch.setattr(worker, "STATS_PUBLISH_INTERVAL_S", 0.05)
    monkeypatch.setattr(seeding, "tick", lambda *a: None)
    name = collections_mod.load_all()[0].name
    s = sched.scheduler()
    s.sync(sched.RES_INDEX, {name: object()})
    s.declare(sched.RES_INDEX, name)
    turn = s.acquire(sched.RES_INDEX)
    turn.spent(3.0, units=1)
    turn.release(idle=False)

    beat = threading.Thread(target=worker._stats_publisher, daemon=True)
    beat.start()
    try:
        con = db.connect()
        assert _wait(lambda: con.execute(
            "SELECT 1 FROM events WHERE stage='sched' LIMIT 1").fetchone() is not None), \
            "no fair-share line reached the log"
    finally:
        _halt(beat)


def test_the_index_maintainer_works_while_running_and_idles_when_paused(isolated_config,
                                                                         monkeypatch):
    monkeypatch.setattr(worker, "INDEX_MAINTAINER_INTERVAL_S", 0.05)
    calls: list[str] = []
    monkeypatch.setattr(worker, "_maintain_vector_index",
                        lambda con, c: calls.append(c.name) or False)
    assert db.get_control(db.connect(), db.WORKER_STATE_KEY) is None
    name = collections_mod.load_all()[0].name
    node_state.set_worker_state("running")
    # the index has work: announced the way real work is (a declared flow
    # alone waits for the scheduler's stale-readmission interval)
    sched.scheduler().demand(sched.RES_INDEX, name)
    maintainer = threading.Thread(target=worker._index_maintainer, daemon=True)
    maintainer.start()
    try:
        assert _wait(lambda: len(calls) >= 1), \
            "a running node maintains its index (a manifest read would say 'paused')"
        node_state.set_worker_state("seed-only")
        time.sleep(0.3)             # a turn already in flight finishes
        seen = len(calls)
        sched.scheduler().demand(sched.RES_INDEX, name)     # more work, while paused
        time.sleep(0.5)
        assert len(calls) == seen, "a seed-only node leaves its index alone"
    finally:
        _halt(maintainer)
