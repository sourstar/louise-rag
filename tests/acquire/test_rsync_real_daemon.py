"""A stop against REAL rsync ends the way rsync ends it, in about a second.

The fakes in test_rsync_orphans.py are shell scripts whose forks die on the
first SIGTERM; real rsync's receiver does not. Sent to the whole group, a
stop's SIGTERM reached the receiver too, which then waited for a controlled
shutdown from a generator already gone — re-parented, holding the pipes —
until the SIGKILL 10 s later, on every pause, seed-only, SIGTERM or
`node stop` with a fetch in flight (review 2026-09-14: 0.5 s with the
SIGTERM to the leader alone, 10.5 s with it to the group). This runs the
image's rsync against `rsync --daemon --no-detach` on loopback, over the
daemon protocol the node uses for gutenberg.
"""
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.acquire import budget
from app.acquire import transport_rsync as tr
from app.acquire.spec import Endpoint, RemoteFile, TransferAborted, Transport
from app.transfer.bucket import Priority
from app.transfer.manager import get_manager

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None or not Path("/proc/self/stat").exists(),
    reason="needs the rsync binary and Linux /proc")

_SIZE = 8 * 1024 * 1024
#: KiB/s, through the lease as the budget hands it out: 16 s for the whole
#: file, so the stop always lands mid-transfer
_BWLIMIT = 500
#: rsync ends in 0.5-0.9 s once signalled, and the transport sees the stop
#: within one `_POLL_SECONDS`; the group-first SIGTERM took 10+
_PROMPT = 4.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.fixture
def daemon(tmp_path):
    """(the module's URL, the file it serves)."""
    mod = tmp_path / "mod"
    mod.mkdir()
    source = mod / "big.bin"
    source.write_bytes(os.urandom(_SIZE))
    conf = tmp_path / "rsyncd.conf"
    # as root the daemon would drop to `nobody`, who cannot enter tmp_path
    as_root = "uid = root\ngid = root\n" if os.geteuid() == 0 else ""
    conf.write_text(f"use chroot = no\n{as_root}pid file = {tmp_path}/rsyncd.pid\n"
                    f"[mod]\npath = {mod}\nread only = yes\n")
    port = _free_port()
    proc = subprocess.Popen(
        ["rsync", "--daemon", "--no-detach", f"--port={port}", "--address=127.0.0.1",
         f"--config={conf}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        until = time.monotonic() + 10
        while not _listening(port):
            if proc.poll() is not None or time.monotonic() > until:
                pytest.fail("the rsync daemon did not start")
            time.sleep(0.05)
        yield f"rsync://127.0.0.1:{port}/mod", source
    finally:
        proc.kill()
        proc.wait(5)


def _members(pgid: int) -> list[str]:
    """The live processes of process group `pgid` (zombies are dead)."""
    out = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            fields = (p / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            out.append(f"{p.name}:{fields[0]}")
    return out


class _Fetch:
    """fetch_one in a thread under a bound stop, as the fetch pool runs it —
    recording rsync's pid and every signal sent to its group."""

    def __init__(self, monkeypatch, url: str, dest: Path, bwlimit: int | None):
        @contextlib.contextmanager
        def lease(endpoint_key=None):
            yield bwlimit
        monkeypatch.setattr(budget, "rsync_reservation", lease)
        monkeypatch.setattr(budget, "note_external_bytes", lambda n: None)
        self.leaders: list[int] = []
        self.group_signals: list[signal.Signals] = []
        real_popen, real_killpg = subprocess.Popen, os.killpg

        def popen(*a, **k):
            proc = real_popen(*a, **k)
            self.leaders.append(proc.pid)
            return proc

        def killpg(pid, sig):
            self.group_signals.append(signal.Signals(sig))
            real_killpg(pid, sig)

        monkeypatch.setattr(tr.subprocess, "Popen", popen)
        monkeypatch.setattr(tr.os, "killpg", killpg)
        self.stop = threading.Event()
        self.box: dict = {}
        endpoint = Endpoint(transport=Transport.RSYNC, url=url)

        def run():
            with get_manager().bind(Priority.BACKFILL, stop=self.stop):
                try:
                    self.box["result"] = tr.RsyncTransport().fetch_one(
                        endpoint, RemoteFile(rel_path="big.bin"), dest, 0)
                except BaseException as e:   # noqa: BLE001 — carried to the test
                    self.box["error"] = e

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


def test_a_real_fetch_over_the_daemon_protocol_lands_whole(daemon, tmp_path, monkeypatch):
    """The exit path with a real receiver: it closes the pipes with its
    parent, so a clean exit is never taken for an orphan (`held`)."""
    url, source = daemon
    dest = tmp_path / "out" / "big.bin"
    fetch = _Fetch(monkeypatch, url, dest, bwlimit=None)
    fetch.thread.join(60)
    assert not fetch.thread.is_alive()
    assert fetch.box.get("result") == _SIZE, fetch.box
    assert dest.read_bytes() == source.read_bytes()
    assert fetch.group_signals == [], "nothing was signalled on a clean exit"


def test_a_stop_ends_real_rsync_its_own_way_within_seconds(daemon, tmp_path, monkeypatch):
    url, source = daemon
    dest = tmp_path / "out" / "big.bin"
    part = dest.with_name(dest.name + ".part")
    fetch = _Fetch(monkeypatch, url, dest, bwlimit=_BWLIMIT)
    until = time.monotonic() + 20
    while not (part.exists() and part.stat().st_size > 256 * 1024):
        assert fetch.thread.is_alive() and time.monotonic() < until, \
            f"the transfer never got going: {fetch.box}"
        time.sleep(0.05)

    stopped_at = time.monotonic()
    fetch.stop.set()
    fetch.thread.join(30)
    took = time.monotonic() - stopped_at
    assert not fetch.thread.is_alive()
    assert isinstance(fetch.box.get("error"), TransferAborted), fetch.box
    assert took < _PROMPT, \
        f"the stop took {took:.1f}s: rsync was not left to end its own transfer"
    assert signal.SIGKILL not in fetch.group_signals, \
        f"rsync ended itself; no SIGKILL was needed: {fetch.group_signals}"

    leader = fetch.leaders[0]
    deadline = time.monotonic() + 2
    while _members(leader) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _members(leader) == [], "nothing of rsync's group is left alive"

    data = part.read_bytes()
    assert 0 < len(data) < _SIZE, len(data)
    assert data == source.read_bytes()[:len(data)], \
        "the .part is a true prefix, so the next run resumes from it"
    assert not dest.exists(), "nothing moves into place on a stop"
