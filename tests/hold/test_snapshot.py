"""tests/hold/test_snapshot.py — snapshots are content-addressed (docs/02 §2.2).

The collection half of this file moved to test_collection.py when the Codex
gained the fields of 09 §9.3; what is left here is the snapshot, which step
2 of the migration brings up to §9.4."""
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from app.hold import codex as cx, collection as col, snapshot as sn
from app.hold.codex import CodexError


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

