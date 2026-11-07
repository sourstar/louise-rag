"""tests/test_scheduled_checks.py — the plan in the background (03 §3.2;
app/scheduled_checks.py): due when a snapshot has no scheduled check or one
cadence has passed since; run as a child process, its report kept; a stop
ends the child and records nothing; a timed-out run is recorded as such;
nothing runs in a mode that does not parse; one collection per turn. And
(2026-11-07, the check → OOM kill → restart → check loop): a run the worker
died under is recorded as interrupted from its started marker, and after
CHECK_MAX_INTERRUPTS waits a cadence; a stop is no interruption; a check is
admitted only when the worker's cgroup has room for its last peak; a child
that writes more than a pipe buffer finishes."""
import inspect
import os
import signal
import sys
import time
from types import SimpleNamespace

import pytest

from app import scheduled_checks as sc

NOW = 1_800_000_000.0
MIB = 2 ** 20
GIB = 2 ** 30


@pytest.fixture(autouse=True)
def _no_cgroup_here(tmp_path, monkeypatch):
    """The host running the suite has its own cgroup: unread, so a turn is
    admitted wherever the tests run; the admission tests write their own."""
    monkeypatch.setattr(sc, "CGROUP_DIR", str(tmp_path / "no-cgroup"))
    monkeypatch.setattr(sc, "_deferred_said", {})


def test_due_by_snapshot_and_by_cadence(tmp_path):
    codex = SimpleNamespace(update=SimpleNamespace(cadence="weekly"))
    (tmp_path / "c").mkdir()                         # a record never creates the collection's folder
    assert not sc.due(tmp_path, "c", codex, None, now=NOW), "no snapshot: nothing to check"
    assert sc.due(tmp_path, "c", codex, "2026-W40", now=NOW), "never checked"
    sc.record(tmp_path, "c", "2026-W40", {"exit": 0, "outcome": "pass", "secs": 1.0}, now=NOW)
    assert not sc.due(tmp_path, "c", codex, "2026-W40", now=NOW + 86400), "checked this week"
    assert sc.due(tmp_path, "c", codex, "2026-W41", now=NOW + 86400), "a new snapshot"
    assert sc.due(tmp_path, "c", codex, "2026-W40", now=NOW + 8 * 86400), "a cadence later"
    monthly = SimpleNamespace(update=SimpleNamespace(cadence="monthly"))
    assert not sc.due(tmp_path, "c", monthly, "2026-W40", now=NOW + 8 * 86400)
    # a snapshot is its hash: one withdrawn unshared and made again under the same id is new
    sc.record(tmp_path, "c", "2026-09", {"outcome": "pass"}, now=NOW, snapshot_hash="sha256:aa")
    assert not sc.due(tmp_path, "c", monthly, "2026-09", now=NOW + 60, snapshot_hash="sha256:aa")
    assert sc.due(tmp_path, "c", monthly, "2026-09", now=NOW + 60, snapshot_hash="sha256:bb")
    # a record from before the hash was kept compares by id, as it did
    sc.record(tmp_path, "c", "2026-09", {"outcome": "pass"}, now=NOW)
    assert not sc.due(tmp_path, "c", monthly, "2026-09", now=NOW + 60, snapshot_hash="sha256:bb")
    assert sc.last(tmp_path, "c")["outcome"] == "pass"


def test_the_origin_goes_with_the_check_when_there_is_one(tmp_path):
    corpus = tmp_path / "origin"
    corpus.mkdir()
    argv = sc.argv_for(SimpleNamespace(name="gutenberg", corpus_dir=corpus, source="origin"))
    assert argv[1:6] == ["-m", "app.louise", "hold", "check", "gutenberg"] and argv[-2:] == ["--origin", str(corpus)]
    fetched = sc.argv_for(SimpleNamespace(name="g", corpus_dir=corpus, source="shard"))
    assert "--origin" not in fetched, "a fetched copy has no origin here: its check says so"


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_run_is_a_child_its_report_kept_and_a_stop_ends_it():
    c = SimpleNamespace(name="c")
    got = sc.run_one(c, stop=lambda: False, argv=_py('print(\'{"outcomes": []}\')'))
    assert got["exit"] == 0 and got["outcome"] == "pass" and got["report"] == {"outcomes": []}
    got = sc.run_one(c, stop=lambda: False, argv=_py("import sys; print('boom', file=sys.stderr); sys.exit(1)"))
    assert got["outcome"] == "fail" and got["stderr_tail"] == ["boom"] and got["report"] is None
    t0 = time.time()
    flag = {"stop": False}
    import threading
    threading.Timer(1.5, lambda: flag.update(stop=True)).start()
    got = sc.run_one(c, stop=lambda: flag["stop"], argv=_py("import time; time.sleep(60)"))
    assert got["outcome"] == "stopped" and time.time() - t0 < 30, "ended at the stop, not at the end"
    got = sc.run_one(c, stop=lambda: False, timeout_s=1.0, argv=_py("import time; time.sleep(60)"))
    assert got["outcome"] == "timed out"


