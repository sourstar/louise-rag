"""A process rsync forked must not hold a fetch thread after rsync exits.

Observed live on the node 2026-09-14 (worker under docker-init): a
daemon-protocol rsync client (host::module) forks a receiver that inherits
stdout and stderr. When the parent exited first — exit 35 "timeout waiting
for daemon connection", exit 10, or our kill — the receiver was re-parented
to init and lived on for 10+ minutes holding the pipes. The parent sat in
state Z for 700+ s, never reaped, because communicate() waits for EOF
before it reaps; the fetch thread sat in poll(), its transfer slot held;
and the stop and deadline paths killed the already-dead parent and then
communicate()d with no timeout — unbounded, so a SIGTERM could hang the
thread indefinitely (invariant 2, and "a pipeline thread never waits
without a bound").

A fake `rsync` first on PATH reproduces the fork with a real process tree:
a mocked Popen cannot hold a pipe open from another process, which is the
whole failure. Every call runs in a thread joined with a bound, so code
that waits on the orphan FAILS here instead of hanging the suite. The
forks die on the first SIGTERM except in the modes that ignore it
(`stubborn`, `held-ok`), as real rsync's receiver in effect does —
test_rsync_real_daemon.py runs the real one.
"""
import contextlib
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from app.acquire import budget, engine
from app.acquire import transport_rsync as tr
from app.acquire.spec import Endpoint, RemoteFile, TransferAborted, Transport, TransportError
from app.transfer.bucket import Priority
from app.transfer.manager import get_manager

pytestmark = pytest.mark.skipif(not Path("/proc/self/stat").exists(),
                                reason="needs Linux /proc")

#: Stands in for a daemon-protocol rsync client: it FORKS, and the child
#: inherits stdout and stderr as rsync's receiver does. It records
#: "pid pgid child" once the child exists. In every mode but `ok` the child
#: would outlive the fake by 30 s — far past `_BOUND`. A destination ending
#: in "/" is a listing: `deliver` prints one entry instead of writing a file.
_FAKE = """#!/bin/sh
case "$FAKE_RSYNC_MODE" in
    ok)                   sleep 0.5 & ;;
    escaped|escaped-hang) python3 -c 'import os, time; os.setsid(); time.sleep(30)' & ;;
    stubborn|held-ok)     sh -c 'trap "" TERM; exec sleep 30' & ;;
    *)                    sleep 30 & ;;
esac
child=$!
pgid=$(cut -d' ' -f5 /proc/$$/stat)
echo "$$ $pgid $child" > "$FAKE_RSYNC_PIDS.tmp"
mv "$FAKE_RSYNC_PIDS.tmp" "$FAKE_RSYNC_PIDS"
for dest; do :; done
deliver() {
    case "$dest" in
        */) echo "-rw-r--r--             11 2026/09/14 12:00:00 1/pg1.txt" ;;
        *)  printf 'hello world' > "$dest" ;;
    esac
}
case "$FAKE_RSYNC_MODE" in
    ok|held-ok) deliver; exit 0 ;;
    orphan|escaped)
        echo "rsync error: timeout waiting for daemon connection (code 35) at socket.c(278) [Receiver=3.2.7]" >&2
        exit 35 ;;
    grow) printf 'x' > "$dest"; wait ;;
    *)    wait ;;
esac
"""

#: The fixed code needs ~5 s at worst here (a 1 s poll, the 2 s exit grace,
#: a kill grace cut to 1 s); code that waits on the orphan needs its 30.
_BOUND = 12.0

#: What a stop or a held exit may take when the fork survives SIGTERM: a
#: poll, the exit grace and the kill (~3.5 s) — never the 10 s stop grace a
#: SIGTERM to the whole group waits out, nor a SIGTERM step before the kill.
_PROMPT = 6.0

_ENDPOINT = Endpoint(transport=Transport.RSYNC, url="host::mod")


