"""The parse pool leaves when its process does (invariant 2: SIGTERM finishes
the unit, commits, exits — bounded in time).

München, 2026-10-25: wikipedia-v8's records build was stopped with SIGTERM,
committed its part and returned from main, and never exited. py-spy: the main
thread in `concurrent.futures.process._python_exit`, joining two pool manager
threads that waited for answers no child would give (none was left), their
queue feeders blocked writing to a pipe nobody read.

Two causes, reproduced on Python 3.12.3:
- gh-115634: a child retiring at `max_tasks_per_child` is not replaced while
  stale "idle" tokens last, so the pool runs out of children with parts
  queued (8 parts, 2 children retiring after 2 tasks: 4 answered, 0 children);
- a pool thrown away by the watchdog kept its children (a stuck one for ever),
  and the interpreter's exit joins every pool's manager thread with no bound.

Each scenario runs in a child interpreter: this one's, and the node's 3.12
when this machine has it (3.13.16 carries the upstream fix for the first)."""
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import CancelledError, Future
from pathlib import Path

import pytest

from app import config, parallel

ROOT = Path(__file__).resolve().parent.parent


def _interpreters() -> list[list[str]]:
    """This interpreter, and a 3.12 one if this one is not 3.12: the node runs
    3.12, whose pool lacks CPython's fix (`-S`: its site-packages are not this
    environment's, and the scenarios need the standard library alone)."""
    out = [[sys.executable]]
    other = shutil.which("python3.12")
    if sys.version_info[:2] != (3, 12) and other:
        out.append([other, "-S"])
    return out


INTERPRETERS = _interpreters()
IDS = [Path(i[0]).name for i in INTERPRETERS]


