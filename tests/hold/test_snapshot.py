"""tests/hold/test_snapshot.py — snapshots are content-addressed.

docs/02-collections.md §2.2 says why; docs/09-formats.md §9.4 fixes the
bytes. A snapshot's id is computable from the Codex and a clock; its hash
is computable from its content, so two honest peers that build the same
period produce the same hash.
"""
from datetime import datetime, timezone, timedelta

import pytest

from app.hold import snapshot as sn
from app.hold.codex import CodexError


# ---- ids: the period that CLOSED at the cutoff ----------------------------

def test_a_monthly_id_names_the_month_that_closed():
    rome = timezone(timedelta(hours=2))
    assert sn.snapshot_id_for(datetime(2026, 9, 1, 0, 0, tzinfo=rome)) == "2026-08"
    assert sn.snapshot_id_for(datetime(2026, 9, 30, 23, 59, tzinfo=rome)) == "2026-08"
    assert sn.snapshot_id_for(datetime(2026, 10, 1, 0, 0, tzinfo=rome)) == "2026-09"
    assert sn.snapshot_id_for(datetime(2027, 1, 15, tzinfo=timezone.utc)) == "2026-12"


def test_a_weekly_id_names_the_iso_week_that_closed():
    """09 §9.4's worked example: the cutoff 2026-09-07T00:00 is ISO weekday 1
    of week 37, and names the week that closed — 2026-W36."""
    utc = timezone.utc
    assert sn.snapshot_id_for(datetime(2026, 9, 7, 0, 0, tzinfo=utc), "weekly") == "2026-W36"
    assert sn.snapshot_id_for(datetime(2026, 9, 1, 0, 0, tzinfo=utc), "weekly") == "2026-W35"


def test_a_week_id_is_zero_padded_so_it_sorts():
    utc = timezone.utc
    assert sn.snapshot_id_for(datetime(2026, 3, 5, tzinfo=utc), "weekly") == "2026-W09"


def test_snapshot_id_requires_the_origins_clock():
    with pytest.raises(CodexError, match="timezone-aware"):
        sn.snapshot_id_for(datetime(2026, 9, 1))


def test_only_the_two_cadences_have_an_id_form():
    with pytest.raises(CodexError, match="monthly|weekly"):
        sn.snapshot_id_for(datetime(2026, 9, 1, tzinfo=timezone.utc), "daily")


def test_next_id_rolls_the_year_and_the_iso_year():
    assert sn.next_snapshot_id("2026-11") == "2026-12"
    assert sn.next_snapshot_id("2026-12") == "2027-01"
    assert sn.next_snapshot_id("2026-W36") == "2026-W37"
    # 2026 is a 53-week ISO year; the roll is computed through a real date
    # rather than guessed, so W53 exists and W54 does not.
    assert sn.next_snapshot_id("2026-W52") == "2026-W53"
    assert sn.next_snapshot_id("2026-W53") == "2027-W01"


def test_the_cadence_is_read_off_the_id():
    assert sn.cadence_of("2026-08") == "monthly"
    assert sn.cadence_of("2026-W36") == "weekly"


# ---- content addressing ---------------------------------------------------

def shard(slice_="year=1975-1979", n=17, root="1", first="100", last="200",
          chunks=900, **over):
    d = dict(slice=slice_, n=n, root="sha256:" + root * 64,
             records_root="sha256:" + "a" * 64, first_id=first, last_id=last,
             records=100, chunks=chunks, chunks_root="sha256:" + "b" * 64,
             tokens=chunks * 512, bytes=40_000)
    d.update(over)
    return sn.Shard(**d)


def shards():
    return (shard(n=17, root="1", first="100", last="200"),
            shard(n=18, root="2", first="201", last="300"),
            shard("year=1980", 1, "3", "301", "400"))


def snap(**over):
    d = dict(collection_id="sha256:" + "c" * 64, snapshot_id="2026-09",
             previous=None, shards=shards(), delta=sn.Delta((), (), ()))
    d.update(over)
    return sn.Snapshot(**d)


def test_same_content_same_hash_regardless_of_order():
    a = snap()
    b = snap(shards=tuple(reversed(shards())))
    assert a.hash == b.hash


def test_shards_sort_by_slice_bytes_then_number():
    """09 §9.4: sorted by (`slice` bytes, `n`)."""
    listed = [(s["slice"], s["n"]) for s in snap().to_dict()["shards"]]
    assert listed == [("year=1975-1979", 17), ("year=1975-1979", 18),
                      ("year=1980", 1)]


