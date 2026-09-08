"""tests/share/test_rendezvous.py — the properties docs/04-sharing.md §4.5
promises, each asserted."""
import math

import pytest

from app.share import rendezvous as rv
from app.share.rendezvous import Peer, ShardWork

# 04 §4.5: the limit is the collection's WORK PERIOD — `3 x tokens / w <=
# seconds(work.per)` — not a fixed number of hours. P1M is the default.
LIMIT = 30 * 24 * 3600
SHARD = "sha256:c/03ae64488c899305/year=1980/00001"


def peers(n, benchmark=100.0):
    return [Peer(f"peer{i:02d}", benchmark) for i in range(n)]


def shards(n, tokens=100_000):
    return [ShardWork(f"sha256:c/03ae64488c899305/year=1980/{i:05d}", tokens) for i in range(n)]


def test_deterministic_across_calls_and_peer_order():
    ps, ss = peers(7), shards(50)
    a = rv.assign(ss, ps, LIMIT)
    b = rv.assign(ss, list(reversed(ps)), LIMIT)
    assert a == b == rv.assign(ss, ps, LIMIT)
    for s in ss:
        assert [p.key for p in rv.ranking(s, ps, LIMIT)] == [p.key for p in rv.ranking(s, reversed(ps), LIMIT)]


def test_alone_a_peer_takes_everything():
    me = Peer("solo", 100.0)
    ss = shards(40)
    assert rv.mine("solo", ss, [me], LIMIT) == ss
    assert all(v == "solo" for v in rv.assign(ss, [me], LIMIT).values())


def test_ranking_is_a_full_ordering_of_eligible_peers():
    ps, s = peers(9), shards(1)[0]
    r = rv.ranking(s, ps, LIMIT)
    assert len(r) == 9 and len({p.key for p in r}) == 9
    scores = [rv.score(p, s) for p in r]
    assert scores == sorted(scores, reverse=True)


def test_removing_a_peer_reassigns_only_its_shards():
    ps, ss = peers(10), shards(400)
    before = rv.assign(ss, ps, LIMIT)
    gone = "peer03"
    after = rv.assign(ss, [p for p in ps if p.key != gone], LIMIT)
    moved = {k for k in before if before[k] != after[k]}
    assert moved == {k for k in before if before[k] == gone}, "only the departed peer's shards move"
    # and they move to a deterministic successor: the next in that shard's old ranking
    for k in moved:
        s = next(x for x in ss if x.shard_id == k)
        assert after[k] == rv.successor(s, ps, gone, LIMIT).key


def test_adding_a_peer_only_takes_shards_it_now_leads():
    ps, ss = peers(10), shards(400)
    before = rv.assign(ss, ps, LIMIT)
    after = rv.assign(ss, ps + [Peer("newcomer", 100.0)], LIMIT)
    changed = {k for k in before if before[k] != after[k]}
    assert all(after[k] == "newcomer" for k in changed)
    assert 0 < len(changed) < len(ss)


def test_twice_the_weight_leads_about_twice_the_shards():
    strong = Peer("strong", 200.0)
    weak = [Peer(f"w{i}", 100.0) for i in range(9)]
    share = rv.share_of_leadership([strong] + weak, shards(4000))
    # expected: strong 2/11 ≈ 0.182, each weak 1/11 ≈ 0.091
    assert share["strong"] == pytest.approx(2 / 11, abs=0.02)
    for w in weak:
        assert share[w.key] == pytest.approx(1 / 11, abs=0.02)


