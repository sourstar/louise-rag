"""The fair-share scheduler (app/sched.py) — one test per guarantee the
operator's model names, because every one of them has an obvious wrong
implementation that behaves identically until the system is busy.

These are COST tests, not behaviour tests: they count turns and seconds.
"""
import pytest

from app import sched


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def s(clock):
    return sched.Scheduler(base_quantum_s=10.0, clock=clock)


def run_turn(s, resource, seconds, *, idle=False, units=1):
    """One turn that spends `seconds` and hands the resource back."""
    turn = s.acquire(resource)
    assert turn is not None
    turn.spent(seconds, units)
    turn.release(idle=idle)
    return turn.collection


# --------------------------------------------------------------- fair share

def test_equal_flows_get_equal_share(s):
    for name in ("a", "b", "c"):
        s.demand("gpu", name)
    served = [run_turn(s, "gpu", 10.0) for _ in range(9)]
    assert served == ["a", "b", "c"] * 3
    shares = {n: e["share"] for n, e in s.snapshot()["gpu"].items()}
    assert all(abs(v - 1 / 3) < 0.01 for v in shares.values())


def test_a_flow_that_needs_nothing_is_assigned_nothing(s):
    """B's requirement: an equal share of each resource it ACTUALLY needs.

    A declared collection is probed ONCE (the safety net — a missed demand
    signal must not strand it), finds nothing, and then holds no share at
    all: it does not appear again until the reprobe interval, however long
    the others run."""
    s.declare("gpu", "cpu-only-collection", 1.0)
    s.demand("gpu", "a")
    served = []
    for _ in range(6):
        turn = s.acquire("gpu")
        if turn.collection == "a":
            turn.spent(10.0)
            turn.release(idle=False)
        else:
            turn.release(idle=True)               # nothing to do here
        served.append(turn.collection)
    assert served.count("cpu-only-collection") == 1
    assert served.count("a") == 5


def test_unused_capacity_goes_to_the_others_not_to_a_ledger(s):
    """Work-conserving: with b idle, a and c simply come round faster.
    Nothing is banked, so there is no loan to repay when b returns."""
    for name in ("a", "b", "c"):
        s.demand("gpu", name)
    run_turn(s, "gpu", 10.0)                      # a
    run_turn(s, "gpu", 0.0, idle=True)            # b: nothing to do, leaves
    assert [run_turn(s, "gpu", 10.0) for _ in range(4)] == ["c", "a", "c", "a"]


# ------------------------------------------- release, and re-entry at the tail

def test_a_released_resource_is_reclaimed_only_on_the_next_turn(s):
    """The operator's rule. A releases early and immediately wants back in;
    B must still get its turn first — a release is not a way to cut in."""
    for name in ("a", "b"):
        s.demand("gpu", name)
    assert run_turn(s, "gpu", 1.0, idle=True) == "a"
    s.demand("gpu", "a")                          # "upon need", straight away
    assert run_turn(s, "gpu", 10.0) == "b"
    assert run_turn(s, "gpu", 10.0) == "a"


def test_release_on_deferral_costs_the_flow_nothing(s):
    """A deferral (busy manifest, unreachable encoder) is idle=True: the
    flow leaves without spending its slot, and its next turn is full-sized."""
    s.demand("gpu", "a")
    turn = s.acquire("gpu")
    turn.release(idle=True)                       # never even worked
    s.demand("gpu", "a")
    again = s.acquire("gpu")
    assert again.quantum_s == 10.0
    assert again.may_continue()


def test_going_idle_does_not_reduce_the_entitlement(clock, s):
    """The bug this rules out: decaying the share of whoever is idle. A
    bursty collection would lose its share for being bursty."""
    for name in ("a", "b"):
        s.demand("gpu", name)
    run_turn(s, "gpu", 0.0, idle=True)            # a idles out
    for _ in range(20):
        run_turn(s, "gpu", 10.0)                  # b runs alone for a while
    clock.advance(3600.0)
    s.demand("gpu", "a")
    run_turn(s, "gpu", 10.0)                      # b is ahead: a waits its turn
    turn = s.acquire("gpu")
    assert turn.collection == "a"
    assert turn.quantum_s == 10.0                 # full slot, undiminished


def test_going_idle_does_not_bank_credit(s):
    """The mirror image: an idle flow must not return with a huge deficit
    and monopolise the resource to 'catch up'."""
    for name in ("a", "b"):
        s.demand("gpu", name)
    run_turn(s, "gpu", 0.0, idle=True)            # a leaves at once
    for _ in range(5):
        run_turn(s, "gpu", 10.0)                  # b works
    s.demand("gpu", "a")
    run_turn(s, "gpu", 10.0)                      # b's turn first
    turn = s.acquire("gpu")
    assert turn.collection == "a"
    turn.spent(10.0)
    assert not turn.may_continue()                # exactly one quantum, no more


