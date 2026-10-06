"""tests/share/test_levels.py — trust levels (03 §3.6): counted for the
snapshot named and two cadences, independent keys and seeds, the trust
list as a filter, checked and verified by the thresholds, flagged by one
deterministic failure under an image that counts or two statistical ones
on the same chunks."""
import time

from app.hold import snapshot as sn
from app.share import levels, messages
from tests.hold.conftest import pubmed
from tests.share.test_coverage import a_snapshot

NOW = 1_800_000_000.0
IMG = "sha256:" + "1f" * 32            # pubmed()'s platform image


def key(i: int) -> str:
    return "ed25519:" + f"{i:02x}" * 32


def att(codex, snap, check, *, k, result="pass", seed=None, shards=(), detail=None, image=IMG,
        time_text=None, snapshot_hash=None):
    return {"collection_id": codex.collection_id, "snapshot_id": snap.snapshot_id,
            "snapshot_hash": snapshot_hash or snap.hash, "check": check, "seed": seed,
            "shards": list(shards), "result": result, "detail": detail or {},
            "platform_image": image, "key": key(k), "time": time_text or messages.time_text(NOW - 3600)}


def shard_ids(codex, snap):
    return [f"{codex.collection_id}/{s.slice}/{s.n:05d}" for s in snap.shards]


def test_only_this_snapshot_within_two_cadences_from_keys_not_shut_out_counts():
    codex = pubmed()
    snap = a_snapshot(codex)
    a = att(codex, snap, "roots", k=1)
    old = att(codex, snap, "roots", k=2, time_text=messages.time_text(NOW - 61 * 86400))
    other = att(codex, snap, "roots", k=3, snapshot_hash="sha256:" + "9" * 64)
    denied = att(codex, snap, "roots", k=4)
    future = att(codex, snap, "roots", k=5, time_text=messages.time_text(NOW + 7200))
    inconclusive = {**att(codex, snap, "roots", k=6), "result": "inconclusive"}
    got = levels.counting(codex, snap, [a, old, other, denied, future, inconclusive], now=NOW,
                          trust={"deny": {key(4)}})
    assert got == [a], "two cadences (sixty days), this snapshot, pass or fail, not denied"
    assert levels.counting(codex, None, [a], now=NOW) == []


def test_independence_is_distinct_keys_and_distinct_seeds_for_a_sampled_check():
    codex = pubmed()
    snap = a_snapshot(codex)
    twice = [att(codex, snap, "roots", k=1), att(codex, snap, "roots", k=1)]
    assert len(levels.independent(twice, "roots")) == 1, "a key is counted once"
    same_seed = [att(codex, snap, "vectors-sample", k=1, seed="aa" * 32),
                 att(codex, snap, "vectors-sample", k=2, seed="aa" * 32),
                 att(codex, snap, "vectors-sample", k=3, seed="bb" * 32)]
    assert [b["key"] for b in levels.independent(same_seed, "vectors-sample")] == [key(1), key(3)]
    null_seeds = [att(codex, snap, "roots", k=1), att(codex, snap, "roots", k=2)]
    assert len(levels.independent(null_seeds, "roots")) == 2, "a null seed is not compared"


def test_unverified_then_checked_then_verified_by_the_thresholds():
    codex = pubmed()
    snap = a_snapshot(codex, chunks=tuple([30_000] * 8))
    ids = shard_ids(codex, snap)
    bodies = []
    out = levels.level(codex, snap, bodies, now=NOW)
    assert out["level"] == levels.UNVERIFIED and len(out["why"]) == 4
    for k in (1, 2, 3):
        for check in ("roots", "chunks", "calibration"):
            bodies.append(att(codex, snap, check, k=k, seed=f"{k:02x}" * 32 if check == "calibration" else None))
    out = levels.level(codex, snap, bodies, now=NOW)
    assert out["level"] == levels.UNVERIFIED and out["why"] == ["records-vs-origin: no key has passed it on a sample"]
    bodies.append(att(codex, snap, "records-vs-origin", k=1, seed="01" * 32, shards=ids[:1]))
    out = levels.level(codex, snap, bodies, now=NOW)
    assert out["level"] == levels.CHECKED and out["keys"] == 3
    assert out["passed"] == {"roots": 3, "chunks": 3, "calibration": 3, "records-vs-origin": 1, "vectors-sample": 0}
    assert out["coverage"] == {"records-vs-origin": 0.125, "vectors-sample": 0.0}
    # a producer whose vectors nobody else checked holds `checked` back
    rows = [{"producer": key(9), "accepted_at": "2026-10-01T00:00:00Z"}]
    out = levels.level(codex, snap, bodies, rows, now=NOW)
    assert out["level"] == levels.UNVERIFIED and "nobody but" in out["why"][0]
    bodies.append(att(codex, snap, "vectors-sample", k=9, seed="09" * 32, shards=ids[:1],
                      detail={"producers": [key(9)]}))
    assert levels.level(codex, snap, bodies, rows, now=NOW)["level"] == levels.UNVERIFIED, "its own word"
    bodies.append(att(codex, snap, "vectors-sample", k=2, seed="12" * 32, shards=ids[:1],
                      detail={"producers": [key(9)]}))
    assert levels.level(codex, snap, bodies, rows, now=NOW)["level"] == levels.CHECKED
    # verified: eight keys, a quarter of shards sampled against the origin, a tenth re-embedded
    for k in range(4, 9):
        for check in ("roots", "chunks"):
            bodies.append(att(codex, snap, check, k=k))
    out = levels.level(codex, snap, bodies, rows, now=NOW)
    assert out["level"] == levels.CHECKED and out["keys"] == 9 and any("quarter" in w for w in out["why"])
    bodies.append(att(codex, snap, "records-vs-origin", k=4, seed="04" * 32, shards=ids[1:2]))
    out = levels.level(codex, snap, bodies, rows, now=NOW)
    assert out["level"] == levels.VERIFIED and out["coverage"]["records-vs-origin"] == 0.25
    assert out["why"] == []