def test_a_turn_runs_one_due_collection_only_in_a_mode_that_parses(isolated_config, monkeypatch):
    from app import collections as collections_mod, node_state
    from app.share import seeder
    monkeypatch.setattr(seeder, "current_snapshot", lambda d: SimpleNamespace(snapshot_id="2026-09"))
    ran, lines = [], []

    def fake(c, stop):
        ran.append(c.name)
        return {"exit": 0, "outcome": "pass", "secs": 2.0, "report": None}
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "paused")
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW, run=fake) is None and ran == []
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "running")
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW, run=fake) == "gutenberg"
    root = collections_mod.louise_root() / "collections"
    assert sc.last(root, "gutenberg")["snapshot_id"] == "2026-09" and ran == ["gutenberg"]
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 60, run=fake) is None, "done until due again"
    assert any("scheduled check pass" in m for m in lines)
    # a stopped run is not recorded: it runs again
    sc.state_path(root, "gutenberg").unlink()
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW,
                   run=lambda c, stop: {"outcome": "stopped", "secs": 1.0}) == "gutenberg"
    assert sc.last(root, "gutenberg") is None


def test_the_worker_starts_the_runner_and_show_reports_the_last():
    from app import louise, worker
    assert "scheduled_checks.loop" in inspect.getsource(worker)
    assert "scheduled_check" in inspect.getsource(louise._show_shard)


def test_a_collection_renamed_while_it_is_checked_is_recorded_under_its_new_name(isolated_config, monkeypatch):
    """München, 2026-10-20: a 24-minute scheduled check started under
    `gutenberg-qi`; the re-pin renamed it to `gutenberg` meanwhile, and the
    check's end re-created `collections/gutenberg-qi/state/` BY NAME. The
    report, the attestations and the run statements now follow the
    collection's identity; a collection held under no name gets nothing,
    and no folder is re-created."""
    import shutil
    import pytest
    from app import collections as collections_mod, node_state
    from app.hold import check as chk, collection as col, runs
    from app.hold.codex import CodexError
    from app.share import seeder
    monkeypatch.setattr(seeder, "current_snapshot", lambda d: SimpleNamespace(snapshot_id="2026-09"))
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "running")
    root = collections_mod.louise_root() / "collections"
    cid = col.read(root, "gutenberg").collection_id
    assert col.where_now(root, "gutenberg", cid) == "gutenberg"

    def renamed_meanwhile(c, stop):
        shutil.move(str(root / "gutenberg"), str(root / "gutenberg-renamed"))
        return {"exit": 0, "outcome": "pass", "secs": 1437.0, "report": None}
    lines: list[str] = []
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW, run=renamed_meanwhile) == "gutenberg"
    assert not (root / "gutenberg").exists(), "the old name is not re-created"
    assert sc.last(root, "gutenberg-renamed")["snapshot_id"] == "2026-09"
    assert any("started as gutenberg, renamed meanwhile" in m for m in lines)
    assert col.where_now(root, "gutenberg", cid) == "gutenberg-renamed"
    # the writers a check's child uses refuse a folder that is gone
    assert chk.write_attestations(root, "gutenberg", SimpleNamespace(outcomes=()), None, None, None,
                                  platform_image="x", now="t") == {}
    assert not (root / "gutenberg").exists()
    with pytest.raises(CodexError, match="no such collection here"):
        runs.keep(root, "gutenberg", {"body": {"run": "sha256:" + "a" * 64, "role": "document"}})
    assert not (root / "gutenberg").exists()
    with pytest.raises(FileNotFoundError):
        sc.record(root, "gutenberg", "2026-09", {"outcome": "pass"}, now=NOW)
    # held under no name: not recorded
    shutil.rmtree(root / "gutenberg-renamed")
    assert col.where_now(root, "gutenberg", cid) is None


