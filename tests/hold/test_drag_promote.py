"""Promoting a built file into the drag when the drag is on other storage.

The drag's bytes may sit on bulk storage mounted at `drag/`, as `origin/`'s
do (operator, 2026-09-21), while `records build` stages under `state/` on
the fast pool. A rename cannot cross two filesystems (EXDEV), and the
promote used to be a bare `os.replace` — the first build into a mounted
drag would have died on its first shard.
"""
import errno
import os
import shutil
from pathlib import Path

import pytest

from app.hold import drag as drag_mod
from app.hold import records as rec


def _exdev_on_cross_dir(monkeypatch):
    """Make os.replace behave as it does across two mounts: a rename
    between different directories fails with EXDEV, one inside a
    directory (the temporary over the target) goes through."""
    real = os.replace

    def replace(src, dst):
        if Path(src).parent != Path(dst).parent:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", replace)


def test_same_filesystem_is_a_rename(tmp_path):
    src, target = tmp_path / "a" / "x.bin", tmp_path / "b" / "x.bin"
    src.parent.mkdir()
    target.parent.mkdir()
    src.write_bytes(b"shard")
    drag_mod.promote(src, target)
    assert target.read_bytes() == b"shard"
    assert not src.exists()


def test_across_filesystems_the_bytes_arrive_whole(tmp_path, monkeypatch):
    _exdev_on_cross_dir(monkeypatch)
    src, target = tmp_path / "state" / "x.bin", tmp_path / "drag" / "x.bin"
    src.parent.mkdir()
    target.parent.mkdir()
    payload = os.urandom(3 << 20)
    src.write_bytes(payload)
    drag_mod.promote(src, target)
    assert target.read_bytes() == payload
    assert not src.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == ["x.bin"], \
        "no temporary left in the drag"


def test_a_failed_copy_leaves_no_partial_file_and_keeps_the_source(tmp_path, monkeypatch):
    _exdev_on_cross_dir(monkeypatch)
    src, target = tmp_path / "state" / "x.bin", tmp_path / "drag" / "x.bin"
    src.parent.mkdir()
    target.parent.mkdir()
    src.write_bytes(b"shard")

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(shutil, "copyfileobj", full)
    with pytest.raises(OSError) as e:
        drag_mod.promote(src, target)
    assert e.value.errno == errno.ENOSPC
    assert list(target.parent.iterdir()) == []
    assert src.read_bytes() == b"shard"


def test_other_errors_are_not_mistaken_for_a_crossing(tmp_path):
    with pytest.raises(FileNotFoundError):
        drag_mod.promote(tmp_path / "missing", tmp_path / "x")


def test_records_build_promotes_into_a_drag_on_another_filesystem(tmp_path, monkeypatch):
    _exdev_on_cross_dir(monkeypatch)
    staging, drag = tmp_path / "state" / "build", tmp_path / "drag"
    (staging / "records" / "en").mkdir(parents=True)
    (staging / "figures" / "en").mkdir(parents=True)
    (staging / "records" / "en" / "00000.jsonl.zst").write_bytes(b"r0")
    (staging / "records" / "en" / "00001.jsonl.zst").write_bytes(b"r1")
    (staging / "figures" / "en" / "00000.fig.zst").write_bytes(b"f0")
    assert rec._promote_records(staging, drag) == 2
    assert (drag / "records" / "en" / "00000.jsonl.zst").read_bytes() == b"r0"
    assert (drag / "records" / "en" / "00001.jsonl.zst").read_bytes() == b"r1"
    assert (drag / "figures" / "en" / "00000.fig.zst").read_bytes() == b"f0"
    assert not list((staging / "records").rglob("*.zst"))


def test_a_real_second_filesystem_when_the_host_has_one(tmp_path):
    """The same, against the kernel rather than a stand-in: /dev/shm is a
    tmpfs, a different device from the test's tmp_path in a container."""
    shm = Path("/dev/shm")
    if not shm.is_dir() or shm.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("no second filesystem here")
    staging = Path(os.path.join(shm, f"promote-{os.getpid()}"))
    staging.mkdir()
    try:
        src = staging / "x.bin"
        src.write_bytes(b"across")
        target = tmp_path / "x.bin"
        with pytest.raises(OSError) as e:
            os.replace(src, target)
        assert e.value.errno == errno.EXDEV, "the premise: a rename cannot cross"
        drag_mod.promote(src, target)
        assert target.read_bytes() == b"across"
        assert not src.exists()
    finally:
        shutil.rmtree(staging, ignore_errors=True)
