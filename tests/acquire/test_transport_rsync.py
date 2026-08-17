"""app/acquire/transport_rsync.py — tested against local directories (no
network needed; rsync supports local-to-local sync natively), skipped where
the rsync binary isn't installed.
"""
import shutil
import subprocess
import threading

import pytest

from app import platform_config
from app.acquire.spec import Endpoint, RemoteFile, TransferAborted, Transport
from app.acquire.transport_rsync import RsyncTransport
from app.transfer import schedule as sched
from app.transfer.bucket import Priority
from app.transfer.manager import TransferManager, set_manager

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")


@pytest.fixture(autouse=True)
def _isolated_manager():
    yield
    set_manager(None)


def _manager(down=None):
    platform = platform_config.PlatformConfig(
        bandwidth=platform_config.BandwidthConfig(down_max_bps=sched.parse_rate(down)))
    manager = TransferManager(platform=platform)
    set_manager(manager)
    return manager


def test_list_files_parses_rsync_listing(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello")
    (src / "b.txt").write_bytes(b"world!!")
    (src / "sub").mkdir()
    (src / "sub" / "c.txt").write_bytes(b"nested")

    endpoint = Endpoint(transport=Transport.RSYNC, url=str(src))
    files = {f.rel_path: f for f in RsyncTransport().list_files(endpoint, None)}
    assert files["a.txt"].size == 5
    assert files["b.txt"].size == 7
    assert "sub" not in files   # directories excluded; contents traversed by the transfer itself


def test_fetch_one_copies_file(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"hello world")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    endpoint = Endpoint(transport=Transport.RSYNC, url=str(src))
    remote = RemoteFile(rel_path="a.txt", size=11)
    size = RsyncTransport().fetch_one(endpoint, remote, dest_dir / "a.txt", resume_from=0)
    assert size == 11
    assert (dest_dir / "a.txt").read_bytes() == b"hello world"
    assert not (dest_dir / "a.txt.part").exists(), "staging file must be renamed away"


# ---------------- P0.7: the budget reaches rsync ----------------

def _captured_argv(monkeypatch):
    """Intercept Popen so the exact argv is observable without needing a slow
    or throttled real transfer."""
    seen = {}
    real_popen = subprocess.Popen

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return ("", "")

        def terminate(self):
            pass

        def kill(self):
            pass

    def _fake(argv, *a, **kw):
        seen["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake)
    return seen, real_popen


def test_bwlimit_is_derived_from_the_shared_budget(tmp_path, monkeypatch):
    _manager(down="10MB")
    seen, _ = _captured_argv(monkeypatch)
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(tmp_path))
    remote = RemoteFile(rel_path="a.txt", size=11)
    part = tmp_path / "dest" / "a.txt.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"hello world")

    RsyncTransport().fetch_one(endpoint, remote, tmp_path / "dest" / "a.txt", resume_from=0)
    flags = [a for a in seen["argv"] if a.startswith("--bwlimit=")]
    assert flags, "rsync must be given a --bwlimit derived from the shared budget"
    kbps = int(flags[0].split("=", 1)[1])
    assert kbps == pytest.approx(10_000_000 // 1024, rel=0.01)


def test_bwlimit_is_never_zero_because_zero_means_unlimited_to_rsync(tmp_path, monkeypatch):
    """--bwlimit=0 uncaps rsync entirely — exactly the wrong behavior when the
    budget is most contended. The floor must never round down to 0."""
    manager = _manager(down="10MB")
    manager.record_throughput(str(tmp_path), byte_count=1, seconds=10_000)
    seen, _ = _captured_argv(monkeypatch)
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(tmp_path))
    remote = RemoteFile(rel_path="a.txt", size=11)
    part = tmp_path / "dest" / "a.txt.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"hello world")

    RsyncTransport().fetch_one(endpoint, remote, tmp_path / "dest" / "a.txt", resume_from=0)
    kbps = int([a for a in seen["argv"] if a.startswith("--bwlimit=")][0].split("=", 1)[1])
    assert kbps >= 1


def test_no_bwlimit_flag_when_the_budget_is_unlimited(tmp_path, monkeypatch):
    _manager(down=None)
    seen, _ = _captured_argv(monkeypatch)
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(tmp_path))
    remote = RemoteFile(rel_path="a.txt", size=11)
    part = tmp_path / "dest" / "a.txt.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"hello world")

    RsyncTransport().fetch_one(endpoint, remote, tmp_path / "dest" / "a.txt", resume_from=0)
    assert not [a for a in seen["argv"] if a.startswith("--bwlimit=")]


def test_lease_is_released_after_the_transfer(tmp_path, monkeypatch):
    manager = _manager(down="10MB")
    seen, _ = _captured_argv(monkeypatch)
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(tmp_path))
    remote = RemoteFile(rel_path="a.txt", size=11)
    part = tmp_path / "dest" / "a.txt.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"hello world")

    RsyncTransport().fetch_one(endpoint, remote, tmp_path / "dest" / "a.txt", resume_from=0)
    assert manager.bucket().core.leased == pytest.approx(0.0)
    assert manager.bucket().core.rate == pytest.approx(10_000_000)


def test_stop_event_aborts_rsync_and_leaves_the_final_path_untouched(tmp_path):
    """A killed rsync must never leave a torn file at the FINAL path, where
    discovery.py would ingest it as complete."""
    _manager()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 4_000_000)
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    stop = threading.Event()
    stop.set()
    manager = _manager()
    endpoint = Endpoint(transport=Transport.RSYNC, url=str(src))
    remote = RemoteFile(rel_path="a.txt", size=4_000_000)
    with manager.bind(Priority.BACKFILL, stop=stop):
        with pytest.raises(TransferAborted):
            RsyncTransport().fetch_one(endpoint, remote, dest_dir / "a.txt", resume_from=0)
    assert not (dest_dir / "a.txt").exists(), "final path must never hold a torn file"