class _FakeRsync:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        script = bindir / "rsync"
        script.write_text(_FAKE, newline="\n")
        script.chmod(0o755)
        self._pids = tmp_path / "pids"
        self._monkeypatch = monkeypatch
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        monkeypatch.setenv("FAKE_RSYNC_PIDS", str(self._pids))

    def use(self, mode: str) -> None:
        self._monkeypatch.setenv("FAKE_RSYNC_MODE", mode)

    def started(self, within: float = 5.0) -> bool:
        until = time.monotonic() + within
        while not self._pids.exists():
            if time.monotonic() > until:
                return False
            time.sleep(0.02)
        return True

    def pids(self) -> tuple[int, int, int]:
        """(the fake's pid, its process group, its forked child)."""
        assert self.started(), "the fake rsync never ran"
        pid, pgid, child = (int(x) for x in self._pids.read_text().split())
        return pid, pgid, child

    def reap(self) -> None:
        """Kill whatever the test left, pass or fail: a fork outlives it by
        30 s, running into the tests after it."""
        if not self._pids.exists():
            return
        _, pgid, child = (int(x) for x in self._pids.read_text().split())
        with contextlib.suppress(OSError):
            os.kill(child, signal.SIGKILL)
        # never our own group: code without start_new_session shares it
        if pgid != os.getpgrp():
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    # the budget is not what is under test: no lease, no accounting
    @contextlib.contextmanager
    def no_reservation(endpoint_key=None):
        yield None
    monkeypatch.setattr(budget, "rsync_reservation", no_reservation)
    monkeypatch.setattr(budget, "note_external_bytes", lambda n: None)
    rsync = _FakeRsync(tmp_path, monkeypatch)
    yield rsync
    rsync.reap()


class _Call:
    """`call` in a daemon thread under a bound stop, as the fetch pool runs it."""

    def __init__(self, call):
        self.stop = threading.Event()
        self._box: dict = {}
        self.started_at = time.monotonic()
        self.stopped_at: float | None = None
        self.ended_at: float | None = None
        self._thread = threading.Thread(target=self._run, args=(call,), daemon=True)
        self._thread.start()

    def _run(self, call) -> None:
        with get_manager().bind(Priority.BACKFILL, stop=self.stop):
            try:
                self._box["result"] = call()
            except BaseException as e:   # noqa: BLE001 — carried to the test
                self._box["error"] = e
            finally:
                self.ended_at = time.monotonic()

    def stop_now(self) -> None:
        self.stopped_at = time.monotonic()
        self.stop.set()

    def outcome(self):
        """The call's result or exception, once it has ended within `_BOUND`."""
        self._thread.join(_BOUND)
        assert not self._thread.is_alive(), \
            f"still waiting after {_BOUND:g}s: an unbounded wait on the forked child"
        return self._box.get("error", self._box.get("result"))

    def took(self) -> float:
        """From the stop, or from the start when there was none, to the end."""
        return self.ended_at - (self.stopped_at or self.started_at)


def _state(pid: int) -> str | None:
    """The process's state letter; None once it has been reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return stat.rsplit(")", 1)[1].split()[0]


def _dead(pid: int, within: float = 3.0) -> bool:
    """Gone, or a zombie: dead. An orphan's zombie is whoever adopted it's
    to reap (PID 1 here), not ours."""
    until = time.monotonic() + within
    while _state(pid) not in (None, "Z", "X"):
        if time.monotonic() > until:
            return False
        time.sleep(0.02)
    return True


def _assert_nothing_left(fake: _FakeRsync) -> None:
    pid, _, child = fake.pids()
    assert _state(pid) is None, \
        f"the fake rsync {pid} is {_state(pid)!r}: the direct child must be reaped, " \
        f"never left a zombie"
    assert _dead(child), \
        f"the forked child {child} is still {_state(child)!r}: it dies with its group"


def _dest(tmp_path: Path) -> Path:
    return tmp_path / "corpus" / "1" / "pg1.txt"


def _fetch(tmp_path: Path):
    return lambda: tr.RsyncTransport().fetch_one(
        _ENDPOINT, RemoteFile(rel_path="1/pg1.txt"), _dest(tmp_path), 0)


def _listing(tmp_path: Path):
    return lambda: list(tr.RsyncTransport().list_files(_ENDPOINT, None))


_KINDS = {"fetch": _fetch, "listing": _listing}


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_an_exit_35_whose_receiver_lives_on_ends_within_seconds(fake, tmp_path, kind):
    """The node's case: rsync exits 35 and its receiver keeps the pipes."""
    fake.use("orphan")
    got = _Call(_KINDS[kind](tmp_path)).outcome()
    assert isinstance(got, TransportError) and not isinstance(got, TransferAborted), got
    assert "exit 35" in str(got), f"rsync's own exit code, as before: {got}"
    assert "timeout waiting for daemon connection" in str(got), \
        f"and its own words: {got}"
    _assert_nothing_left(fake)
    assert not _dest(tmp_path).exists()


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_stop_ends_rsync_and_everything_it_forked(fake, tmp_path, kind):
    """terminate() reached the parent only; the orphan kept the pipes and
    the communicate() after the kill waited on it with no timeout."""
    fake.use("hang")
    call = _Call(_KINDS[kind](tmp_path))
    assert fake.started(), "the fake rsync ran and forked"
    call.stop_now()
    got = call.outcome()
    assert isinstance(got, TransferAborted), f"a stop is never a mirror verdict: {got!r}"
    _assert_nothing_left(fake)
    assert not _dest(tmp_path).exists(), "nothing moves into place on a stop"


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_stop_whose_fork_survives_sigterm_ends_within_seconds(fake, tmp_path, kind):
    """Real rsync's receiver outlives a SIGTERM of its own. A SIGTERM to the
    whole group left it holding the pipes for the full 10 s stop grace, on
    every stop; the leader's exit is the signal to stop waiting."""
    fake.use("stubborn")
    call = _Call(_KINDS[kind](tmp_path))
    assert fake.started(), "the fake rsync ran and forked"
    call.stop_now()
    got = call.outcome()
    assert isinstance(got, TransferAborted), f"a stop is never a mirror verdict: {got!r}"
    assert call.took() < _PROMPT, f"the stop took {call.took():.1f}s"
    _assert_nothing_left(fake)


