"""tools/archive_backend.py — a backend's installed files kept as
`<tree hex>.tar.zst`, verified by unpacking (docs/notes/v5-migration.md G6;
02 §2.4, 09 §9.3 amended 2026-10-08)."""
import json
import os

import pytest

from tools import archive_backend as ab


def _tree(d):
    """What a backend directory holds: files, an executable, links relative
    and absolute (a venv's interpreter), an empty directory, nesting, and
    LocalAI's install record, which the digest leaves out."""
    (d / "lib" / "deep").mkdir(parents=True)
    (d / "lib" / "deep" / "weights.bin").write_bytes(os.urandom(70_000))
    (d / "run.sh").write_text("#!/bin/sh\nexec ./backend \"$@\"\n")
    os.chmod(d / "run.sh", 0o755)
    (d / "empty").mkdir()
    os.symlink("lib/deep/weights.bin", d / "weights")
    os.symlink("/usr/bin/python3", d / "python")
    (d / "metadata.json").write_text('{"name": "x"}')
    return d


def test_the_archive_is_named_by_the_tree_digest_and_unpacks_to_it(tmp_path, capsys):
    src = _tree(tmp_path / "backend")
    src.mkdir(exist_ok=True)
    want = ab.tree_digest(src)
    assert ab.main([str(src), str(tmp_path / "keep")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["digest"] == want and out["verified"] is True
    assert out["path"] == str(tmp_path / "keep" / f"{want.split(':')[1]}.tar.zst")
    assert (tmp_path / "keep" / f"{want.split(':')[1]}.tar.zst").read_bytes()[:4] == \
        b"\x28\xb5\x2f\xfd", "a standard zstd frame: `zstd -d | tar x` reads it anywhere"
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    ab.unpack(tmp_path / "keep" / f"{want.split(':')[1]}.tar.zst", unpacked)
    assert ab.tree_digest(unpacked) == want
    assert os.readlink(unpacked / "python") == "/usr/bin/python3", "links kept, never followed"
    assert os.stat(unpacked / "run.sh").st_mode & 0o111
    assert (unpacked / "empty").is_dir()
    assert sorted(p.name for p in (tmp_path / "keep").iterdir()) == [f"{want.split(':')[1]}.tar.zst"]


def test_re_running_verifies_the_archive_already_there(tmp_path):
    src = _tree(tmp_path / "backend")
    first = ab.archive_backend(src, tmp_path / "keep")
    again = ab.archive_backend(src, tmp_path / "keep")
    assert again == first and again["verified"]


def test_an_archive_that_unpacks_to_another_tree_is_not_kept(tmp_path, monkeypatch, capsys):
    src = _tree(tmp_path / "backend")
    monkeypatch.setattr(ab, "verify", lambda archive, want, scratch: "sha256:" + "0" * 64)
    assert ab.main([str(src), str(tmp_path / "keep")]) == 1
    assert list((tmp_path / "keep").iterdir()) == [], "neither the archive nor its part"


def test_any_directory_works_and_the_digest_is_of_the_bytes(tmp_path):
    a = tmp_path / "ir-a"
    a.mkdir()
    (a / "model.xml").write_text("<net/>")
    (a / "model.bin").write_bytes(b"\x00" * 1000)
    out = ab.archive_backend(a, tmp_path / "keep")
    assert out["verified"] and out["digest"] == ab.tree_digest(a)


def test_a_missing_directory_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        ab.archive_backend(tmp_path / "nope", tmp_path / "keep")
