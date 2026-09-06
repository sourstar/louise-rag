"""tests/hold/test_snapshot_and_collection.py — snapshots are content-addressed
(docs/02 §2.2); a collection on disk is a directory named by its id (§7.3)."""
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from app.hold import codex as cx, collection as col, snapshot as sn
from app.hold.codex import CodexError, Defect, Layer, Space, Toolchain
from tests.hold.test_codex import root_codex, IMG, IMG2, ENC, ENC2


# ---- snapshot ids from the clock ------------------------------------------

def test_snapshot_id_names_the_month_that_closed():
    rome = timezone(timedelta(hours=2))
    assert sn.snapshot_id_for(datetime(2026, 9, 1, 0, 0, tzinfo=rome)) == "2026-08"
    assert sn.snapshot_id_for(datetime(2026, 9, 30, 23, 59, tzinfo=rome)) == "2026-08"
    assert sn.snapshot_id_for(datetime(2026, 10, 1, 0, 0, tzinfo=rome)) == "2026-09"
    assert sn.snapshot_id_for(datetime(2027, 1, 15, tzinfo=timezone.utc)) == "2026-12"


def test_snapshot_id_requires_the_origins_clock():
    with pytest.raises(CodexError, match="timezone-aware"):
        sn.snapshot_id_for(datetime(2026, 9, 1))


def test_next_snapshot_id_rolls_the_year():
    assert sn.next_snapshot_id("2026-11") == "2026-12"
    assert sn.next_snapshot_id("2026-12") == "2027-01"


# ---- content addressing ------------------------------------------------------

def shards():
    return (sn.Shard("year=1975-1979", 17, "sha256:" + "1" * 64, "pg:100", "pg:200", 100, 900),
            sn.Shard("year=1975-1979", 18, "sha256:" + "2" * 64, "pg:201", "pg:300", 100, 950),
            sn.Shard("year=1980", 1, "sha256:" + "3" * 64, "pg:301", "pg:400", 100, 800))


def test_same_content_same_hash_regardless_of_order():
    a = sn.Snapshot("sha256:c", "2026-09", None, shards(), sn.compute_delta(None, {"x": "h"}))
    b = sn.Snapshot("sha256:c", "2026-09", None, tuple(reversed(shards())), sn.compute_delta(None, {"x": "h"}))
    assert a.hash == b.hash
    assert a.members("sha256:" + "f" * 64) == ["records-year=1975-1979", "records-year=1980",
                                              "vectors-ffffffffffff-year=1975-1979", "vectors-ffffffffffff-year=1980"]


def test_any_shard_root_changes_the_hash():
    a = sn.Snapshot("sha256:c", "2026-09", None, shards(), sn.Delta((), (), ()))
    changed = shards()[:2] + (sn.Shard("year=1980", 1, "sha256:" + "4" * 64, "pg:301", "pg:400", 100, 800),)
    assert sn.Snapshot("sha256:c", "2026-09", None, changed, sn.Delta((), (), ())).hash != a.hash


def test_delta_is_by_content_hash_only():
    prev = {"a": "h1", "b": "h2", "c": "h3"}
    cur = {"a": "h1", "b": "h2x", "d": "h4"}
    d = sn.compute_delta(prev, cur)
    assert d == sn.Delta(added=("d",), changed=("b",), tombstoned=("c",))
    assert sn.compute_delta(None, cur).added == ("a", "b", "d")


def test_chain_verifies_previous_hash_and_consecutive_months():
    s1 = sn.Snapshot("sha256:c", "2026-09", None, shards(), sn.Delta((), (), ()))
    s2 = sn.Snapshot("sha256:c", "2026-10", s1.hash, shards(), sn.Delta((), (), ()))
    sn.verify_chain([s1, s2])
    with pytest.raises(CodexError, match="previous does not match"):
        sn.verify_chain([s1, sn.Snapshot("sha256:c", "2026-10", "sha256:nope", shards(), sn.Delta((), (), ()))])
    with pytest.raises(CodexError, match="not the month after"):
        sn.verify_chain([s1, sn.Snapshot("sha256:c", "2026-12", s1.hash, shards(), sn.Delta((), (), ()))])
    with pytest.raises(CodexError, match="first snapshot names"):
        sn.verify_chain([s2])


