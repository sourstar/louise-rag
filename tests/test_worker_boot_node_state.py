"""run_forever reads the node state from state/node.sqlite — driven, not assumed.

The control resolution inside run_forever had no test at all (mapped
2026-09-13), and the seed-only incident's lesson is that a test calling a
helper by hand passes while the loop that should call it does not. This one
runs the real loop, briefly, on the fixture corpus: the state the web wrote
to the old place is moved at boot and obeyed, and a change written where
/api/control writes it reaches the loop within a round.
"""
import threading
import time

from app import config, db, node_state, worker


def _wait(predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _app_threads() -> set[threading.Thread]:
    """The live threads running Louise's own code — the worker's, not a
    library's (LanceDB's event loop outlives every test by design)."""
    out = set()
    for t in threading.enumerate():
        target = getattr(t, "_target", None)
        if t is not threading.current_thread() and \
                (getattr(target, "__module__", None) or "").startswith("app."):
            out.add(t)
    return out


def _stop_the_worker(loop: threading.Thread, before: set[threading.Thread]) -> list[str]:
    """Stop run_forever AND every thread it started before `_stop` is cleared.
    `_stop` is the process's one event: cleared while the sources thread was
    still between ticks, that thread ran on for the rest of the suite and,
    its test's isolated_config gone, its next tick created the REAL
    /data/state/node.sqlite — failing whichever test was running a minute
    later, most often a slow VeGa gRPC test (2026-10-08). Which threads
    outlived the stop."""
    worker._stop.set()
    loop.join(timeout=30)
    left = []
    for t in _app_threads() - before:
        t.join(timeout=30)
        if t.is_alive():
            left.append(t.name)
    if loop.is_alive():
        left.append("run_forever")
    return left


def test_the_main_loop_obeys_the_node_state(isolated_config, monkeypatch):
    monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 0)
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)   # not the main thread
    monkeypatch.setattr(worker.spaces, "refuse_boot", lambda collections: [])
    monkeypatch.setattr(worker, "_last_discover", {})
    # where the web wrote the operator's click before node.sqlite
    db.set_worker_state(db.connect(), "seed-only")

    before = _app_threads()
    loop = threading.Thread(target=worker.run_forever, daemon=True)
    loop.start()
    try:
        assert _wait(worker._paused.is_set), "boot moved seed-only and the loop obeyed it"
        assert node_state.worker_state() == "seed-only"
        assert db.get_control(db.connect(), db.WORKER_STATE_KEY) is None, \
            "one home: the manifest copy is gone"
        assert worker._main_loop_live.is_set()

        node_state.set_worker_state("acquire-only")     # where /api/control writes
        assert _wait(lambda: not worker._paused.is_set()
                     and worker._acquire_only.is_set()), \
            "a change written to the node file reaches the loop"
    finally:
        left = _stop_the_worker(loop, before)
        worker._stop.clear()
        worker._paused.clear()
        worker._acquire_only.clear()
    assert left == [], f"the loop and every thread it started exit on the stop: {left}"
    assert not worker._main_loop_live.is_set(), "and says so, so the beat stands down"


def test_a_collection_refused_at_boot_does_not_take_the_node_down(isolated_config,
                                                                  monkeypatch):
    """Invariant 1: a space mismatch refuses THAT collection, not the node.
    The refusal branch printed to `sys.stderr` in a module that never
    imported sys — a NameError at boot, and a crash loop under
    `restart: unless-stopped`. And the state the web wrote INTO the refused
    collection (it is `load_all()[0]`, where the web writes) still moves:
    the control split's own scenario."""
    monkeypatch.setattr(config, "WORKER_PIPELINE_THREADS", 0)
    monkeypatch.setattr(config, "WORKER_IDLE_SLEEP", 0.05)
    monkeypatch.setattr(worker.signal, "signal", lambda *a: None)
    monkeypatch.setattr(worker.spaces, "refuse_boot",
                        lambda collections: [(c, "space mismatch (test)") for c in collections])
    monkeypatch.setattr(worker.spaces, "validate_boot", lambda collections: None)
    monkeypatch.setattr(worker, "_last_discover", {})
    db.set_worker_state(db.connect(), "acquire-only")

    errors: list[BaseException] = []

    def _run():
        try:
            worker.run_forever()
        except BaseException as e:   # noqa: BLE001 — the assertion is that none escapes
            errors.append(e)

    before = _app_threads()
    loop = threading.Thread(target=_run, daemon=True)
    loop.start()
    try:
        assert _wait(worker._main_loop_live.is_set), f"the worker booted: {errors}"
        assert not errors
        assert node_state.worker_state() == "acquire-only", \
            "moved out of the refused collection the web wrote to"
    finally:
        left = _stop_the_worker(loop, before)
        worker._stop.clear()
        worker._paused.clear()
        worker._acquire_only.clear()
    assert not errors, errors
    assert left == [], f"the loop and every thread it started exit on the stop: {left}"