def test_the_check_runs_with_a_quarter_of_the_parse_pool(monkeypatch):
    """2026-11-07: wikidata's check at the whole quota (12 children, 5.3 GB)
    beside the parse pool passed the worker's 24 GiB cap twice in 7 minutes."""
    from app import parallel
    monkeypatch.setattr(parallel, "processes", lambda: 11)
    assert sc.check_processes() == 2 and sc.child_env()["PARSE_PROCESSES"] == "2"
    monkeypatch.setattr(parallel, "processes", lambda: 2)
    assert sc.check_processes() == 1, "never none"
    got = sc.run_one(SimpleNamespace(name="c"), stop=lambda: False,
                     argv=_py("import os, json; print(json.dumps({'p': os.environ['PARSE_PROCESSES']}))"))
    assert got["report"] == {"p": "1"}, "the child is told its pool"


def test_a_stop_ends_the_check_s_own_children_too(tmp_path):
    """A pool child outlives its parent, waiting on a queue, and keeps its
    memory: a stop and a timeout end the check's whole session."""
    import os
    import pytest
    if os.name != "posix":
        pytest.skip("process groups are POSIX")
    pidfile = tmp_path / "grandchild.pid"
    code = ("import subprocess, sys, time; "
            f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
            f"open({str(pidfile)!r}, 'w').write(str(p.pid)); time.sleep(120)")
    got = sc.run_one(SimpleNamespace(name="c"), stop=lambda: False, timeout_s=2.0, argv=_py(code))
    assert got["outcome"] == "timed out"
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.1)
    else:
        raise AssertionError("the check's own child outlived the stop")


# ---- 2026-11-07: check → OOM kill → restart → the same check from zero ------------

def posix_only(fn):
    """A worker's death is a fork killed (POSIX). The fork is of a process
    with threads; its child runs one turn and is killed, so the warning is
    not this test's subject."""
    fn = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")(fn)
    return pytest.mark.skipif(not hasattr(os, "fork"), reason="a worker's death is a fork killed (POSIX)")(fn)


def _until(cond, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _script(tmp_path, name: str, code: str) -> list[str]:
    p = tmp_path / f"{name}.py"
    p.write_text(code, encoding="utf-8")
    return [sys.executable, str(p)]


def _kill_group_of(pidfile) -> None:
    """The check leads its own session, so its worker's death leaves it."""
    try:
        os.killpg(int(pidfile.read_text()), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def _killed_mid_check(root, name: str, *, run=None, ready=lambda m: True) -> None:
    """A worker that runs a turn and dies under its check as the kernel's
    OOM kill ends it — SIGKILL: no handler, no `finally`. A forked process,
    killed once the check's started marker satisfies `ready`."""
    pid = os.fork()
    if pid == 0:                                       # the worker
        try:
            sc.turn(stop=lambda: False, log=lambda m: None, now=NOW, run=run)
        finally:
            os._exit(0)
    try:
        assert _until(lambda: (m := sc.started(root, name)) is not None and ready(m)), "the check never started"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def _running_with_a_snapshot(monkeypatch):
    from app import collections as collections_mod, node_state
    from app.share import seeder
    monkeypatch.setattr(seeder, "current_snapshot",
                        lambda d: SimpleNamespace(snapshot_id="2026-09", hash="sha256:aa"))
    monkeypatch.setattr(node_state, "peek_worker_state", lambda: "running")
    return collections_mod.louise_root() / "collections"


def _passes(ran):
    def fake(c, stop):
        ran.append(c.name)
        return {"exit": 0, "outcome": "pass", "secs": 1.0, "report": None}
    return fake


@posix_only
def test_a_run_the_worker_died_under_is_interrupted_counted_and_bounded(isolated_config, monkeypatch, tmp_path):
    """München, 2026-11-07: the kernel killed the worker at 06:17 with
    wikidata's check running; `turn` had recorded nothing, as for a stop, so
    the restarted worker began the same check from zero at 06:18:01 — check,
    kill, restart, check. The marker the dead worker leaves is now an
    interruption, counted per snapshot with the peak the check had reached;
    the third start waits a cadence (or a new snapshot)."""
    from app.hold import collection as col
    root = _running_with_a_snapshot(monkeypatch)
    pidfile = tmp_path / "check.pid"
    # the first start: the real runner, its check at ~160 MiB when the worker dies
    argv = _script(tmp_path, "check", f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
                                      f"x = b'x' * (160 * {MIB})\ntime.sleep(120)\n")
    monkeypatch.setattr(sc, "argv_for", lambda c: argv)
    try:
        _killed_mid_check(root, "gutenberg", ready=lambda m: m.get("peak_rss", 0) >= 150 * MIB)
    finally:
        _kill_group_of(pidfile)
    m = sc.started(root, "gutenberg")
    assert m["snapshot_id"] == "2026-09" and m["snapshot_hash"] == "sha256:aa" and m["pid"] != os.getpid()
    assert sc.last(root, "gutenberg") is None, "the dead worker recorded nothing"
    # the second start: the interruption recorded, the check retried — and the worker dies again
    _killed_mid_check(root, "gutenberg", run=lambda c, stop: time.sleep(120) or {},
                      ready=lambda now: now["run"] != m["run"])
    first = sc.last(root, "gutenberg")
    assert first["outcome"] == "interrupted" and first["interrupts"] == 1
    assert first["peak_rss"] >= 150 * MIB, "the peak the killed check had reached is kept"
    # the third start is refused until the cadence
    ran, lines = [], []
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 60, run=_passes(ran)) is None and ran == []
    got = sc.last(root, "gutenberg")
    assert got["interrupts"] == 2 and got["peak_rss"] == first["peak_rss"] and sc.started(root, "gutenberg") is None
    assert any(m.startswith("gutenberg: ") and "never ended" in m and "interrupted 2 time(s)" in m
               and "not retried before" in m for m in lines), lines
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 120, run=_passes(ran)) is None and ran == []
    from app import louise
    codex = col.read(root, "gutenberg")
    shown = louise._scheduled_check(root, "gutenberg", codex)       # what `hold collection show` displays
    assert shown["outcome"] == "interrupted" and shown["interrupts"] == 2
    assert shown["retry_at"] == NOW + sc.period_of(codex)
    assert "interrupted 2 times" in shown["note"] and "retried at " in shown["note"]
    # a new snapshot is due at once; the same one a cadence after the last start
    assert sc.due(root, "gutenberg", codex, "2026-10", now=NOW + 120, snapshot_hash="sha256:bb")
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + sc.period_of(codex),
                   run=_passes(ran)) == "gutenberg"
    assert ran == ["gutenberg"] and sc.last(root, "gutenberg")["outcome"] == "pass"


