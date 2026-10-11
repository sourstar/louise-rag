"""tests/share/test_coverage.py — the coverage map (02 §2.5; 09 §9.10;
04 §4.5, §4.7): rows per accepted bundle, claims that count for nothing,
the per-shard view with the leader and the window, producers, vitality
from signed work, and `hold vectors show`."""
import json
import sqlite3
import time
from dataclasses import dataclass

import pytest

from app.hold import snapshot as sn
from app.hold.codex import Work
from app.share import coverage as cov, messages, rendezvous
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import pubmed
from tests.share.test_messages import PEER_KEY, a_shard

OTHER_KEY = "ed25519:" + "9" * 64
NOW = 1_800_000_000.0                    # 2027-01-15T08:00:00Z
NOW_TEXT = messages.time_text(NOW)


def mem_con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    return con


@dataclass
class FakeTier:
    tier: str
    verdict: str


@dataclass
class FakeReport:
    bundle_id: str
    producer: str
    tiers: tuple
    admitted: tuple
    ok: bool


def test_period_seconds_reads_iso_durations_with_thirty_day_months():
    assert cov.period_seconds("P1M") == 30 * 86400
    assert cov.period_seconds("P1W") == 7 * 86400
    assert cov.period_seconds("P1Y") == 365 * 86400
    assert cov.period_seconds("P1DT2H3M4S") == 86400 + 7384
    with pytest.raises(ValueError):
        cov.period_seconds("1M")
    with pytest.raises(ValueError):
        cov.period_seconds("P")


def test_a_row_per_bundle_claims_beside_it_and_a_re_import_is_the_same_row():
    con = mem_con()
    codex = pubmed()
    sid = cov.shard_id(codex, "year=2023/00011")
    assert sid == f"{codex.collection_id}/{codex.space_segment}/year=2023/00011"
    report = FakeReport("sha256:" + "2f" * 32, PEER_KEY,
                        (FakeTier("T0", "pass"), FakeTier("T1", "skipped"), FakeTier("T2", "pass"),
                         FakeTier("T3", "pass")), tuple(range(14_800)), True)
    header = {"shard": "year=2023/00011", "uids": ["pubmed:37000021", "pubmed:37010000", "pubmed:37019994"]}
    cov.claim(con, bundle_id=report.bundle_id, shard=sid, key=OTHER_KEY)       # heard before it arrived
    placeholder = cov.row(con, report.bundle_id)
    assert (placeholder["accepted_at"], placeholder["chunks"], placeholder["claimed_by"]) == (None, 0, [OTHER_KEY])
    r = cov.record_from_report(con, codex, report, header, source=cov.FETCHED, now=NOW_TEXT)
    assert r == {"shard": sid, "bundle_id": report.bundle_id, "producer": PEER_KEY,
                 "uids": ["pubmed:37000021", "pubmed:37019994"], "chunks": 14_800,
                 "tiers": {"T0": "pass", "T1": "skipped", "T2": "pass", "T3": "pass"},
                 "accepted_at": NOW_TEXT, "source": "fetched", "verified_on_use": 0,
                 "claimed_by": [OTHER_KEY]}, "09 §9.10's row, the claim kept beside it"
    cov.claim(con, bundle_id=report.bundle_id, shard=sid, key=OTHER_KEY)
    cov.claim(con, bundle_id=report.bundle_id, shard=sid, key=PEER_KEY)
    cov.mark_verified_on_use(con, report.bundle_id, 211)
    again = cov.record_from_report(con, codex, report, header, source=cov.FETCHED, now=NOW_TEXT)
    assert again["claimed_by"] == sorted([OTHER_KEY, PEER_KEY]) and again["verified_on_use"] == 211
    assert len(cov.rows(con)) == 1 and cov.opened_at(con) == {sid: NOW_TEXT}
    refused = FakeReport("sha256:" + "9a" * 32, OTHER_KEY,
                         (FakeTier("T0", "pass"), FakeTier("T1", "fail")), (), False)
    r2 = cov.record_from_report(con, codex, refused, header, source=cov.FETCHED, now=NOW_TEXT)
    assert r2["accepted_at"] is None and r2["chunks"] == 0 and r2["tiers"]["T1"] == "fail"
    assert [x["bundle_id"] for x in cov.rows(con, sid)] == [r2["bundle_id"], r["bundle_id"]]
    with pytest.raises(ValueError):
        cov.record(con, bundle_id="x", shard=sid, producer="k", uids=[], chunks=0, tiers={},
                   source="guessed", accepted_at=None)


def test_fold_claims_reads_the_neighbours_digests_for_this_collection_only():
    con = mem_con()
    codex = pubmed()
    sid = cov.shard_id(codex, "year=2023/00001")
    digests = [
        {"key": PEER_KEY, "collections": [
            {"collection_id": codex.collection_id, "coverage": [{"shard": sid, "bundles": ["sha256:" + "a" * 64]}]},
            {"collection_id": "sha256:" + "f" * 64, "coverage": [{"shard": "x", "bundles": ["sha256:" + "b" * 64]}]}]},
        {"key": OTHER_KEY, "collections": [
            {"collection_id": codex.collection_id, "coverage": [{"shard": sid, "bundles": ["sha256:" + "a" * 64]}]}]},
        {"collections": "garbage"},
    ]
    assert cov.fold_claims(con, codex, digests) == 2
    (r,) = cov.rows(con)
    assert r["bundle_id"] == "sha256:" + "a" * 64 and r["claimed_by"] == sorted([PEER_KEY, OTHER_KEY])
    assert r["accepted_at"] is None, "a claim counts for nothing (02 §2.5)"


