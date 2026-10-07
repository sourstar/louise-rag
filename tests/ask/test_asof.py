"""tests/ask/test_asof.py — `as_of` (05 §5.1; 07 §7.3): the date read in
the origin's clock against the cutoff rule, the held snapshot that
answers, the records changed since it dropped and their shards named, a
chain with a gap withheld, a date before the first snapshot withheld, and
the search carrying which snapshot answered."""
from datetime import date
from types import SimpleNamespace

import pytest

from app import query_service
from app.ask import asof, results
from app.hold import collection as col, snapshot as sn
from app.hold.codex import Update
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.share.test_messages import a_drag

NO_DELTA = sn.Delta((), (), ())


def shard(slice_, n, first, last):
    return sn.Shard(slice=slice_, n=n, root="sha256:" + "00" * 32, records_root="sha256:" + "11" * 32,
                    first_id=first, last_id=last, records=5, chunks=10, chunks_root="sha256:" + "22" * 32,
                    tokens=5120, bytes=100)


def chain(codex, d):
    """2026-06 → 2026-07 → 2026-08, two slices; 2026-08 changes pubmed:2,
    adds pubmed:7 and tombstones pubmed:9."""
    shards = (shard("year=2023", 1, "1", "5"), shard("year=2024", 1, "6", "9"))
    s6 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-06", previous=None, shards=shards,
                     delta=NO_DELTA, files={})
    s7 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-07", previous=s6.hash, shards=shards,
                     delta=sn.Delta(("pubmed:4",), (), ()), files={})
    s8 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=s7.hash, shards=shards,
                     delta=sn.Delta(("pubmed:7",), ("pubmed:2",), (sn.Tombstone("pubmed:9", "withdrawn"),)),
                     files={})
    for p in (d / "snapshots").glob("*"):
        p.unlink()
    for s in (s6, s7, s8):
        (d / "snapshots" / f"{s.snapshot_id}.json").write_bytes(s.canonical())
    (d / "records/year=2023").mkdir(parents=True, exist_ok=True)
    (d / "records/year=2023/00001.jsonl.zst").write_bytes(b"held")
    return s6, s7, s8


def test_the_date_is_read_in_the_origin_s_clock_against_the_cutoff(node):  # noqa: F811
    codex, *_ = a_drag(node)                        # pubmed: America/New_York, monthly, day 1
    assert codex.update.cadence == "monthly" and codex.update.day == 1
    assert asof.target_id(codex, date(2026, 9, 15)) == "2026-08"
    assert asof.target_id(codex, date(2026, 9, 1)) == "2026-08", "the cutoff itself is 'at or before'"
    assert asof.target_id(codex, date(2026, 8, 31)) == "2026-07"
    assert asof.target_id(codex, date(2026, 1, 3)) == "2025-12"
    mid = SimpleNamespace(update=Update("monthly", 15, 7), origin=codex.origin)
    assert asof.target_id(mid, date(2026, 9, 10)) == "2026-07", "before the 15th, August has not closed"
    assert asof.target_id(mid, date(2026, 9, 15)) == "2026-08"
    weekly = SimpleNamespace(update=Update("weekly", 2, 7), origin=codex.origin)
    assert asof.target_id(weekly, date(2026, 9, 8)) == "2026-W36", "Tuesday of week 37: week 36 closed"
    assert asof.target_id(weekly, date(2026, 9, 7)) == "2026-W35", "Monday of week 37: not yet"
    for bad in ("2026-9-2", "2026-09", "20260902", 20260902, "2026-13-01"):
        with pytest.raises(asof.AsOfError) as e:
            asof.parse_date(bad)
        assert e.value.exit_code == 2
    assert asof.parse_date("2026-09-02") == date(2026, 9, 2)