def test_every_shard_field_of_9_4_is_present():
    entry = snap().to_dict()["shards"][0]
    assert set(entry) == {"slice", "n", "root", "records_root", "first_id",
                          "last_id", "records", "chunks", "chunks_root",
                          "tokens", "bytes", "toolchain"}
    assert entry["toolchain"] == "own"


def test_any_root_changes_the_hash():
    base = snap().hash
    for changed in (shard(root="4"), shard(records_root="sha256:" + "9" * 64),
                    shard(chunks_root="sha256:" + "9" * 64)):
        assert snap(shards=(changed,) + shards()[1:]).hash != base


def test_chunks_root_is_in_the_snapshot_because_a_count_is_not_enough():
    """The reason it exists: a chunker that moved every boundary without
    changing the number would pass a count comparison (09 §9.5)."""
    same_count = shard(chunks_root="sha256:" + "d" * 64)
    assert same_count.chunks == shards()[0].chunks
    assert snap(shards=(same_count,) + shards()[1:]).hash != snap().hash


def test_the_snapshot_carries_relations_and_files():
    s = snap(relations=(sn.RelationShard("year=1980", 1, "sha256:" + "e" * 64,
                                         42, 900),),
             files={"validation.json": {"root": "sha256:" + "f" * 64,
                                        "bytes": 300}})
    d = s.to_dict()
    assert d["relations"][0] == {"slice": "year=1980", "n": 1,
                                 "root": "sha256:" + "e" * 64,
                                 "edges": 42, "bytes": 900}
    assert d["files"]["validation.json"]["bytes"] == 300
    assert snap().to_dict()["relations"] == []


def test_the_totals_are_derived_and_not_stored():
    s = snap()
    assert s.records == 300
    assert s.chunks == 2700
    assert s.tokens == 2700 * 512
    assert s.slices == ["year=1975-1979", "year=1980"]


def test_members_are_slice_by_kind():
    s = snap(relations=(sn.RelationShard("year=1980", 1, "sha256:e", 1, 1),))
    assert s.members() == ["records-year=1975-1979", "records-year=1980",
                           "relations-year=1980"]
    assert "vectors-03ae64488c899305-year=1980" in s.members("03ae64488c899305")


# ---- the delta ------------------------------------------------------------

def test_delta_is_by_content_hash_only():
    prev = {"a": "h1", "b": "h2", "c": "h3"}
    cur = {"a": "h1", "b": "h2x", "d": "h4"}
    d = sn.compute_delta(prev, cur)
    assert d.added == ("d",)
    assert d.changed == ("b",)
    assert d.tombstoned == (sn.Tombstone("c", "withdrawn"),)
    assert sn.compute_delta(None, cur).added == ("a", "b", "d")


def test_a_tombstone_carries_a_reason_and_withdrawn_is_the_honest_default():
    """09 §9.4: `retracted` — the origin says the record should not have
    existed; `withdrawn` — the origin no longer supplies it. Only the origin
    can say the first, so absence alone means the second."""
    d = sn.compute_delta({"a": "h", "b": "h"}, {}, reasons={"a": "retracted"})
    assert [t.to_dict() for t in d.tombstoned] == [
        {"uid": "a", "reason": "retracted"},
        {"uid": "b", "reason": "withdrawn"}]


def test_an_unknown_tombstone_reason_is_refused():
    with pytest.raises(CodexError, match="must be retracted or withdrawn"):
        sn.compute_delta({"a": "h"}, {}, reasons={"a": "deleted"})


def test_tombstones_are_sorted_by_uid_in_the_bytes():
    d = sn.Delta((), (), (sn.Tombstone("z", "withdrawn"),
                          sn.Tombstone("a", "retracted")))
    assert [t["uid"] for t in d.to_dict()["tombstoned"]] == ["a", "z"]


# ---- the chain ------------------------------------------------------------

def test_chain_verifies_previous_hash_and_consecutive_periods():
    s1 = snap()
    s2 = snap(snapshot_id="2026-10", previous=s1.hash)
    sn.verify_chain([s1, s2])
    with pytest.raises(CodexError, match="previous does not match"):
        sn.verify_chain([s1, snap(snapshot_id="2026-10", previous="sha256:nope")])
    with pytest.raises(CodexError, match="not the period after"):
        sn.verify_chain([s1, snap(snapshot_id="2026-12", previous=s1.hash)])
    with pytest.raises(CodexError, match="first snapshot names"):
        sn.verify_chain([s2])