def test_unspent_budget_is_not_carried(s):
    """'A collection that does not consume its slot' keeps neither the
    remainder nor a claim on it."""
    s.demand("gpu", "a")
    turn = s.acquire("gpu")
    turn.spent(2.0)
    turn.release(idle=False)                      # 8 s unspent
    nxt = s.acquire("gpu")
    nxt.spent(10.0)
    assert not nxt.may_continue()


# ------------------------------------------------------- overrun becomes debt

def test_an_overrun_shortens_the_next_turn(s):
    for name in ("a", "b"):
        s.demand("gpu", name)
    turn = s.acquire("gpu")                       # a
    turn.spent(14.0)                              # 4 s over its 10 s quantum
    turn.release(idle=False)
    assert run_turn(s, "gpu", 10.0) == "b"
    back = s.acquire("gpu")
    assert back.collection == "a"
    back.spent(6.0)
    assert not back.may_continue()                # 10 - 4 = 6 s, not 10


def test_paying_off_debt_never_reports_the_resource_as_idle(s):
    """Regression, and it would have been expensive. `acquire` returning
    None means "nothing wants this resource", and the worker sleeps on it.
    A flow settling an overrun still WANTS the resource, so the debt has to
    be worked off inside acquire — the first cut returned None instead,
    which would have alternated every 9 s unit with a 5 s idle sleep."""
    s.demand("gpu", "only")
    for _ in range(10):
        turn = s.acquire("gpu")
        assert turn is not None, "the worker would have gone to sleep here"
        turn.spent(9.0)                           # 9x its quantum, every time
        turn.release(idle=False)
    assert s.snapshot()["gpu"]["only"]["turns"] == 10


def test_debt_is_clamped_at_one_quantum(s):
    """An unbounded unit must not starve its own collection for many turns.
    The clamp is the deliberate trade: bounded starvation over exact
    fairness, with the oversized unit reported rather than hidden."""
    for name in ("a", "b"):
        s.demand("gpu", name)
    turn = s.acquire("gpu")                       # a
    turn.spent(300.0)                             # 30x its quantum
    turn.release(idle=False)
    served = [run_turn(s, "gpu", 10.0) for _ in range(4)]
    assert served.count("a") >= 1                 # not starved for 30 turns
    assert served[0] == "b"                       # but b is served first
    entry = s.snapshot()["gpu"]["a"]
    assert entry["overruns"] == 1
    assert entry["suggest_floor_s"] == 300.0      # the unit is the problem


# --------------------------------------------------------------- the floor

def test_the_floor_is_the_minimum_efficient_unit(s):
    """A collection whose smallest useful unit costs 40 s gets a 40 s slot,
    not the 10 s base: a slot below the floor means it can do nothing."""
    s.demand("gpu", "gutenberg", floor_s=40.0)
    s.demand("gpu", "pubmed", floor_s=0.4)
    big = s.acquire("gpu")
    assert big.collection == "gutenberg"
    assert big.quantum_s == 40.0
    big.spent(40.0)
    assert not big.may_continue()
    big.release(idle=False)
    small = s.acquire("gpu")
    assert small.collection == "pubmed"
    assert small.quantum_s == 10.0                # floor below base: base wins


def test_worst_case_reentry_is_the_sum_of_the_other_quanta(s):
    """Derived, not tunable — the number an operator needs when deciding
    how many collections may embed at once."""
    s.demand("gpu", "a", floor_s=40.0)
    s.demand("gpu", "b", floor_s=30.0)
    s.demand("gpu", "c", floor_s=0.1)
    quanta = {n: e["quantum_s"] for n, e in s.snapshot()["gpu"].items()}
    assert quanta == {"a": 40.0, "b": 30.0, "c": 10.0}
    run_turn(s, "gpu", 0.0, idle=True)            # a steps out
    s.demand("gpu", "a")                          # and wants back in
    waited = 0.0
    while True:
        turn = s.acquire("gpu")
        if turn.collection == "a":
            break
        waited += turn.quantum_s
        turn.spent(turn.quantum_s)
        turn.release(idle=False)
    assert waited == 40.0                         # b's 30 + c's 10


def test_the_floor_is_measured_from_what_the_units_cost(clock):
    """'The minimum time slot must let a plugin do its minimum processing'
    — so the floor is a measurement, not a setting."""
    s = sched.Scheduler(base_quantum_s=1.0, learn_floor=True, floor_cap_s=60.0,
                        clock=clock)
    s.demand("gpu", "a")
    for _ in range(10):
        turn = s.acquire("gpu")
        turn.spent(9.0)                           # every unit costs 9 s
        turn.release(idle=False)
    assert s.snapshot()["gpu"]["a"]["floor_s"] == 9.0
    assert s.acquire("gpu").quantum_s == 9.0      # the slot fits one unit


def test_a_measured_floor_survives_being_redeclared(clock):
    """Regression. The rotation re-declares its flows on every pass; an
    earlier cut passed a floor of 0 there, which wiped the measurement
    before it could ever take effect."""
    s = sched.Scheduler(base_quantum_s=1.0, learn_floor=True, clock=clock)
    s.demand("gpu", "a")
    for _ in range(10):
        turn = s.acquire("gpu")
        turn.spent(9.0)
        turn.release(idle=False)
    s.declare("gpu", "a")                         # as the rotation does
    assert s.snapshot()["gpu"]["a"]["floor_s"] == 9.0