def test_a_marker_is_counted_once_and_per_cadence(tmp_path):
    """A worker that died between the record and the marker's removal ran its
    check to the end: no interruption. A count starts again a cadence on
    (systemd's StartLimitIntervalSec), and with another snapshot."""
    month = sc.PERIOD_S["monthly"]
    (tmp_path / "c").mkdir()
    sc.mark_started(tmp_path, "c", "2026-09", now=NOW)
    sc.record(tmp_path, "c", "2026-09", {"outcome": "pass"}, now=NOW)
    assert sc.settle_interrupted(tmp_path, "c", period=month) is None
    assert sc.started(tmp_path, "c") is None and sc.last(tmp_path, "c")["outcome"] == "pass"
    for k, t in enumerate((NOW + 10, NOW + 20), start=1):
        sc.mark_started(tmp_path, "c", "2026-09", now=t)
        assert sc.settle_interrupted(tmp_path, "c", period=month)["interrupts"] == k
    assert sc.held_back(sc.last(tmp_path, "c"), now=NOW + 30, period=month)
    sc.mark_started(tmp_path, "c", "2026-09", now=NOW + 20 + month)
    assert sc.settle_interrupted(tmp_path, "c", period=month)["interrupts"] == 1, "a cadence on: counted afresh"
    sc.mark_started(tmp_path, "c", "2026-10", now=NOW + 30 + month)
    assert sc.settle_interrupted(tmp_path, "c", period=month)["interrupts"] == 1, "another snapshot"
    assert sc.settle_interrupted(tmp_path, "c", period=month) is None, "no marker: nothing"
    with pytest.raises(FileNotFoundError):
        sc.mark_started(tmp_path, "gone", "2026-09", now=NOW)
    assert not (tmp_path / "gone").exists(), "a marker never re-creates a collection by name"