def test_a_deterministic_failure_flags_when_ours_or_seconded_and_is_a_claim_otherwise():
    codex = pubmed()
    snap = a_snapshot(codex)
    ids = shard_ids(codex, snap)
    bodies = [att(codex, snap, "roots", k=k) for k in (1, 2, 3)]
    other_image = att(codex, snap, "chunks", k=4, result="fail", shards=ids[:1], image="sha256:" + "ee" * 32)
    out = levels.level(codex, snap, bodies + [other_image], now=NOW)
    assert out["level"] != levels.FLAGGED and out["claims"] == [], \
        "a failure under an image that does not count is nothing (03 §3.4)"
    out = levels.level(codex, snap, bodies + [other_image], now=NOW,
                       reference_images={IMG, "sha256:" + "ee" * 32})
    assert out["level"] == levels.UNVERIFIED and out["claims"] == [
        {"check": "chunks", "shard": ids[0], "key": key(4), "time": other_image["time"]}], \
        "a stranger's lone failure is a claim until reproduced here or seconded"
    seconded = att(codex, snap, "chunks", k=6, result="fail", shards=ids[:1])
    out = levels.level(codex, snap, bodies + [other_image, seconded], now=NOW,
                       reference_images={IMG, "sha256:" + "ee" * 32})
    assert out["level"] == levels.FLAGGED and "two keys on the same shard" in out["why"][0]
    elsewhere = att(codex, snap, "chunks", k=7, result="fail", shards=ids[1:2])
    assert levels.level(codex, snap, bodies + [seconded, elsewhere], now=NOW)["level"] == levels.UNVERIFIED, \
        "two strangers on two shards: two claims"
    ours = att(codex, snap, "records-vs-origin", k=5, result="fail", shards=ids[1:2])
    out = levels.level(codex, snap, bodies + [ours], now=NOW, own_key=key(5))
    assert out["level"] == levels.FLAGGED and out["why"][0].startswith("this peer's own records-vs-origin failed")
    assert levels.level(codex, snap, bodies + [ours], now=NOW, own_key=key(1))["level"] == levels.UNVERIFIED


def test_flagged_by_two_statistical_failures_on_the_same_chunks_from_independent_verifiers():
    codex = pubmed()
    snap = a_snapshot(codex)
    first = att(codex, snap, "vectors-sample", k=1, result="fail", seed="aa" * 32,
                detail={"failed": ["c1", "c2", "c3"]})
    assert levels.level(codex, snap, [first], now=NOW)["level"] == levels.UNVERIFIED, "one failure is one word"
    same_key = att(codex, snap, "vectors-sample", k=1, result="fail", seed="bb" * 32, detail={"failed": ["c1"]})
    assert levels.level(codex, snap, [first, same_key], now=NOW)["level"] == levels.UNVERIFIED
    elsewhere = att(codex, snap, "vectors-sample", k=2, result="fail", seed="cc" * 32, detail={"failed": ["c9"]})
    assert levels.level(codex, snap, [first, elsewhere], now=NOW)["level"] == levels.UNVERIFIED, "another sample"
    second = att(codex, snap, "vectors-sample", k=2, result="fail", seed="dd" * 32, detail={"failed": ["c2", "c3"]})
    out = levels.level(codex, snap, [first, second], now=NOW)
    assert out["level"] == levels.FLAGGED and "failed twice on the same chunks" in out["why"][0]
    out = levels.level(codex, snap, [first, second], now=NOW, trust={"deny": {key(2)}})
    assert out["level"] == levels.UNVERIFIED, "a denied key's word is not counted"
