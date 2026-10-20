"""app/share/leading.py — the embed stage leaves to another live peer the
open parts it leads (04 §4.5). Every doubt embeds: the worst case is the
old duplicate, never a gap."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.share import leading, rendezvous

ME, PEER = "ed25519:" + "aa" * 32, "ed25519:" + "bb" * 32
LIVE = [rendezvous.Peer(ME, 1000.0), rendezvous.Peer(PEER, 1000.0)]


def _codex(keys=("lang",), unknown="lang=unknown", order="numeric"):
    slicing = {"keys": list(keys), "order": order, "shard_records": 50}
    if unknown:
        slicing["unknown"] = unknown
    return SimpleNamespace(collection_id="sha256:c0", space_segment="5ac3c5810f2cd56a",
                           slicing=slicing)


def _shard(slice_, n, first, last):
    return SimpleNamespace(slice=slice_, n=n, first_id=str(first), last_id=str(last))


SNAPSHOT = SimpleNamespace(snapshot_id="2026-09", shards=[
    _shard("lang=en", 1, 10, 500), _shard("lang=en", 2, 501, 900),
    _shard("lang=fr", 1, 400, 700),          # overlaps en's ids: slices are per key
    _shard("lang=unknown", 1, 5, 9000),
])


def _sid(codex, slice_, n):
    return f"{codex.collection_id}/{codex.space_segment}/{slice_}/{n:05d}"


def _leaders(codex, **lead):
    """lead: '<slice>/<n>' -> key, every other shard closed (absent)."""
    return {_sid(codex, *k.rsplit("/", 1)[:1], int(k.rsplit("/", 1)[1])): v for k, v in lead.items()}


def test_a_part_another_live_peer_leads_is_skipped_and_ours_is_not():
    c = _codex()
    s = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=en/1": PEER, "lang=en/2": ME}),
                      me=ME, live=LIVE)
    assert s is not None and s.skipped_parts == 1
    assert s.skips("100", "en") is True, "en part 1: the peer's"
    assert s.skips("600", "en") is False, "en part 2: ours"
    assert s.skips("600", "fr") is False, "fr's part is closed here, nobody leads it"
    assert s.skips("950", "en") is False, "no part holds the id: embedded"


def test_the_slice_column_decides_between_overlapping_slices():
    c = _codex()
    s = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=fr/1": PEER, "lang=en/1": ME}),
                      me=ME, live=LIVE)
    assert s.skips("450", "fr") is True and s.skips("450", "en") is False
    assert s.skips_row({"native_id": "450", "lang": "fr"}) is True


def test_without_the_slice_an_id_two_slices_could_hold_is_embedded():
    c = _codex(keys=("year",), unknown=None)            # no manifest column names the slice
    s = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=fr/1": PEER}), me=ME, live=LIVE)
    assert leading.slice_column(c) is None
    assert s.skips("450") is False, "en and fr both hold 450: only the record knows — embed"
    s2 = leading.build(c, SimpleNamespace(snapshot_id="x", shards=SNAPSHOT.shards[:3]),
                       leaders=_leaders(c, **{"lang=en/1": PEER}), me=ME, live=LIVE)
    assert s2.skips("100") is True, "one slice can hold 100: its part decides"


def test_a_record_with_no_language_is_in_the_unknown_slice():
    c = _codex()
    s = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=unknown/1": PEER}), me=ME, live=LIVE)
    assert s.skips("100", None) is True and s.skips("100", "") is True
    assert s.skips("100", "en") is False


@pytest.mark.parametrize("me, live", [
    (None, LIVE),                                      # no key: leads nothing, skips nothing
    (ME, [rendezvous.Peer(PEER, 1000.0)]),              # not live (no benchmark yet)
    (ME, [rendezvous.Peer(ME, 1000.0)]),                # alone: everything is ours (04 §4.1)
])
def test_a_node_that_cannot_lead_skips_nothing(me, live):
    c = _codex()
    assert leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=en/1": PEER}),
                         me=me, live=live) is None


def test_nothing_led_elsewhere_means_no_filter_at_all():
    c = _codex()
    assert leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=en/1": ME}), me=ME,
                         live=LIVE) is None, "the stage runs exactly as before"


def test_an_id_the_order_cannot_place_is_embedded():
    c = _codex()
    s = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=en/1": PEER}), me=ME, live=LIVE)
    assert s.skips("not-a-number", "en") is False


def test_a_lookup_is_one_bisection_never_a_walk_over_the_parts(monkeypatch):
    """COST: with the slice known, one slice is searched, by bisection — a
    lookup does not grow with the number of parts (CLAUDE.md engineering
    rules)."""
    c = _codex()
    shards = [_shard("lang=en", i, i * 100, i * 100 + 99) for i in range(1, 2001)]
    snap = SimpleNamespace(snapshot_id="big", shards=shards)
    lead = {_sid(c, "lang=en", i): (PEER if i % 2 else ME) for i in range(1, 2001)}
    s = leading.build(c, snap, leaders=lead, me=ME, live=LIVE)
    calls = []
    real = leading.Skips._part_in
    monkeypatch.setattr(leading.Skips, "_part_in",
                        lambda self, name, k: calls.append(name) or real(self, name, k))
    assert s.skips("150", "en") is True and s.skips("250", "en") is False
    assert calls == ["lang=en", "lang=en"], "one slice per lookup"
    real_bisect = leading.bisect.bisect_right
    probes = []
    monkeypatch.setattr(leading.bisect, "bisect_right",
                        lambda a, x: probes.append(1) or real_bisect(a, x))
    s.skips("199950", "en")
    assert len(probes) == 1


def test_the_hot_path_reads_no_digest_within_the_rebuild_interval(monkeypatch, tmp_path):
    """The embed stage asks every round; digests, statements and the coverage
    map are read at most once a REBUILD_S."""
    from app.share import contribute, coverage as cov
    leading.reset_cache()
    c = _codex()
    coll = SimpleNamespace(codex=c, name="gutenberg", manifest_path=tmp_path / "m.sqlite")
    reads = []
    monkeypatch.setattr(cov, "live_peers", lambda *a, **k: reads.append(1) or LIVE)
    monkeypatch.setattr(cov, "own_benchmarks", lambda *a, **k: [])
    monkeypatch.setattr(contribute, "work_order",
                        lambda codex, snap, con, me, live, now: {_sid(codex, "lang=en", 1): 2})

    class Con:
        def execute(self, *a):
            return SimpleNamespace(fetchone=lambda: (0, None))
    kw = dict(root=tmp_path, own_key=ME, snapshot=SNAPSHOT, node_con=object())
    f = leading.skip_test(coll, Con(), now=1000.0, **kw)
    assert f is not None and f({"native_id": "100", "lang": "en"}) is True
    assert leading.skip_test(coll, Con(), now=1030.0, **kw) is not None
    assert reads == [1], "within REBUILD_S: the cache, no digest read"
    leading.skip_test(coll, Con(), now=1000.0 + leading.REBUILD_S + 1, **kw)
    assert reads == [1, 1], "past REBUILD_S: the inputs are read again"
    leading.reset_cache()


def test_any_failure_to_read_the_inputs_skips_nothing(monkeypatch, tmp_path):
    from app.share import coverage as cov
    leading.reset_cache()
    coll = SimpleNamespace(codex=_codex(), name="g", manifest_path=tmp_path / "m")
    monkeypatch.setattr(cov, "own_benchmarks", lambda *a, **k: 1 / 0)
    assert leading.skip_test(coll, object(), root=tmp_path, own_key=ME, snapshot=SNAPSHOT) is None
    assert leading.skip_test(SimpleNamespace(codex=None, name="x"), object()) is None
    leading.reset_cache()


def test_a_failure_is_cached_like_a_success_not_retried_every_round(monkeypatch, tmp_path):
    """BLOCKING in the review of 45535ea: a locked node.sqlite or an
    unreadable digest cost its busy timeout every round. Now once a REBUILD_S."""
    from app.share import coverage as cov
    leading.reset_cache()
    coll = SimpleNamespace(codex=_codex(), name="g", manifest_path=tmp_path / "m")
    tries = []
    monkeypatch.setattr(cov, "own_benchmarks", lambda *a, **k: tries.append(1) or 1 / 0)
    kw = dict(root=tmp_path, own_key=ME, snapshot=SNAPSHOT)
    assert leading.skip_test(coll, object(), now=1000.0, **kw) is None
    assert leading.skip_test(coll, object(), now=1010.0, **kw) is None
    assert tries == [1], "within REBUILD_S the failure is not retried"
    leading.skip_test(coll, object(), now=1000.0 + leading.REBUILD_S + 1, **kw)
    assert tries == [1, 1]
    leading.reset_cache()


def test_the_node_database_is_read_under_a_short_bound(monkeypatch, tmp_path):
    """A pipeline thread never waits without a bound (CLAUDE.md)."""
    from app.share import coverage as cov, peers
    leading.reset_cache()
    pragmas = []

    class NodeCon:
        def execute(self, s, *a):
            pragmas.append(s)

        def close(self):
            pass
    monkeypatch.setattr(peers, "connect", lambda: NodeCon())
    monkeypatch.setattr(cov, "own_benchmarks", lambda *a, **k: [])
    monkeypatch.setattr(cov, "live_peers", lambda *a, **k: [])
    coll = SimpleNamespace(codex=_codex(), name="g", manifest_path=tmp_path / "m")

    class Con:
        def execute(self, *a):
            return SimpleNamespace(fetchone=lambda: (0, None))
    leading.skip_test(coll, Con(), root=tmp_path, own_key=ME, snapshot=SNAPSHOT)
    assert pragmas == [f"PRAGMA busy_timeout = {leading.NODE_DB_TIMEOUT_MS}"]
    leading.reset_cache()


# ---- 04 §4.5, amended 2026-10-20: the bounty list has no leader ------------------------------------------------

def test_ranks_walk_the_bounty_list_first_rank_first_and_widen_when_it_is_empty():
    """A part where this peer ranks second is left for now, not for ever:
    once nothing is left at rank 1, `widen` admits rank 2. A part left
    aside (None: someone visibly on it, or not eligible) stays aside."""
    c = _codex()
    ranks = {_sid(c, "lang=en", 1): 1, _sid(c, "lang=en", 2): 2, _sid(c, "lang=fr", 1): None}
    s1 = leading.build(c, SNAPSHOT, ranks=ranks, me=ME, live=LIVE, rank_limit=1)
    assert s1.skips("100", "en") is False and s1.skips("600", "en") is True and s1.skips("450", "fr") is True
    assert (s1.rank_limit, s1.max_rank, s1.skipped_parts) == (1, 2, 2)
    s2 = leading.build(c, SNAPSHOT, ranks=ranks, me=ME, live=LIVE, rank_limit=2)
    assert s2.skips("600", "en") is False, "rank 2 admitted"
    assert s2.skips("450", "fr") is True, "left aside at any rank"
    assert s2.skipped_parts == 1
    # the 2026-10-19 form still reads: the first peer's parts are rank 1, the others' rank 2
    s3 = leading.build(c, SNAPSHOT, leaders=_leaders(c, **{"lang=en/1": PEER, "lang=en/2": ME}), me=ME, live=LIVE)
    assert s3.skips("100", "en") is True and s3.skips("600", "en") is False


def test_widen_moves_the_stage_to_the_next_rank_and_a_changed_view_starts_again(monkeypatch, tmp_path):
    from app.share import contribute, coverage as cov
    leading.reset_cache()
    c = _codex()
    coll = SimpleNamespace(codex=c, name="g", manifest_path=tmp_path / "m.sqlite")
    monkeypatch.setattr(cov, "live_peers", lambda *a, **k: LIVE)
    monkeypatch.setattr(cov, "own_benchmarks", lambda *a, **k: [])
    ranks = {_sid(c, "lang=en", 1): 2, _sid(c, "lang=en", 2): 3}
    monkeypatch.setattr(contribute, "work_order", lambda codex, snap, con, me, live, now: ranks)
    fp = {"n": 0}

    class Con:
        def execute(self, *a):
            return SimpleNamespace(fetchone=lambda: (fp["n"], None))
    kw = dict(root=tmp_path, own_key=ME, snapshot=SNAPSHOT, node_con=object())
    f = leading.skip_test(coll, Con(), now=1000.0, **kw)
    assert f({"native_id": "100", "lang": "en"}) is True and f({"native_id": "600", "lang": "en"}) is True, "rank 1 only"
    assert leading.widen(coll) == 2 and leading.rank_limit_of(coll) == 2
    f = leading.skip_test(coll, Con(), now=1001.0, **kw)
    assert f({"native_id": "100", "lang": "en"}) is False and f({"native_id": "600", "lang": "en"}) is True
    assert leading.widen(coll) == 3
    assert leading.skip_test(coll, Con(), now=1002.0, **kw) is None, "every open part admitted: no filter"
    assert leading.widen(coll) == 3, "the deepest rank: no further"
    # a bundle lands (the coverage changed): past REBUILD_S the view is rebuilt and
    # the walk starts again at rank 1
    fp["n"] = 1
    f = leading.skip_test(coll, Con(), now=1002.0 + leading.REBUILD_S + 1, **kw)
    assert leading.rank_limit_of(coll) == 1 and f({"native_id": "100", "lang": "en"}) is True
    leading.reset_cache()


def test_a_producer_is_visibly_on_a_part_only_within_the_parts_time_at_its_rate():
    from app.share import contribute, messages
    ranking = [rendezvous.Peer(PEER, 100.0), rendezvous.Peer(ME, 50.0)]
    tokens = 30_000                                    # 300 s at 100 tokens/s, ×3 = 900 s
    now = 10_000.0
    rows = [{"producer": PEER, "accepted_at": messages.time_text(now - 600)}]
    assert contribute.visibly_on(ranking, rows, tokens, me=ME, now=now) == PEER
    rows = [{"producer": PEER, "accepted_at": messages.time_text(now - 1000)}]
    assert contribute.visibly_on(ranking, rows, tokens, me=ME, now=now) is None, "older than its window: anyone's"
    rows = [{"producer": ME, "accepted_at": messages.time_text(now - 10)}]
    assert contribute.visibly_on(ranking, rows, tokens, me=ME, now=now) is None, "my own bundle is not someone else"
    rows = [{"producer": "ed25519:" + "cc" * 32, "accepted_at": messages.time_text(now - 10)}]
    assert contribute.visibly_on(ranking, rows, tokens, me=ME, now=now) is None, "a producer not live is not on it"
    assert rendezvous.rank_of(ME, ranking) == 2 and rendezvous.rank_of(PEER, ranking) == 1
    assert rendezvous.rank_of("ed25519:" + "cc" * 32, ranking) is None