@posix_only
def test_a_stop_is_not_an_interruption(isolated_config, monkeypatch, tmp_path):
    """`node stop` and SIGTERM set the worker's stop. The run's marker goes
    the moment the check sees it, BEFORE its child is ended: a worker that
    exits during the child's grace (or is killed then) leaves no
    interruption, and the check is due again as at any start. The worker
    waits for the check's thread on its way out."""
    root = _running_with_a_snapshot(monkeypatch)
    # in hand: the stop ends the child, nothing is recorded, no marker is left
    monkeypatch.setattr(sc, "argv_for", lambda c: _py("import time; time.sleep(60)"))
    t0 = time.time()
    assert sc.turn(stop=lambda: time.time() - t0 > 1.5, log=lambda m: None, now=NOW) == "gutenberg"
    assert time.time() - t0 < 30 and sc.last(root, "gutenberg") is None and sc.started(root, "gutenberg") is None
    # the worker gone while its check, deaf to SIGTERM, is in its grace
    pidfile, flag = tmp_path / "check.pid", tmp_path / "stop"
    argv = _script(tmp_path, "deaf", "import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                     f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
    monkeypatch.setattr(sc, "argv_for", lambda c: argv)
    pid = os.fork()
    if pid == 0:                                       # the worker
        try:
            sc.turn(stop=flag.exists, log=lambda m: None, now=NOW)
        finally:
            os._exit(0)
    try:
        assert _until(lambda: sc.started(root, "gutenberg") is not None and pidfile.exists())
        flag.touch()                                   # the stop
        assert _until(lambda: sc.started(root, "gutenberg") is None, 10.0), "the stop removes the marker at once"
        assert os.waitpid(pid, os.WNOHANG) == (0, 0), "the check is still in its grace"
    finally:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        _kill_group_of(pidfile)
    ran, lines = [], []
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 60, run=_passes(ran)) == "gutenberg"
    assert not any("never ended" in m for m in lines) and sc.last(root, "gutenberg")["outcome"] == "pass"
    from app import worker
    assert "check_thread.join(timeout=CHECK_STOP_WAIT_S)" in inspect.getsource(worker)
    assert worker.CHECK_STOP_WAIT_S > sc.STOP_GRACE_S


def _cgroup(d, *, limit, current, inactive_file=0) -> str:
    d.mkdir(exist_ok=True)
    (d / "memory.max").write_text(f"{limit}\n")
    (d / "memory.current").write_text(f"{current}\n")
    (d / "memory.stat").write_text(f"anon {current - inactive_file}\nfile {inactive_file}\n"
                                   f"active_file 0\ninactive_file {inactive_file}\n")
    return str(d)


def test_a_check_is_admitted_only_when_the_worker_has_room_for_its_peak(tmp_path):
    """wikidata's check peaked at ~6.4 GB beside a worker at ~17 GB in a
    24 GiB cgroup. (limit − in use) ≥ the last peak × 1.2, or ≥ 4 GiB when
    no peak is known; the page cache the kernel reclaims first is room."""
    cg = tmp_path / "cg"
    (tmp_path / "c").mkdir()
    sc.record(tmp_path, "c", "2026-09", {"outcome": "pass", "peak_rss": 6 * GIB}, now=NOW)
    ok, why = sc.admission(tmp_path, "c", cgroup=_cgroup(cg, limit=24 * GIB, current=17 * GIB))
    assert not ok and "7.0 GiB free" in why and "needs 7.2 GiB" in why and "last peak, 6.0 GiB" in why, why
    assert sc.admission(tmp_path, "c", cgroup=_cgroup(cg, limit=24 * GIB, current=16 * GIB))[0]
    assert sc.admission(tmp_path, "c", cgroup=_cgroup(cg, limit=24 * GIB, current=23 * GIB,
                                                      inactive_file=7 * GIB))[0], "inactive page cache is room"
    (tmp_path / "d").mkdir()                          # never measured: the default
    ok, why = sc.admission(tmp_path, "d", cgroup=_cgroup(cg, limit=24 * GIB, current=21 * GIB))
    assert not ok and "the default" in why and sc.ADMIT_DEFAULT_BYTES == 4 * GIB
    assert sc.admission(tmp_path, "d", cgroup=_cgroup(cg, limit=24 * GIB, current=20 * GIB))[0]