def test_a_learned_floor_is_capped(clock):
    """A pathological unit must not be able to claim an unbounded slot: it
    is reported as an overrun instead, where a human sees it."""
    s = sched.Scheduler(base_quantum_s=1.0, learn_floor=True, floor_cap_s=20.0,
                        clock=clock)
    s.demand("gpu", "a")
    for _ in range(10):
        turn = s.acquire("gpu")
        turn.spent(300.0)
        turn.release(idle=False)
    entry = s.snapshot()["gpu"]["a"]
    assert entry["floor_s"] == 20.0               # capped
    assert entry["suggest_floor_s"] == 300.0      # but the truth is reported
    assert entry["overruns"] == 10


def test_one_expensive_unit_does_not_set_the_floor(clock):
    """Below the sample minimum the number is one document's luck."""
    s = sched.Scheduler(base_quantum_s=1.0, learn_floor=True, clock=clock)
    s.demand("gpu", "a")
    turn = s.acquire("gpu")
    turn.spent(30.0)
    turn.release(idle=False)
    assert s.snapshot()["gpu"]["a"]["floor_s"] == 0.0


# ------------------------------------------------------- query reservations

def test_a_reservation_is_served_before_the_rotation(s):
    for name in ("a", "b"):
        s.demand("gpu", name)
    s.reserve("gpu", "b")
    assert s.acquire("gpu").collection == "b"     # jumps a


def test_a_reservation_does_not_spend_the_collections_quantum(s):
    """A5: query work is a reservation, not a share. Answering a query must
    not cost that collection its place or its slot."""
    for name in ("a", "b"):
        s.demand("gpu", name)
    s.reserve("gpu", "b")
    turn = s.acquire("gpu")
    turn.spent(10.0)
    turn.release(idle=False)
    assert run_turn(s, "gpu", 10.0) == "a"        # rotation order intact
    assert run_turn(s, "gpu", 10.0) == "b"


# ------------------------------------------------------------- the safety net

def test_a_flow_with_no_demand_is_reprobed_not_stranded(clock, s):
    """Demand is announced, so a missed edge is possible. It must cost one
    delayed probe, never a collection that never runs again.

    A never-run flow is probed at once — at startup nothing has announced
    anything yet, and waiting out the interval would idle the worker for a
    minute after every restart."""
    s.declare("gpu", "a", 1.0)
    first = s.acquire("gpu")
    assert first is not None and first.collection == "a"
    first.release(idle=True)                      # probed: nothing to do
    assert s.acquire("gpu") is None               # and not probed again...
    clock.advance(sched.IDLE_REPROBE_S + 1)
    again = s.acquire("gpu")                      # ...until the interval
    assert again is not None and again.collection == "a"


def test_nothing_demanded_returns_none_immediately(s):
    assert s.acquire("gpu") is None               # never blocks, never sleeps


def test_a_stranded_flow_is_readmitted_while_the_others_stay_busy(clock, s):
    """Regression. The first cut offered the probe only when the rotation
    was EMPTY, which is no safety net at all: a collection that always has
    work holds the rotation open forever, so the stranded one would never
    be looked at again."""
    def drain(n):
        out = []
        for _ in range(n):
            turn = s.acquire("gpu")
            if turn is None:
                break
            out.append(turn.collection)
            if turn.collection == "busy":
                turn.spent(10.0)
                turn.release(idle=False)
            else:
                turn.release(idle=True)           # genuinely nothing to do
        return out

    s.demand("gpu", "busy")
    s.declare("gpu", "stranded", 1.0)
    assert "stranded" in drain(2)                 # the startup probe
    assert "stranded" not in drain(6)             # then it holds no share
    clock.advance(sched.IDLE_REPROBE_S + 1)
    assert "stranded" in drain(3)                 # ...and is not forgotten


# --------------------------------------------------------------- lifecycle

def test_sync_drops_removed_collections(s):
    for name in ("a", "b"):
        s.demand("gpu", name)
    s.sync("gpu", ["a"])
    assert [run_turn(s, "gpu", 10.0) for _ in range(2)] == ["a", "a"]
    assert "b" not in s.snapshot()["gpu"]


def test_the_turn_context_manager_releases_on_an_exception(s):
    """An exception is a deferral: the flow must not be left holding the
    resource, and must not be charged for work it did not do."""
    s.demand("gpu", "a")
    with pytest.raises(RuntimeError):
        with s.acquire("gpu") as turn:
            raise RuntimeError("boom")
    assert s.snapshot()["gpu"]["a"]["in_rotation"] is False
    assert s.snapshot()["gpu"]["a"]["seconds"] == 0.0


def test_double_release_is_harmless(s):
    s.demand("gpu", "a")
    turn = s.acquire("gpu")
    turn.spent(1.0)
    turn.release(idle=False)
    turn.release(idle=True)                       # ignored
    assert s.snapshot()["gpu"]["a"]["in_rotation"] is True
