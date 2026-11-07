"""tools/archive_backend.py — a backend's installed files kept as
`<tree hex>.tar.zst`, verified by unpacking (docs/notes/v5-migration.md G6;
02 §2.4, 09 §9.3 amended 2026-10-08)."""
import hashlib
import importlib.util
import json
import os
import re
import shutil
from pathlib import Path

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
    name = f"{want.split(':')[1]}.tar.zst"
    assert sorted(p.name for p in (tmp_path / "keep").iterdir()) == [name, f"{name}.verified"]


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


# ---- the sidecar: what the node's host, which has no zstd, checks (München, 2026-11-07) ----

def test_the_sidecar_records_the_tree_the_unpack_proved_and_the_files_sha256(tmp_path):
    src = _tree(tmp_path / "backend")
    out = ab.archive_backend(src, tmp_path / "keep")
    archive = Path(out["path"])
    assert out["sidecar"] == str(archive) + ".verified"
    doc = json.loads(Path(out["sidecar"]).read_text())
    assert set(doc) == {"tree", "sha256", "bytes", "verified_at", "tool", "tool_version"}
    assert doc["tree"] == ab.tree_digest(src) == out["digest"]
    assert doc["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest(), "what sha256sum prints"
    assert doc["bytes"] == archive.stat().st_size
    assert doc["tool"] == "tools/archive_backend.py" and doc["tool_version"] == ab.VERSION == 2
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["verified_at"])
    assert ab.check_sidecar(archive, doc["tree"]) is None


def test_verified_again_rewrites_the_sidecar(tmp_path):
    src = _tree(tmp_path / "backend")
    out = ab.archive_backend(src, tmp_path / "keep")
    Path(out["sidecar"]).write_text('{"tree": "stale"}')
    again = ab.archive_backend(src, tmp_path / "keep")
    assert again["verified"] and json.loads(Path(again["sidecar"]).read_text())["tree"] == out["digest"]


def test_no_sidecar_for_an_archive_that_did_not_verify(tmp_path, monkeypatch):
    """A new archive that unpacks to another tree: neither file. One already
    kept that no longer verifies: its old sidecar goes, never outliving its proof."""
    src = _tree(tmp_path / "backend")
    real = ab.verify
    monkeypatch.setattr(ab, "verify", lambda archive, want, scratch: "sha256:" + "0" * 64)
    out = ab.archive_backend(src, tmp_path / "keep")
    assert not out["verified"] and out["sidecar"] is None
    assert list((tmp_path / "keep").iterdir()) == []
    monkeypatch.setattr(ab, "verify", real)
    kept = ab.archive_backend(src, tmp_path / "keep")
    assert Path(kept["sidecar"]).is_file()
    monkeypatch.setattr(ab, "verify", lambda archive, want, scratch: "sha256:" + "0" * 64)
    again = ab.archive_backend(src, tmp_path / "keep")
    assert not again["verified"] and again["sidecar"] is None
    assert not Path(kept["sidecar"]).exists() and Path(kept["path"]).is_file()


def test_the_one_off_writes_the_sidecar_of_an_archive_already_kept(tmp_path, capsys):
    """`--sidecar`: the archives made before the sidecar, verified against
    the tree digest their names state."""
    src = _tree(tmp_path / "backend")
    out = ab.archive_backend(src, tmp_path / "keep")
    archive, sidecar = Path(out["path"]), Path(out["sidecar"])
    sidecar.unlink()                                        # as on the node: no sidecar yet
    other = _tree(tmp_path / "other")
    (other / "lib" / "deep" / "weights.bin").write_bytes(b"another engine")
    renamed = tmp_path / "keep" / f"{'ab' * 32}.tar.zst"    # unpacks to another tree
    ab.write_archive(other, renamed)
    assert ab.main(["--sidecar", str(archive), str(renamed)]) == 1
    std = capsys.readouterr()
    lines = [json.loads(line) for line in std.out.splitlines()]
    assert [r["verified"] for r in lines] == [True, False]
    assert json.loads(sidecar.read_text())["tree"] == out["digest"]
    assert not ab.sidecar_of(renamed).exists() and "no sidecar" in std.err
    assert ab.main(["--sidecar", str(archive)]) == 0
    with pytest.raises(SystemExit):
        ab.sidecar(tmp_path / "keep" / "engine.tar.zst")    # no tree digest in its name


def test_the_one_off_without_zstd_says_so_and_writes_nothing(tmp_path, monkeypatch, capsys):
    src = _tree(tmp_path / "backend")
    out = ab.archive_backend(src, tmp_path / "keep")
    Path(out["sidecar"]).unlink()
    no_zstd(monkeypatch)
    assert ab.main(["--sidecar", out["path"]]) == 2
    assert "no zstd here" in capsys.readouterr().err
    assert not Path(out["sidecar"]).exists()


def no_zstd(monkeypatch):
    """The node's host (München, 2026-11-07): no zstandard, no pyarrow, no
    `zstd` program — archive_backend's three codecs, each made absent."""
    find_spec, which = importlib.util.find_spec, shutil.which
    monkeypatch.setattr(ab.importlib.util, "find_spec",
                        lambda name, *a: None if name in ("zstandard", "pyarrow") else find_spec(name, *a))
    monkeypatch.setattr(ab.shutil, "which", lambda name, *a: None if name == "zstd" else which(name, *a))
    with pytest.raises(ab.NoZstd):
        ab._codec()
