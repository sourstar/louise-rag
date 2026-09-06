"""tests/share/test_rendezvous.py — the properties docs/04-sharing.md §4.5
promises, each asserted."""
import math

import pytest

from app.share import rendezvous as rv
from app.share.rendezvous import Peer, ShardWork

LIMIT = 6 * 3600          # a shard type may take at most six hours with margin (§4.5)


def peers(n, benchmark=100.0):
    return [Peer(f"peer{i:02d}", benchmark) for i in range(n)]


def shards(n, tokens=100_000):
    return [ShardWork(f"sha256:c/year=1980/{i:05d}", tokens) for i in range(n)]


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
    weak = Peer("weak", 5.0)           # 5 tokens/s
    strong = Peer("strong", 100.0)
    small = ShardWork("s", tokens=10_000)      # weak: 10k/5*3 = 6000 s = 1.7 h  → eligible
    large = ShardWork("l", tokens=100_000)     # weak: 100k/5*3 = 60000 s = 16.7 h → not
    assert rv.eligible(weak, small, LIMIT) and not rv.eligible(weak, large, LIMIT)
    assert rv.eligible(strong, large, LIMIT)
    assert [p.key for p in rv.ranking(large, [weak, strong], LIMIT)] == ["strong"]
    assert not rv.eligible(Peer("zero", 0.0), small, LIMIT)


def test_no_eligible_peer_means_no_leader_not_a_crash():
    tiny = [Peer("t", 1.0)]
    assert rv.leader(ShardWork("x", 10**9), tiny, LIMIT) is None
    assert rv.assign([ShardWork("x", 10**9)], tiny, LIMIT) == {"x": None}


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