def test_the_held_snapshot_that_answers_and_what_it_cannot(node):  # noqa: F811
    codex, name, d, _ = a_drag(node)
    s6, s7, s8 = chain(codex, d)
    r = asof.resolve(codex, d, date(2026, 9, 2))
    assert r.snapshot.hash == s8.hash and r.same_as_current and r.changed == set() and r.shards == []
    r = asof.resolve(codex, d, date(2026, 8, 20))
    assert r.snapshot.hash == s7.hash and not r.same_as_current
    assert r.changed == {"pubmed:2", "pubmed:7", "pubmed:9"}, "added, changed and tombstoned after 2026-07"
    assert r.shards == ["year=2023/1", "year=2024/1"] and r.held == 1
    entry = asof.withheld_entry(SimpleNamespace(name=name), codex, r, date(2026, 8, 20))
    assert entry["shards"] == r.shards and entry["collection_id"] == codex.collection_id
    assert "3 records of snapshot 2026-07" in entry["reason"] and "1 held here" in entry["reason"]
    r = asof.resolve(codex, d, date(2026, 7, 10))
    assert r.snapshot.hash == s6.hash and r.changed == {"pubmed:4", "pubmed:2", "pubmed:7", "pubmed:9"}
    r = asof.resolve(codex, d, date(2026, 6, 30))
    assert r.snapshot is None and "the first held is 2026-06" in r.reason
    assert asof.withheld_entry(SimpleNamespace(name=name), codex, r, date(2026, 6, 30))["shards"] == "all"


def test_a_gap_in_the_chain_withholds_and_too_much_change_is_refused(node, monkeypatch):  # noqa: F811
    codex, name, d, _ = a_drag(node)
    chain(codex, d)
    monkeypatch.setattr(asof, "MAX_CHANGED", 2)
    with pytest.raises(asof.TooFarBack) as e:
        asof.resolve(codex, d, date(2026, 8, 20))
    assert e.value.exit_code == 3
    monkeypatch.setattr(asof, "MAX_CHANGED", 500_000)
    (d / "snapshots" / "2026-07.json").unlink()
    r = asof.resolve(codex, d, date(2026, 7, 10))
    assert r.snapshot is None and "not held whole" in r.reason and "2026-08 does not follow 2026-06" in r.reason
    assert asof.resolve(codex, d, date(2026, 8, 20)).snapshot is None, "2026-07 asked for, not held: nothing later answers"


def test_the_search_answers_from_the_snapshot_asked_for_and_names_the_rest(node, monkeypatch):  # noqa: F811
    codex, name, d, _ = a_drag(node)
    chain(codex, d)
    red = SimpleNamespace(name=name, plugin="pubmed", codex=codex)
    legacy = SimpleNamespace(name="gutenberg", plugin="gutenberg", codex=None)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [red, legacy])
    monkeypatch.setattr(results, "_layer", lambda c, h: "abstract")
    seen = {}

    def run(names, query, k, mode, translate, rerank_depth=50, all_languages=False, filters=None):
        seen["names"] = names
        return {"hits": [
            {"chunk_id": "pubmed:1:0:1", "book_id": 1, "source_collection": name, "leg_ranks": {"text": 1}},
            {"chunk_id": "pubmed:2:0:1", "book_id": 2, "source_collection": name, "leg_ranks": {"text": 2}},
            {"chunk_id": "pubmed:7:0:1", "book_id": 7, "source_collection": name, "leg_ranks": {"vector": 1}},
        ], "reranked": False, "timings": {"total": 1}}
    out = results.search("q", as_of="2026-08-20", root=node, run=run)
    assert seen["names"] == [name], "a collection without a Codex has no dated state: not searched as of"
    (lst,) = out["lists"]
    assert [h["uid"] for h in lst["hits"]] == ["pubmed:1"], "records changed after 2026-07 are not answered"
    assert lst["hits"][0]["snapshot_id"] == "2026-07" and lst["hits"][0]["rank"] == 1
    assert out["as_of"] == {"date": "2026-08-20", "snapshots": {name: "2026-07"}, "excluded": {name: 2}}
    by = {w["collection"]: w for w in out["withheld"]}
    assert by["gutenberg"]["shards"] == "all" and "no dated snapshots" in by["gutenberg"]["reason"]
    assert by[name]["shards"] == ["year=2023/1", "year=2024/1"]

    out = results.search("q", as_of="2026-09-02", root=node, run=run)
    assert [h["uid"] for h in out["lists"][0]["hits"]] == ["pubmed:1", "pubmed:2", "pubmed:7"]
    assert out["as_of"]["snapshots"] == {name: "2026-08"} and out["as_of"]["excluded"] == {}

    out = results.search("q", as_of="2026-06-30", root=node, run=run)
    assert out["lists"] == [] and {w["collection"] for w in out["withheld"]} == {name, "gutenberg"}
    with pytest.raises(asof.AsOfError):
        results.search("q", as_of="2026-6-30", root=node, run=run)
