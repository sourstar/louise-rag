"""app/plugins/walk.py: every corpus walk through the links its origin holds
(wikipedia-v8, 2026-10-30: rglob enters no symlinked directory, on Python
3.11, 3.12 and 3.13 alike)."""
from pathlib import Path

from app.plugins import walk


def test_rglob_itself_does_not_enter_a_link_which_is_why_this_exists(tmp_path):
    real = tmp_path / "real" / "de"
    real.mkdir(parents=True)
    (real / "000.parquet").write_bytes(b"x")
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "data").symlink_to(tmp_path / "real", target_is_directory=True)
    assert list(origin.rglob("*.parquet")) == []
    assert walk.files(origin, "*.parquet") == [origin / "data" / "de" / "000.parquet"]


def test_a_link_loop_ends_and_a_twice_linked_directory_yields_once(tmp_path):
    root = tmp_path / "origin"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.zip").write_bytes(b"z")
    (root / "a" / "back").symlink_to(root, target_is_directory=True)     # a loop
    (root / "b").symlink_to(root / "a", target_is_directory=True)        # a second link
    got = walk.files(root, "*.zip")
    assert len(got) == 1 and got[0].name == "x.zip"


def test_only_files_whose_name_matches_case_sensitively(tmp_path):
    (tmp_path / "x.parquet").mkdir()                # a directory, not a shard
    (tmp_path / "a.parquet").write_bytes(b"")
    (tmp_path / "B.PARQUET").write_bytes(b"")
    assert walk.files(tmp_path, "*.parquet") == [tmp_path / "a.parquet"]
    assert walk.files(tmp_path / "missing", "*") == []