def a_snapshot(codex, chunks=(30_000, 30_000)):
    shards = tuple(sn.Shard(slice="year=2023", n=i, root="sha256:" + "11" * 32,
                            records_root="sha256:" + "22" * 32, first_id="a", last_id="z",
                            records=100, chunks=c, chunks_root="sha256:" + "33" * 32,
                            tokens=c * codex.space.ctx, bytes=1)
                   for i, c in enumerate(chunks, 1))
    return sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None,
                       shards=shards, delta=sn.Delta((), (), ()), files={})


def test_the_shard_view_has_covered_open_the_leader_and_the_window():
    con = mem_con()
    codex = pubmed()
    snap = a_snapshot(codex)
    s1 = cov.shard_id(codex, "year=2023/00001")
    s2 = cov.shard_id(codex, "year=2023/00002")
    cov.record(con, bundle_id="sha256:" + "2f" * 32, shard=s1, producer=PEER_KEY,
               uids=["a", "m"], chunks=14_800, tiers={}, source=cov.LOCAL,
               accepted_at=messages.time_text(NOW - 3 * 86400))
    cov.record(con, bundle_id="sha256:" + "9a" * 32, shard=s1, producer=OTHER_KEY,
               uids=["n", "z"], chunks=15_200, tiers={}, source=cov.FETCHED,
               accepted_at=messages.time_text(NOW - 86400))
    cov.record(con, bundle_id="sha256:" + "77" * 32, shard=s2, producer=OTHER_KEY,
               uids=["a", "k"], chunks=10_000, tiers={}, source=cov.FETCHED,
               accepted_at=messages.time_text(NOW - 2 * 86400))
    cov.record(con, bundle_id="sha256:" + "88" * 32, shard=s2, producer=PEER_KEY,
               uids=["k", "q"], chunks=5_000, tiers={}, source=cov.FETCHED, accepted_at=None)
    peers = [rendezvous.Peer(PEER_KEY, 1000.0), rendezvous.Peer(OTHER_KEY, 1000.0)]
    view = cov.shards_view(codex, snap, cov.rows(con), peers=peers, now=NOW)
    assert [(v["shard"], v["chunks"], v["covered"], v["open"]) for v in view] == [
        (s1, 30_000, 30_000, False), (s2, 30_000, 10_000, True)]
    assert view[0]["leader"] is None and view[0]["window_ends"] is None, "a covered shard has no leader"
    lead = rendezvous.leader(rendezvous.ShardWork(s2, 30_000 * codex.space.ctx), peers,
                             cov.period_seconds("P1M"))
    assert view[1]["leader"] == lead.key
    if lead.key == OTHER_KEY:
        assert view[1]["window_ends"] == messages.time_text(NOW - 2 * 86400 + 7 * 86400), \
            "the leader's last accepted bundle plus grace_days"
    else:
        assert view[1]["window_ends"] is None, "no bundle from the leader and no opened_at known"
    assert len(view[1]["bundles"]) == 2 and view[1]["bundles"][0]["accepted_at"] is None
    assert cov.shards_view(codex, snap, cov.rows(con), peers=[], now=NOW)[1]["leader"] is None
    assert cov.shards_view(codex, None, [], now=NOW) == []
    # the window from the moment the shard was first seen open, when the leader has no bundle
    cov.note_open(con, codex, "year=2023/00002", now=messages.time_text(NOW - 5 * 86400))
    view = cov.shards_view(codex, snap, cov.rows(con), peers=[rendezvous.Peer(PEER_KEY, 1000.0)],
                           opened=cov.opened_at(con), now=NOW)
    assert view[1]["leader"] == PEER_KEY and view[1]["window_ends"] == messages.time_text(NOW + 2 * 86400)