def test_a_stop_whose_holder_left_the_group_is_abandoned_not_waited_on(
        fake, tmp_path, monkeypatch):
    """No signal to the group reaches a holder that called setsid, so its
    pipes never close: the stop gives up on them and still ends."""
    fake.use("escaped-hang")
    monkeypatch.setattr(tr, "_KILL_GRACE", 1.0, raising=False)
    call = _Call(_fetch(tmp_path))
    assert fake.started(), "the fake rsync ran and forked"
    call.stop_now()
    got = call.outcome()
    assert isinstance(got, TransferAborted), f"a stop is never a mirror verdict: {got!r}"
    assert call.took() < _PROMPT, f"the stop took {call.took():.1f}s"
    pid, _, _ = fake.pids()
    assert _state(pid) is None, "the direct child is reaped all the same"


def test_a_stop_during_the_exit_grace_still_ends_within_bounds(fake, tmp_path):
    """The stop lands after rsync has exited, while its orphan holds the
    pipes: that wait is bounded on its own, and nothing is left."""
    fake.use("orphan")
    call = _Call(_fetch(tmp_path))
    pid, _, _ = fake.pids()
    until = time.monotonic() + 5
    while _state(pid) is not None:          # reaped: the exit grace has begun
        assert time.monotonic() < until, "the transport never reaped its child"
        time.sleep(0.02)
    call.stop_now()
    got = call.outcome()
    assert isinstance(got, TransportError), repr(got)
    _assert_nothing_left(fake)
    assert not _dest(tmp_path).exists()


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_deadline_ends_rsync_and_everything_it_forked(fake, tmp_path, monkeypatch, kind):
    fake.use("hang")
    monkeypatch.setattr(tr, "_DEFAULT_TIMEOUT", 1)
    monkeypatch.setattr(tr, "_LISTING_TIMEOUT", 1)
    got = _Call(_KINDS[kind](tmp_path)).outcome()
    assert isinstance(got, TransportError) and not isinstance(got, TransferAborted), got
    assert "timed out after 1s" in str(got), got
    _assert_nothing_left(fake)
    assert not _dest(tmp_path).exists()