def test_a_signal_exits_130_as_07_says(monkeypatch):
    """docs/07 §7.7: "The node stops every collection at its unit and exits
    130 on a signal" — invariant 2 says the same. The worker exited 0."""
    monkeypatch.setattr(worker, "run_forever", lambda: worker._stop.set())
    try:
        assert worker.main() == 130
    finally:
        worker._stop.clear()
    monkeypatch.setattr(worker, "run_forever", lambda: None)
    assert worker.main() == 0


def test_the_mode_the_worker_acts_on_is_said_once_per_change(monkeypatch, capsys):
    """2026-10-23: seed-only was set at 12:25 and the worker's log had no
    line saying whether the worker read it."""
    monkeypatch.setattr(worker, "_mode_seen", [None])
    worker._say_mode("running")
    worker._say_mode("running")
    worker._say_mode("seed-only")
    lines = [ln for ln in capsys.readouterr().out.splitlines() if "node mode" in ln]
    assert len(lines) == 2
    assert "node mode read: runs embed, fetch, index, parse, seed" in lines[0]
    assert "node mode running -> seed-only: runs seed (read from " in lines[1]
    assert str(node_state.path()) in lines[1]


def test_every_mode_write_is_recorded_with_who_made_it(isolated_config):
    """2026-10-23: the node set seed-only at 12:25 acted as running by 12:34,
    and no record said whether anyone had written the mode meanwhile."""
    node_state.set_worker_state("seed-only", by="louise node pause --seed-only")
    node_state.set_worker_state("running", by="POST /api/control from 10.0.0.5 (Mozilla)")
    node_state.set_worker_state("paused")
    got = node_state.mode_history()
    assert [(h["state"], h["by"]) for h in got] == [
        ("seed-only", "louise node pause --seed-only"),
        ("running", "POST /api/control from 10.0.0.5 (Mozilla)"),
        ("paused", "unnamed")]
    assert node_state.worker_state() == "paused"
    for _ in range(node_state.MODE_HISTORY_KEEP + 5):
        node_state.set_worker_state("running", by="x")
    assert len(node_state.mode_history()) == node_state.MODE_HISTORY_KEEP


def test_node_show_lists_the_last_mode_writes(isolated_config):
    from app import node_show
    node_state.set_worker_state("seed-only", by="louise node pause --seed-only")
    got = node_show.overrides()["mode_writes"]
    assert got[-1]["state"] == "seed-only" and got[-1]["by"] == "louise node pause --seed-only"


def test_every_collection_pause_is_recorded_with_who_made_it(isolated_config):
    """2026-10-31: normattiva was found paused, and neither the operator nor
    the node session had paused it; nothing recorded who had."""
    from app import node_show
    node_state.set_paused("normattiva", True, by="louise node pause normattiva (from bash probe.sh)")
    node_state.set_paused("pubmed", True)
    node_state.set_paused("pubmed", False, by="louise node resume pubmed")
    got = node_state.pause_history()
    assert [(h["collection"], h["paused"], h["by"]) for h in got] == [
        ("normattiva", True, "louise node pause normattiva (from bash probe.sh)"),
        ("pubmed", True, "unnamed"), ("pubmed", False, "louise node resume pubmed")]
    shown = node_show.overrides()["paused_by"]
    assert set(shown) == {"normattiva"} and "probe.sh" in shown["normattiva"]["by"]
    assert "user" in node_state.invoker() or node_state.invoker() == ""
