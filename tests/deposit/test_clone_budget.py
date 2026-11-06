"""A deposit source's git clone draws from the budget as it lands
(invariant 5; 06 §6.7): git takes no rate cap, so the clone's `.git` is
measured while it runs, its process group paused while each increment is
drawn, and bounded in bytes and time. A local repository, no network."""
from __future__ import annotations

import os
import subprocess

import pytest

from app.acquire import budget, spec
from app.hold import sources


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "origin"
    r.mkdir()
    git = ["git", "-C", str(r), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    (r / "blob.bin").write_bytes(os.urandom(300_000))     # incompressible: a real pack
    subprocess.run(git + ["add", "."], check=True)
    subprocess.run(git + ["commit", "-qm", "one"], check=True)
    return "file://" + str(r)


def test_every_byte_the_clone_landed_is_drawn(repo, monkeypatch):
    drawn: list[int] = []
    monkeypatch.setattr(budget, "acquire_bytes", lambda n: drawn.append(n))
    landed: list[int] = []
    real = sources._landed
    monkeypatch.setattr(sources, "_landed", lambda d: landed.append(real(d)) or landed[-1])
    text = sources.fetch_git(repo)
    assert "--- File: README.md ---" in text
    assert sum(drawn) == landed[-1] > 300_000


def test_a_clone_over_the_bound_is_stopped(repo, monkeypatch):
    monkeypatch.setattr(budget, "acquire_bytes", lambda n: None)
    monkeypatch.setattr(sources, "MAX_CLONE_BYTES", 1000)
    with pytest.raises(sources.SourceError, match="more than 1000 bytes"):
        sources.fetch_git(repo)


def test_a_stop_while_drawing_kills_the_clone(repo, monkeypatch):
    """SIGTERM parks threads inside the bucket (budget.acquire_bytes raises
    TransferAborted): the clone's group is killed, not left running."""
    started: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def popen(*a, **k):
        started.append(real_popen(*a, **k))
        return started[-1]

    def abort(n):
        raise spec.TransferAborted("stop")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(budget, "acquire_bytes", abort)
    with pytest.raises(spec.TransferAborted):
        sources.fetch_git(repo)
    assert started and started[0].returncode is not None


def test_a_failed_clone_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "acquire_bytes", lambda n: None)
    with pytest.raises(sources.SourceError, match="git clone failed"):
        sources.fetch_git("file://" + str(tmp_path / "nothing-here"))