def test_a_weekly_chain_verifies_the_same_way():
    s1 = snap(snapshot_id="2026-W36")
    s2 = snap(snapshot_id="2026-W37", previous=s1.hash)
    sn.verify_chain([s1, s2])
    with pytest.raises(CodexError, match="not the period after"):
        sn.verify_chain([s1, snap(snapshot_id="2026-W39", previous=s1.hash)])


def test_the_cadence_may_not_change_mid_chain():
    """A chain is one cadence: only a sister may change it, and she begins
    her own chain (09 §9.3)."""
    s1 = snap()
    with pytest.raises(CodexError, match="cadence changes mid-chain"):
        sn.verify_chain([s1, snap(snapshot_id="2026-W40", previous=s1.hash)])


def test_a_collection_may_not_change_mid_chain():
    s1 = snap()
    with pytest.raises(CodexError, match="collection changes mid-chain"):
        sn.verify_chain([s1, snap(snapshot_id="2026-10", previous=s1.hash,
                                  collection_id="sha256:" + "d" * 64)])


# ---- what was re-issued ---------------------------------------------------

def test_reissued_is_derived_from_the_roots_and_listed_nowhere():
    """09 §9.4: "The set of shards re-issued by a snapshot is the set whose
    `root` differs from the same `(slice, n)` in `previous`; nothing lists it
    separately"."""
    before = snap()
    after = snap(snapshot_id="2026-10", previous=before.hash,
                 shards=(shard(n=17, root="9", first="100", last="200"),)
                        + shards()[1:])
    assert [(s.slice, s.n) for s in sn.reissued(before, after)] == \
        [("year=1975-1979", 17)]
    assert sn.reissued(before, before) == []
    assert len(sn.reissued(None, before)) == 3, "the first snapshot issues all"


def test_a_new_shard_number_is_not_a_re_issue():
    before = snap()
    after = snap(shards=shards() + (shard("year=1980", 2, "7", "401", "500"),))
    assert sn.reissued(before, after) == []


# ---- resolving a uid to a shard -------------------------------------------

def test_a_uid_resolves_to_the_shard_whose_range_contains_it():
    """05 §5.5, and the reason `hold records build` refuses an out-of-order
    native_id: the ranges have to partition the id space."""
    s = snap()
    assert s.shard_of("150").n == 17
    assert s.shard_of("250").n == 18
    assert s.shard_of("350").slice == "year=1980"
    assert s.shard_of("999") is None


def test_a_snapshot_already_written_keeps_its_own_version(tmp_path):
    """09 §9.4: "snapshots already written stay 1, and a chain links by
    `previous` hash rather than by version, so nothing is re-issued".

    A snapshot is CONTENT-ADDRESSED — its hash is over `canonical()` — so
    re-serialising a loaded v1 document at the current version gives it a
    hash its own file does not have. That invalidates every signature over
    it and breaks every `previous` link to it.

    Caught on pubmed the day `louise-snapshot` moved to 2: the sidecar
    covered sha256:3bfe1d1d… — the file's real bytes, version 1, no
    `figures` key — and the loaded object hashed to sha256:47df84a7…. The
    `signature` check was right; the loader was wrong.
    """
    import json
    v2 = sn.Snapshot(
        collection_id="sha256:" + "ab" * 32, snapshot_id="2026-08",
        previous=None, shards=(), delta=sn.Delta((), (), ()), files={})
    assert v2.version == sn.SNAPSHOT_VERSION == 2
    assert "figures" in json.loads(v2.canonical())

    # the same object as a version 1 document: no `figures` key at all
    doc = json.loads(v2.canonical())
    doc["version"] = 1
    del doc["figures"]
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    v1 = sn._load(raw)
    assert v1.version == 1
    assert "figures" not in json.loads(v1.canonical())
    assert v1.canonical() == raw, "the bytes it was written with, exactly"
    assert v1.hash != v2.hash, "and they are two different snapshots"


def test_a_version_1_snapshot_may_not_carry_figures():
    """There is no third shape: v1 omits the key, v2 carries the list."""
    from app.hold.codex import CodexError
    bad = sn.Snapshot(
        collection_id="sha256:" + "ab" * 32, snapshot_id="2026-08",
        previous=None, shards=(), delta=sn.Delta((), (), ()), files={},
        figures=(sn.FiguresShard(slice="lang=en", n=1, root="sha256:x",
                                 figures=1, bytes=10),),
        version=1)
    with pytest.raises(CodexError, match="version 1 snapshot carries no"):
        bad.to_dict()