def test_a_deadline_whose_holder_left_the_group_is_abandoned_not_waited_on(
        fake, tmp_path, monkeypatch):
    """Even the deadline's SIGKILL cannot close pipes held from outside the
    group: they are abandoned, and the verdict is still the deadline's."""
    fake.use("escaped-hang")
    monkeypatch.setattr(tr, "_DEFAULT_TIMEOUT", 1)
    monkeypatch.setattr(tr, "_KILL_GRACE", 1.0, raising=False)
    got = _Call(_fetch(tmp_path)).outcome()
    assert isinstance(got, TransportError) and not isinstance(got, TransferAborted), got
    assert "timed out after 1s" in str(got), got
    pid, _, _ = fake.pids()
    assert _state(pid) is None, "the direct child is reaped all the same"
    assert not _dest(tmp_path).exists()


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_a_clean_exit_still_succeeds(fake, tmp_path, monkeypatch, kind):
    """Exit 0, and the forked child ends half a second later — past a poll,
    inside the exit grace: the normal case, and still a success."""
    fake.use("ok")
    monkeypatch.setattr(tr, "_POLL_SECONDS", 0.1)
    got = _Call(_KINDS[kind](tmp_path)).outcome()
    assert not isinstance(got, BaseException), repr(got)
    if kind == "fetch":
        dest = _dest(tmp_path)
        assert got == 11 and dest.read_bytes() == b"hello world"
        assert not dest.with_name(dest.name + ".part").exists(), \
            "the staging file moved into place"
    else:
        assert [(f.rel_path, f.size) for f in got] == [("1/pg1.txt", 11)], got
    pid, pgid, _ = fake.pids()
    assert pgid == pid, \
        "rsync leads its own process group — else no signal could reach what it forks"
    _assert_nothing_left(fake)


@pytest.mark.parametrize("kind", sorted(_KINDS))
def test_an_exit_0_whose_fork_still_holds_the_pipes_is_a_deferral(fake, tmp_path, kind):
    """An orphaned transfer or listing is never reported as a success: the
    fork had to be killed, so what it was doing is unknown. Nor is it the
    file's fault — the engine defers it rather than writing an error. And
    the fork, which ignores SIGTERM as rsync's receiver does, is killed
    outright rather than asked first."""
    fake.use("held-ok")
    call = _Call(_KINDS[kind](tmp_path))
    got = call.outcome()
    assert isinstance(got, TransportError) and "exit 0" in str(got), repr(got)
    assert engine.transient(str(got)), f"a deferral, not a file error: {got}"
    assert call.took() < _PROMPT, f"the held exit took {call.took():.1f}s"
    assert not _dest(tmp_path).exists(), "nothing moves into place"
    _assert_nothing_left(fake)


def test_a_holder_no_signal_reaches_is_abandoned_not_waited_on(fake, tmp_path, monkeypatch):
    """The last bound: a fork that left the group (setsid) survives even
    SIGKILL to it, so its pipes never close. The transport stops reading
    and closes them instead of waiting — and still quotes what rsync said
    before it exited, from the output read by then."""
    fake.use("escaped")
    monkeypatch.setattr(tr, "_KILL_GRACE", 1.0, raising=False)
    got = _Call(_fetch(tmp_path)).outcome()
    assert isinstance(got, TransportError) and "exit 35" in str(got), repr(got)
    assert "timeout waiting for daemon connection" in str(got), got
    pid, _, _ = fake.pids()
    assert _state(pid) is None, "the direct child is reaped all the same"
    assert not _dest(tmp_path).exists()


def test_an_exception_escaping_the_supervisor_kills_the_group(fake, tmp_path, monkeypatch):
    """rsync leads its own session, so nothing else will ever signal it: a
    raise out of the supervising loop left it moving bytes after the lease
    was released (invariant 5), with the next attempt free to start a second
    --inplace writer on the same .part."""
    fake.use("grow")

    def accounting_fails(n):
        raise RuntimeError("accounting failed")

    monkeypatch.setattr(budget, "note_external_bytes", accounting_fails)
    got = _Call(_fetch(tmp_path)).outcome()
    assert isinstance(got, RuntimeError) and "accounting failed" in str(got), repr(got)
    _assert_nothing_left(fake)


def test_a_ctrl_c_with_no_stop_bound_kills_the_group(fake, tmp_path, monkeypatch):
    """`corpus status` lists with no stop bound and SIGINT at its default:
    Ctrl-C — or a hangup, which the CLI makes one — arrives as a
    KeyboardInterrupt inside the loop, and no longer reaches rsync, which
    leads its own session. It must not leave rsync running behind it."""
    fake.use("hang")

    class _CtrlC:
        def is_set(self) -> bool:
            if fake.started(within=0):       # once rsync has forked
                raise KeyboardInterrupt
            return False

    monkeypatch.setattr(tr, "_current_stop", lambda: _CtrlC())
    got = _Call(_listing(tmp_path)).outcome()
    assert isinstance(got, KeyboardInterrupt), repr(got)
    _assert_nothing_left(fake)