# ---- collections on disk -----------------------------------------------------

def test_create_writes_canonical_bytes_under_its_own_id(tmp_path: Path):
    c = col.create(tmp_path, root_codex())
    d = tmp_path / c.collection_id.replace("sha256:", "")
    assert (d / "codex.json").read_bytes() == c.canonical()
    assert col.read(tmp_path, c.collection_id) == c
    assert col.read(tmp_path, c.collection_id.replace("sha256:", "")) == c     # bare hex accepted
    col.create(tmp_path, root_codex())                                          # idempotent
    assert [x.collection_id for x in col.list_all(tmp_path)] == [c.collection_id]


def test_read_refuses_a_directory_whose_bytes_do_not_hash_to_its_name(tmp_path: Path):
    c = col.create(tmp_path, root_codex())
    d = tmp_path / c.collection_id.replace("sha256:", "")
    other = root_codex(parser_version=99)
    (d / "codex.json").write_bytes(other.canonical())
    with pytest.raises(CodexError, match="bytes hash to"):
        col.read(tmp_path, c.collection_id)


def test_create_refuses_lineage_fields():
    with pytest.raises(CodexError, match="create makes a root"):
        col.create(Path("/nonexistent"), root_codex(derived_from="sha256:" + "1" * 64))


def test_derive_makes_a_sister_and_refuses_no_change(tmp_path: Path):
    parent = col.create(tmp_path, root_codex())
    sister = col.derive(tmp_path, parent.collection_id, encoder_image=ENC2)
    assert sister.relation == "sister" and sister.derived_from == parent.collection_id
    assert sister.space_id != parent.space_id and sister.origin == parent.origin
    with pytest.raises(CodexError, match="nothing about the space changed"):
        col.derive(tmp_path, parent.collection_id)
    shown = col.show(tmp_path, sister.collection_id)
    assert shown["relation"] == "sister" and shown["lineage_id"] == sister.collection_id  # a sister starts a lineage


def test_continue_makes_a_daughter_in_the_same_space_with_a_lineage(tmp_path: Path):
    parent = col.create(tmp_path, root_codex())
    d1 = col.continue_(tmp_path, parent.collection_id, platform_image=IMG2, from_snapshot="2026-10",
                       origin={"transport": "https", "url": "https://gutenberg-moved.example/"},
                       origin_reachable_until="2026-09")
    assert d1.relation == "daughter" and d1.space_id == parent.space_id
    assert d1.toolchain.encoder_image == parent.toolchain.encoder_image
    d2 = col.continue_(tmp_path, d1.collection_id, platform_image="sha256:" + "7" * 64, from_snapshot="2027-02")
    assert col.show(tmp_path, d2.collection_id)["lineage_id"] == parent.collection_id
    assert col.show(tmp_path, parent.collection_id)["lineage_id"] == parent.collection_id


def test_replace_makes_a_corrective_sister_with_fresh_space(tmp_path: Path):
    parent = col.create(tmp_path, root_codex())
    defect = Defect("records-vs-origin", IMG2, ("year=1975-1979/00017",), "sha256:aa", "sha256:bb",
                    "footnotes were dropped")
    fixed = col.replace(tmp_path, parent.collection_id, platform_image=IMG2, encoder_image=None, defect=defect)
    assert fixed.relation == "corrective-sister" and fixed.replaces == parent.collection_id
    assert fixed.toolchain.encoder_image == parent.toolchain.encoder_image
    assert fixed.layers == parent.layers and fixed.cleaning_version == parent.cleaning_version
    assert fixed.space_id != parent.space_id, "identical encoder and layers, still a new space"
    assert col.read(tmp_path, fixed.collection_id).defect == defect