def test_a_check_with_no_room_is_deferred_and_said_every_ten_minutes(isolated_config, monkeypatch, tmp_path):
    """Deferral is not failure: nothing recorded, nothing started, one line
    at most every 10 min naming the collection and the numbers."""
    root = _running_with_a_snapshot(monkeypatch)
    sc.record(root, "gutenberg", "2026-08", {"outcome": "pass", "peak_rss": 6 * GIB}, now=NOW - 86400)
    full = _cgroup(tmp_path / "cg", limit=24 * GIB, current=17 * GIB)
    ran, lines = [], []
    for t in (NOW, NOW + 60, NOW + 601):
        assert sc.turn(stop=lambda: False, log=lines.append, now=t, run=_passes(ran), cgroup=full) is None
    assert ran == [] and sc.started(root, "gutenberg") is None
    assert sc.last(root, "gutenberg")["snapshot_id"] == "2026-08", "nothing recorded"
    said = [m for m in lines if "deferred" in m]
    assert len(said) == 2 and all(m.startswith("gutenberg: ") and "7.0 GiB free" in m for m in said), lines
    roomy = _cgroup(tmp_path / "cg", limit=24 * GIB, current=16 * GIB)
    assert sc.turn(stop=lambda: False, log=lines.append, now=NOW + 700, run=_passes(ran), cgroup=roomy) == "gutenberg"
    assert ran == ["gutenberg"]


def test_an_unreadable_cgroup_admits(tmp_path):
    (tmp_path / "c").mkdir()
    none = str(tmp_path / "none")
    assert sc.cgroup_memory(none) is None and sc.admission(tmp_path, "c", cgroup=none)[0]
    cg = tmp_path / "cg"
    cg.mkdir()
    (cg / "memory.current").write_text(f"{30 * GIB}\n")
    for limit in ("max", "garbage"):
        (cg / "memory.max").write_text(f"{limit}\n")
        assert sc.cgroup_memory(str(cg)) is None and sc.admission(tmp_path, "c", cgroup=str(cg))[0], limit
    (cg / "memory.max").write_text(f"{24 * GIB}\n")
    (cg / "memory.current").unlink()
    assert sc.admission(tmp_path, "c", cgroup=str(cg))[0]
    (cg / "memory.current").write_text(f"{10 * GIB}\n")
    assert sc.cgroup_memory(str(cg)) == (24 * GIB, 10 * GIB), "no memory.stat: memory.current alone"


def test_a_check_that_writes_more_than_a_pipe_buffer_finishes(tmp_path):
    """Read only at the end, a pipe holds 64 KiB and the child blocked on its
    write for ever. To files: the report (1 MiB here) read whole, the stderr
    (1 MiB) kept whole in the state directory and its last lines recorded."""
    argv = _script(tmp_path, "loud", "import json, sys\nsys.stderr.write(('x' * 1023 + '\\n') * 1024)\n"
                                     "print('last line', file=sys.stderr)\n"
                                     "print(json.dumps({'outcomes': ['y' * 1024] * 1024}))\n")
    c = SimpleNamespace(name="c")
    got = sc.run_one(c, stop=lambda: False, timeout_s=20.0, argv=argv)
    assert got["outcome"] == "pass" and got["stderr_tail"][-1] == "last line"
    assert len(got["report"]["outcomes"]) == 1024
    state = tmp_path / "state"
    state.mkdir()
    got = sc.run_one(c, stop=lambda: False, timeout_s=20.0, argv=argv, log_dir=state)
    assert got["outcome"] == "pass" and len(got["report"]["outcomes"]) == 1024
    assert (state / sc.STDERR_FILE).stat().st_size > MIB
    assert [p.name for p in state.iterdir()] == [sc.STDERR_FILE], "the report's file has no name"


@pytest.mark.skipif(os.name != "posix", reason="sessions are POSIX")
def test_the_peak_is_the_check_s_whole_session(tmp_path):
    """The check and its pool's children, each at ~96 MiB: their sum."""
    child = _script(tmp_path, "pool", f"import time\nx = b'x' * (96 * {MIB})\ntime.sleep(3)\n")
    argv = _script(tmp_path, "check", f"import subprocess, time\ng = subprocess.Popen({child!r})\n"
                                      f"x = b'x' * (96 * {MIB})\ntime.sleep(3)\ng.wait()\n")
    heard: list[int] = []
    got = sc.run_one(SimpleNamespace(name="c"), stop=lambda: False, argv=argv, on_peak=heard.append)
    assert got["outcome"] == "pass" and got["peak_rss"] >= 192 * MIB, got.get("peak_rss")
    assert heard and heard == sorted(heard) and heard[-1] <= got["peak_rss"]
    assert sc.session_rss(os.getpid() + 10 ** 7) is None, "no such session"