def test_eligibility_admits_weak_peers_only_to_small_shards():
    """04 §4.5: eligible ⇔ 3 × tokens / w ≤ seconds(work.per). With the
    default P1M that is 864,000 × w tokens — 4.3M for a 5 tok/s peer, 86M
    for a 100 tok/s one."""
    weak = Peer("weak", 5.0)
    strong = Peer("strong", 100.0)
    small = ShardWork(SHARD, tokens=4_000_000)     # weak: 3×4M/5   = 2.4Ms  ≤ P1M
    large = ShardWork(SHARD, tokens=10_000_000)    # weak: 3×10M/5  = 6.0Ms  > P1M
    assert rv.eligible(weak, small, LIMIT) and not rv.eligible(weak, large, LIMIT)
    assert rv.eligible(strong, large, LIMIT)       # 3×10M/100 = 300ks
    assert [p.key for p in rv.ranking(large, [weak, strong], LIMIT)] == ["strong"]
    assert not rv.eligible(Peer("zero", 0.0), small, LIMIT), \
        "no benchmark for this space is UNSCORED, not slow (04 §4.5)"


def test_an_unscored_peer_is_never_scored_as_slow():
    """A peer with no `benchmarks` entry for the space is absent from the
    ordering, not ranked last — scoring it would be a rate measured on
    another encoder answering a question about this one."""
    with pytest.raises(ValueError, match="UNSCORED"):
        rv.score(Peer("no-benchmark", 0.0), ShardWork(SHARD, 1000))


def test_the_score_is_the_documents_formula():
    """04 §4.5, byte for byte: h = (first 64 bits of H(peer_key ‖ shard_id)
    + 0.5) / 2^64, and score = ln(h) / w. `H(a ‖ b)` is 09 §9.1's ONE
    concatenation — SHA-256 over the canonical JSON of [a, b] — not a
    separator byte, which §9.1 rules out explicitly. Every peer must reach
    the same number or the partition is not a partition."""
    import hashlib, json, math
    key, shard_id = "ed25519:" + "ab" * 32, SHARD
    raw = hashlib.sha256(json.dumps([key, shard_id], sort_keys=True,
                                    separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()
    h = (int(raw[:16], 16) + 0.5) / 2 ** 64
    assert rv.score(Peer(key, 7.0), ShardWork(shard_id, 1000)) == math.log(h) / 7.0


def test_a_separator_byte_would_have_given_a_different_partition():
    """The bug this replaced: sha256(key + b"\x00" + shard_id). It is a
    different number, so two peers disagreeing about which form to use
    would each believe they lead a different set of shards."""
    import hashlib, math
    key, shard_id = "ed25519:" + "ab" * 32, SHARD
    old = hashlib.sha256(f"{key}\x00{shard_id}".encode()).digest()
    old_h = (int.from_bytes(old[:8], "big") + 0.5) / 2 ** 64
    assert rv.score(Peer(key, 7.0), ShardWork(shard_id, 1000)) != math.log(old_h) / 7.0


def test_no_eligible_peer_means_no_leader_not_a_crash():
    tiny = [Peer("t", 1.0)]
    assert rv.leader(ShardWork(SHARD, 10**14), tiny, LIMIT) is None
    assert rv.assign([ShardWork(SHARD, 10**14)], tiny, LIMIT) == {SHARD: None}


def test_successor_cascade_walks_the_ranking_and_ends():
    ps, s = peers(4), shards(1)[0]
    order = [p.key for p in rv.ranking(s, ps, LIMIT)]
    assert rv.successor(s, ps, order[0], LIMIT).key == order[1]
    assert rv.successor(s, ps, order[2], LIMIT).key == order[3]
    assert rv.successor(s, ps, order[3], LIMIT) is None
    assert rv.successor(s, ps, "stranger", LIMIT) is None


def test_fetch_leader_is_deterministic_and_unbounded_by_size():
    ps = peers(6)
    a = rv.fetch_leader("sha256:c", "2026-09", ps)
    b = rv.fetch_leader("sha256:c", "2026-09", list(reversed(ps)))
    assert a == b and a is not None
    assert rv.fetch_leader("sha256:c", "2026-10", ps) is not None
    # even a very weak peer can fetch; the fetch has no token cost
    assert rv.fetch_leader("sha256:c", "2026-09", [Peer("weak", 0.001)]).key == "weak"


def test_unit_hash_is_strictly_inside_zero_one():
    for k in range(200):
        u = rv._unit(f"k{k}", "s")
        assert 0.0 < u < 1.0 and math.isfinite(math.log(u))
