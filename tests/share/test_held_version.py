"""A file is held at the snapshot's VERSION, not merely its size
(app/share/holdings.py, 2026-10-12): a re-issued part of the same length
was counted held and never fetched."""
from app.hold.shard import MemberFile
from app.share import holdings


def test_a_same_size_file_marked_under_another_root_is_missing(tmp_path):
    p = tmp_path / "records" / "lang=en" / "00001.jsonl.zst"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x" * 10)
    f = MemberFile("records/lang=en/00001.jsonl.zst", "sha256:new", 10)
    assert holdings.file_held(tmp_path, f), "unmarked (built here): size decides, as before"
    assert holdings.file_held(tmp_path, f, {f.path: {"root": "sha256:new"}})
    assert not holdings.file_held(tmp_path, f, {f.path: {"root": "sha256:old"}}), \
        "promoted under another root: the new version is missing"
    assert not holdings.file_held(tmp_path, MemberFile(f.path, "sha256:new", 11))