def test_vitality_is_any_one_key_s_signed_work_in_the_trailing_period():
    con = mem_con()
    codex = pubmed(work=Work(20_000_000, "P1M"))
    snap = a_snapshot(codex)
    s1 = cov.shard_id(codex, "year=2023/00001")
    ctx = codex.space.ctx
    cov.record(con, bundle_id="sha256:" + "2f" * 32, shard=s1, producer=PEER_KEY, uids=["a", "m"],
               chunks=14_800, tiers={}, source=cov.FETCHED, accepted_at=messages.time_text(NOW - 86400))
    cov.record(con, bundle_id="sha256:" + "9a" * 32, shard=s1, producer=OTHER_KEY, uids=["n", "z"],
               chunks=15_200, tiers={}, source=cov.FETCHED, accepted_at=messages.time_text(NOW - 40 * 86400))
    work = cov.signed_work(codex, snap, cov.rows(con), now=NOW)
    assert work == {PEER_KEY: {"tokens": 14_800 * ctx, "bundles": 1, "attestations": 0}}, \
        "a bundle older than the period counts for nothing"
    assert cov.vitality(codex, snap, cov.rows(con), now=NOW) == cov.DORMANT, "7.6M < 20M"
    roots = {"collection_id": codex.collection_id, "check": "roots", "result": "pass", "key": PEER_KEY,
             "time": messages.time_text(NOW - 3600), "shards": [f"{codex.collection_id}/year=2023/00001",
                                                               f"{codex.collection_id}/year=2023/00002"]}
    sample = {"collection_id": codex.collection_id, "check": "vectors-sample", "result": "pass",
              "key": OTHER_KEY, "time": messages.time_text(NOW - 3600), "detail": {"chunks": 16}}
    failed = {**roots, "result": "fail"}
    other = {**roots, "collection_id": "sha256:" + "f" * 64}
    work = cov.signed_work(codex, snap, cov.rows(con), [roots, sample, failed, other], now=NOW)
    assert work[PEER_KEY] == {"tokens": 14_800 * ctx + 60_000 * ctx, "bundles": 1, "attestations": 1}, \
        "a deterministic check counts the snapshot's tokens of the shards it names"
    assert work[OTHER_KEY] == {"tokens": 16 * ctx, "bundles": 0, "attestations": 1}, \
        "a statistical one counts the chunks it re-embedded"
    assert cov.vitality(codex, snap, cov.rows(con), [roots], now=NOW) == cov.ACTIVE, "38M ≥ 20M"
    whole = {**roots, "shards": []}
    assert cov.signed_work(codex, snap, cov.rows(con), [whole], now=NOW)[PEER_KEY]["tokens"] == \
        14_800 * ctx + codex.work.tokens, "a check naming no shard reproduces the whole: work.tokens"
    got = cov.producers(codex, cov.rows(con), work)
    assert sorted(got, key=lambda p: p["key"]) == sorted([
        {"key": PEER_KEY, "chunks": 14_800, "bundles": 1,
         "tokens_per": {"per": "P1M", "tokens": work[PEER_KEY]["tokens"]}},
        {"key": OTHER_KEY, "chunks": 15_200, "bundles": 1,
         "tokens_per": {"per": "P1M", "tokens": 16 * ctx}}], key=lambda p: p["key"])
    assert [p["key"] for p in got] == sorted([PEER_KEY, OTHER_KEY]), "by key"


def test_hold_vectors_show_prints_the_map(node, capsys):  # noqa: F811
    from app import louise
    codex, name, d, snap = a_shard(node)
    con = cov.manifest(node, name)       # red, unacknowledged: show reads all the same
    sid = cov.shard_id(codex, "year=2023/00001")
    cov.record(con, bundle_id="sha256:" + "2f" * 32, shard=sid, producer=PEER_KEY, uids=["a", "z"],
               chunks=4, tiers={"T0": "pass", "T1": "skipped", "T2": "pass", "T3": "pass"},
               source=cov.FETCHED, accepted_at=messages.time_text(time.time() - 60))
    con.close()
    assert louise.main(["hold", "vectors", "show", name, "--root", str(node)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"collection_id", "space_id", "snapshot_id", "chunks", "covered", "shards",
                        "producers", "vitality"}
    assert (out["snapshot_id"], out["chunks"], out["covered"]) == ("2026-08", 4, 4)
    (shard,) = out["shards"]
    assert (shard["shard"], shard["open"], shard["leader"]) == (sid, False, None)
    assert shard["bundles"][0]["producer"] == PEER_KEY and shard["bundles"][0]["tiers"]["T3"] == "pass"
    assert out["producers"][0]["key"] == PEER_KEY and out["vitality"] == cov.DORMANT, \
        "2048 tokens against pubmed's forty million"


def test_record_exported_writes_local_rows_not_yet_accepted(tmp_path):
    con = mem_con()
    codex = pubmed()
    b = tmp_path / "b"
    b.mkdir()
    (b / "header.json").write_bytes(json.dumps({"uids": ["u1", "u2", "u3"]}).encode())
    exported = {"shard": "year=2023/00001", "bundles": [{"bundle_id": "sha256:" + "ab" * 32,
                                                          "path": str(b), "uids": 3, "chunks": 9}]}
    (r,) = cov.record_exported(con, codex, exported, producer=PEER_KEY, now=NOW_TEXT)
    assert (r["source"], r["accepted_at"], r["uids"], r["chunks"], r["producer"]) == \
        ("local", None, ["u1", "u3"], 0, PEER_KEY)
    cov.accept_local(con, r["bundle_id"], chunks=9, tiers={"T0": "pass", "T1": "skipped", "T2": "pass",
                                                          "T3": "skipped"}, now=NOW_TEXT)
    assert cov.row(con, r["bundle_id"])["accepted_at"] == NOW_TEXT
    assert cov.opened_at(con) == {cov.shard_id(codex, "year=2023/00001"): NOW_TEXT}
