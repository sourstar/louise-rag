"""The encoder rotation in the worker (worker._serve_rotation) — cost tests.

These count TURNS and SECONDS rather than checking that documents move: the
defects this scheduling exists to prevent (a collection with nothing to do
holding a slot, a busy one starving a newly-ready one, a deferral costing a
full budget) all pass a behavioural test unchanged.
"""
import types

import pytest

from app import sched, worker


class FakeCollection:
    def __init__(self, name: str, url: str = "http://gpu:11434"):
        self.name = name
        self.url = url


class FakeClock:
    """Patched over time.monotonic so the scheduler and the rotation share
    one controllable clock — a step's cost is what the test says it is."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(worker.time, "monotonic", c)
    return c


@pytest.fixture
def s(monkeypatch, clock):
    fresh = sched.Scheduler(base_quantum_s=1.0, clock=clock)
    monkeypatch.setattr(sched, "_scheduler", fresh)
    monkeypatch.setattr(worker.collections_mod, "recipe_for",
                        lambda c: types.SimpleNamespace(url=c.url))
    return fresh


def steps(monkeypatch, clock, plan: dict):
    """plan: collection name -> seconds each step costs, or None for "this
    collection has nothing to do / was deferred". Returns the call log."""
    calls: list[str] = []

    def fake_step(con, collection, last_idle_log, pipeline_active=False):
        calls.append(collection.name)
        cost = plan.get(collection.name)
        if cost is None:
            clock.t += 0.001            # finding out is not free, but it is cheap
            return False
        clock.t += cost
        return True

    monkeypatch.setattr(worker, "_run_one_step", fake_step)
    return calls


def rotate(collections, passes=1):
    cons = {c.name: object() for c in collections}
    worked = []
    for _ in range(passes):
        worked.append(worker._serve_rotation(cons, collections, {}, True))
    return worked


def turns(calls: list[str], name: str) -> int:
    return calls.count(name)


# ---------------------------------------------------------------------------

def test_a_collection_with_nothing_to_do_leaves_the_rotation(monkeypatch, clock, s):
    """It is probed once — a missed demand signal must not strand it — and
    then holds no share at all, however long the others run. Before this,
    every collection was visited every round whether it had work or not."""
    a, b = FakeCollection("busy"), FakeCollection("empty")
    calls = steps(monkeypatch, clock, {"busy": 0.5, "empty": None})
    rotate([a, b], passes=5)
    assert turns(calls, "empty") == 1
    assert turns(calls, "busy") >= 5


def test_a_deferral_hands_the_slot_back_at_once(monkeypatch, clock, s):
    """A deferred step (busy manifest, unreachable encoder) must cost one
    attempt, not a full budget of attempts against an endpoint that is not
    answering."""
    a = FakeCollection("deferring")
    calls = steps(monkeypatch, clock, {"deferring": None})
    assert rotate([a]) == [False]           # no work claimed
    assert turns(calls, "deferring") == 1   # and it did not keep trying
    assert s.snapshot()[worker.embed_resource(a)]["deferring"]["in_rotation"] is False


def test_a_deferral_does_not_count_as_a_unit(monkeypatch, clock, s):
    """The measured cost of a collection's units decides its floor, so time
    spent discovering there was nothing to do must not pollute it."""
    a = FakeCollection("a")
    steps(monkeypatch, clock, {"a": None})
    rotate([a])
    entry = s.snapshot()[worker.embed_resource(a)]["a"]
    assert entry["units"] == 0
    assert entry["suggest_floor_s"] is None


def test_an_expensive_collection_does_not_starve_a_cheap_one(monkeypatch, clock, s):
    """The pathology this replaces: one collection inside a long unit while
    another that is ready waits for the whole thing. The expensive one still
    overruns — nothing can preempt a Lance write — but the overrun is debt,
    so it is skipped next time instead of compounding."""
    cheap, dear = FakeCollection("cheap"), FakeCollection("dear")
    calls = steps(monkeypatch, clock, {"cheap": 0.1, "dear": 3.0})
    rotate([cheap, dear], passes=6)
    entry = s.snapshot()[worker.embed_resource(cheap)]
    assert entry["cheap"]["turns"] >= 5          # never starved
    assert entry["dear"]["overruns"] >= 1        # and the overrun is recorded
    # the debt clamp bounds how far ahead an overrunning flow can get
    assert entry["dear"]["share"] < 0.8
    assert turns(calls, "cheap") > turns(calls, "dear")


def test_the_slot_is_spent_over_many_cheap_units(monkeypatch, clock, s):
    """A cheap collection is not limited to one document per turn — that was
    the one-document round that starved cheap stages behind expensive
    neighbours. 1 s quantum / 0.1 s units = ten units."""
    a = FakeCollection("a")
    calls = steps(monkeypatch, clock, {"a": 0.1})
    rotate([a])
    assert turns(calls, "a") == 10


def test_collections_on_different_endpoints_do_not_contend(monkeypatch, clock, s):
    """Two collections pointed at one GPU must share it; two pointed at
    different containers must not. The resource NAME is what tells them
    apart, so both get a turn in the same pass."""
    a = FakeCollection("a", url="http://gpu-1:11434")
    b = FakeCollection("b", url="http://gpu-2:11434")
    calls = steps(monkeypatch, clock, {"a": 0.5, "b": 0.5})
    rotate([a, b])
    snap = s.snapshot()
    assert set(snap) == {"encoder:http://gpu-1:11434", "encoder:http://gpu-2:11434"}
    assert turns(calls, "a") and turns(calls, "b")


def test_collections_on_one_endpoint_share_one_rotation(monkeypatch, clock, s):
    a = FakeCollection("a", url="http://gpu:11434")
    b = FakeCollection("b", url="http://gpu:11434")
    steps(monkeypatch, clock, {"a": 0.5, "b": 0.5})
    rotate([a, b], passes=4)
    snap = s.snapshot()["encoder:http://gpu:11434"]
    assert set(snap) == {"a", "b"}
    assert abs(snap["a"]["share"] - snap["b"]["share"]) < 0.1


def test_a_removed_collection_is_dropped(monkeypatch, clock, s):
    a, b = FakeCollection("a"), FakeCollection("b")
    steps(monkeypatch, clock, {"a": 0.5, "b": 0.5})
    rotate([a, b])
    rotate([a])
    assert set(s.snapshot()[worker.embed_resource(a)]) == {"a"}


def test_stop_ends_the_pass_without_holding_a_slot(monkeypatch, clock, s):
    """SIGTERM: finish the current unit, commit, exit. The rotation must not
    leave a flow holding a resource it will never release."""
    a = FakeCollection("a")
    steps(monkeypatch, clock, {"a": 0.1})
    worker._stop.set()
    try:
        rotate([a])
    finally:
        worker._stop.clear()
    assert s.snapshot() == {} or \
        all(not e["in_rotation"] for e in
            s.snapshot().get(worker.embed_resource(a), {}).values())


def test_legacy_path_still_works(monkeypatch, clock, s):
    """SCHED_FAIR_SHARE=0 keeps the previous behaviour available."""
    a, b = FakeCollection("a"), FakeCollection("b")
    calls = steps(monkeypatch, clock, {"a": 0.5, "b": None})
    cons = {c.name: object() for c in (a, b)}
    monkeypatch.setattr(worker.time, "time", clock)   # the legacy path uses time()
    assert worker._serve_in_order(cons, [a, b], {}, True) is True
    assert turns(calls, "a") >= 1 and turns(calls, "b") == 1
