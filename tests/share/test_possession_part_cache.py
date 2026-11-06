"""Two writers filling the same missing part-uids entry at once must both
succeed (Perugia, 2026-10-25): with one shared `.json.tmp`, the writer that
renamed second found nothing, `possession.covered` raised, and
`counting.view_kwargs` fell back to the row count — 114,154 of 10.47 M
chunks covered for that call."""
from pathlib import Path
from types import SimpleNamespace

from app.hold import land
from app.share import possession

SHARD = SimpleNamespace(root="sha256:" + "ab" * 32, slice="lang=en", n=7)
RECORDS = [{"uid": "u1", "content_hash": "h1"}, {"uid": "u2", "content_hash": "h2"}]


def test_a_concurrent_writer_between_write_and_rename_does_not_break_the_other(tmp_path, monkeypatch):
    monkeypatch.setattr(land, "records_for", lambda *a, **k: RECORDS)
    real_replace = possession.os.replace
    nested = {"done": False}

    def replace(src, dst):
        # the other process fills and renames the same entry first
        if not nested["done"]:
            nested["done"] = True
            assert possession.part_records(tmp_path, "g", None, SHARD) == [("u1", "h1"), ("u2", "h2")]
        real_replace(src, dst)

    monkeypatch.setattr(possession.os, "replace", replace)
    got = possession.part_records(tmp_path, "g", None, SHARD)
    assert got == [("u1", "h1"), ("u2", "h2")]
    d = tmp_path / "g" / "state" / "share" / possession.PARTS
    assert [p.name for p in d.iterdir()] == ["ab" * 32 + ".json"]      # no temp file left behind


def test_a_cached_entry_is_read_not_recomputed(tmp_path, monkeypatch):
    monkeypatch.setattr(land, "records_for", lambda *a, **k: RECORDS)
    possession.part_records(tmp_path, "g", None, SHARD)
    monkeypatch.setattr(land, "records_for", lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-read")))
    assert possession.part_records(tmp_path, "g", None, SHARD) == [("u1", "h1"), ("u2", "h2")]