def _run(argv: list[str], tmp_path: Path, body: str, timeout: float, **env) -> tuple:
    script = tmp_path / "scenario.py"
    script.write_text(textwrap.dedent(body))
    t0 = time.monotonic()
    # its own process group: a child it leaves behind holds the pipes open,
    # so it counts as still running, and is killed with it
    proc = subprocess.Popen([*argv, str(script)], cwd=ROOT, text=True, start_new_session=True,
                            env=dict(os.environ, PYTHONPATH=str(ROOT), **env),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        pytest.fail(f"still running after {timeout:.0f} s, or a child left behind (the hang)\n"
                    f"stdout: {out}\nstderr: {err[-2000:]}")
    return proc.returncode, out, err, time.monotonic() - t0


def _gone(pid: int) -> bool:
    """Dead: no process, or a zombie (dead, not yet reaped)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state in ("Z", "X")


@pytest.mark.parametrize("argv", INTERPRETERS, ids=IDS)
def test_a_child_retired_at_its_task_limit_is_replaced(tmp_path, argv):
    """gh-115634, through the pool's own constructor: every part answered."""
    code, out, err, _ = _run(argv, tmp_path, """
        import os, time
        from concurrent.futures import wait
        from app import config, parallel
        config.PARSE_PROCESSES = 2
        config.PARSE_TASKS_PER_CHILD = 2
        if __name__ == "__main__":
            ex = parallel.pool()
            fs = [ex.submit(time.sleep, 0.05) for _ in range(8)]
            done, not_done = wait(fs, timeout=20)
            print(len(done), len(not_done), len(ex._processes), flush=True)
        """, timeout=60)
    answered, unanswered, children = map(int, out.split())
    assert (answered, unanswered) == (8, 0), f"a retired child was not replaced: {out} {err[-500:]}"


STREAM = """
    import multiprocessing, os, sys, time
    from app import config, parallel, process_exit

    def compute(kind, collection, rows):
        # in a CHILD, the part holding row {stuck} never returns: a parser
        # that hangs (in-process, the same part is quick)
        if multiprocessing.parent_process() is not None and any(r["n"] == {stuck} for r in rows):
            with open(os.environ["STUCK_PID"], "w") as f:
                f.write(str(os.getpid()))
            time.sleep(3600)
        return [(True, r["n"]) for r in rows]

    class Collection:
        name = "scenario"

        def plugin_instance(self):
            raise RuntimeError("no plugin: parts cut by count")

    def main():
        config.PARSE_PROCESSES = 2
        config.PARSE_TASKS_PER_CHILD = {per_child}
        config.PARSE_TIMEOUT_S = {timeout}
        parallel._compute = compute
        pages = ([{{"n": p * 8 + j}} for j in range(8)] for p in range(10_000))
        stream = parallel.compute_pages("chunks", Collection(), pages)
        got = []
        for out in stream:
            got.extend(v for _ok, v in out)
            if len(got) >= {take}:
                stream.close()             # the build's stop
                break
        print(" ".join(map(str, got)), flush=True)
        return 130

    if __name__ == "__main__":
        process_exit.run(main)
    """


@pytest.mark.parametrize("argv", INTERPRETERS, ids=IDS)
def test_a_stopped_build_leaves_at_once_after_the_watchdog_threw_a_stuck_pool_away(tmp_path, argv):
    """München's scenario: a part stuck in a child, the watchdog takes it
    in-process and the pages go on with a fresh pool; then the stop. The
    process exits 130, promptly, and the stuck child is gone. Before: the
    exit joined the old pool's manager, which waited for the stuck child."""
    pid_file = tmp_path / "stuck.pid"
    code, out, err, took = _run(argv, tmp_path, STREAM.format(stuck=3, per_child=500, timeout=2, take=32),
                                timeout=90, STUCK_PID=str(pid_file))
    assert code == 130, f"exit {code}\n{err[-2000:]}"
    assert [int(v) for v in out.split()] == list(range(32)), "every page, in order, the stuck part in-process"
    assert "[parse pool] TimeoutError" in err, "the watchdog took the stuck part"
    assert pid_file.exists() and _gone(int(pid_file.read_text())), "the stuck child was terminated"
    assert took < 45, f"left after {took:.0f} s"


@pytest.mark.parametrize("argv", INTERPRETERS, ids=IDS)
def test_a_long_stream_whose_children_retire_never_stalls_and_leaves(tmp_path, argv):
    """Children retiring after EVERY part, 40 pages, then the stop: no stall
    and a prompt exit. The race it guards is in the stop: a pool shut down
    before its children were terminated (3.14's order) lets the last child
    retire unreplaced, and 2 exits in 8 hung that way on 3.12.3."""
    code, out, err, took = _run(argv, tmp_path, STREAM.format(stuck=-1, per_child=1, timeout=10, take=320),
                                timeout=120, STUCK_PID=str(tmp_path / "unused"))
    assert code == 130, f"exit {code}\n{err[-2000:]}"
    assert [int(v) for v in out.split()] == list(range(320))
    assert "[parse pool]" not in err, f"the pool stalled: {err[-1000:]}"
    assert took < 90, f"left after {took:.0f} s"


# ---- in-process: what a thrown-away pool means to its other callers ---------------------------

class _Proc:
    def __init__(self, alive=True):
        self.alive, self.terminated = alive, False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True


class _Old:
    """A 3.12 executor, as `discard` sees it: no terminate_workers."""

    def __init__(self):
        self._processes = {1: _Proc(), 2: _Proc(alive=False)}
        self.shut = None

    def shutdown(self, wait=True, *, cancel_futures=False):
        self.shut = (wait, cancel_futures)


def test_discard_terminates_the_children_after_cancelling_the_queue():
    ex = _Old()
    parallel.discard(ex)
    assert ex.shut == (False, True), "queued work cancelled, no wait"
    assert ex._processes[1].terminated and not ex._processes[2].terminated, "the live child, only"
    assert ex in parallel._discarded


def test_discard_uses_the_library_s_own_call_where_it_exists():
    class New:
        called = False

        def terminate_workers(self):
            New.called = True

    ex = New()
    parallel.discard(ex)
    assert New.called and ex in parallel._discarded


def test_a_failure_from_a_pool_already_thrown_away_is_not_a_new_one(monkeypatch, capsys):
    """Another caller's failure discarded the shared pool; this caller's
    parts come back cancelled or broken. Computed in-process, not counted,
    and the pool that replaced the old one is left alone."""
    gone = _Old()
    parallel.discard(gone)
    current = _Old()
    monkeypatch.setattr(parallel, "_pool", current)
    parallel.reset_failures()
    parallel._note_failure(CancelledError(), gone)
    assert parallel.health()["failures"] == 0 and parallel._pool is current
    assert "from a pool already stopped" in capsys.readouterr().err
    parallel._note_failure(TimeoutError("no answer"), current)
    assert parallel.health()["failures"] == 1 and parallel._pool is None, "the current pool's own failure counts"
    assert current.shut == (False, True)
    parallel.reset_failures()


class _Cancelled:
    """A pool another caller stopped after this one took it: its parts come
    back cancelled."""

    def submit(self, fn, *args):
        f = Future()
        f.cancel()
        return f


class _ShutDown:
    """The same, a moment later: it takes no new part at all."""

    def submit(self, fn, *args):
        raise RuntimeError("cannot schedule new futures after shutdown")


@pytest.mark.parametrize("gone", [_Cancelled, _ShutDown], ids=["cancelled", "shut-down"])
def test_a_part_on_a_pool_stopped_meanwhile_is_computed_in_process(monkeypatch, gone):
    """Another caller's failure stopped the shared pool. CancelledError and
    the RuntimeError of a submit after shutdown used to escape `compute` and
    `compute_pages`, past their fallback, into the stage."""
    pool = gone()
    parallel._discarded.add(pool)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    monkeypatch.setattr(parallel, "pool", lambda: pool)
    monkeypatch.setattr(parallel, "_partition", lambda c, rows, want: [rows[:1], rows[1:]])
    monkeypatch.setattr(parallel, "_compute", lambda kind, c, rows: [(True, r["n"]) for r in rows])

    class C:
        name = "c"

    parallel.reset_failures()
    assert parallel.compute("chunks", C(), [{"n": 1}, {"n": 2}]) == [(True, 1), (True, 2)]
    assert list(parallel.compute_pages("chunks", C(), iter([[{"n": 3}, {"n": 4}]]))) == [[(True, 3), (True, 4)]]
    assert parallel.health()["failures"] == 0


def test_the_pool_is_stopped_before_the_library_joins_it_at_exit():
    """The interpreter's exit hooks run last-in, first-out: this module's is
    registered after concurrent.futures' own, so it runs first."""
    from concurrent.futures import process as cf

    def func(hook):
        # a lambda over the function (3.12, 3.13), or a partial
        cells = [c.cell_contents for c in (getattr(hook, "__closure__", None) or ())]
        return getattr(hook, "func", None) or next((c for c in cells if callable(c)), hook)
    hooks = [func(h) for h in threading._threading_atexits]
    assert hooks.index(parallel._stop_at_exit) > hooks.index(cf._python_exit)
