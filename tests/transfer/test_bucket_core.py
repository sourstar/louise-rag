"""BucketCore — the pure decision core. No locks, no threads, no clock.

P0.7's statistical acceptance criteria live here, where they are deterministic
and take zero seconds. The threaded wrapper is tested separately for
concurrency correctness only.
"""
import pytest

from app.transfer.bucket import BucketCore, Priority

RATE = 1_000_000.0        # 1 MB/s
BURST = 0.5               # capacity = 500_000 bytes
RESERVE_FRAC = 0.20       # reserve  = 100_000 bytes


def _core(rate=RATE, reserve=RESERVE_FRAC, burst=BURST, now=0.0):
    return BucketCore(rate, now=now, burst_seconds=burst, reserve_fraction=reserve)


# ---------------- refill arithmetic ----------------

def test_starts_full_and_caps_at_capacity():
    c = _core()
    assert c.capacity == pytest.approx(500_000)
    assert c.tokens == pytest.approx(500_000)
    c.refill(now=10.0)                       # long idle must not over-accumulate
    assert c.tokens == pytest.approx(500_000)


def test_refill_is_proportional_to_elapsed_time():
    c = _core()
    c.tokens = 0.0
    c.refill(now=0.1)
    assert c.tokens == pytest.approx(100_000)
    c.refill(now=0.2)
    assert c.tokens == pytest.approx(200_000)


def test_refill_never_runs_backwards():
    c = _core()
    c.tokens = 0.0
    c.refill(now=-5.0)
    assert c.tokens == pytest.approx(0.0)


# ---------------- rule 1: wait against the ceiling, charge the truth ----------------

def test_oversized_request_does_not_deadlock_and_is_charged_in_full():
    """The bug this guards: requiring `tokens >= n + reserve` is unsatisfiable
    forever whenever n > capacity - reserve, which at 64 KiB chunks and a 20%
    reserve is every rate below ~1.25 Mbit/s."""
    c = _core(rate=100_000, burst=0.5)       # capacity 50_000, reserve 10_000
    n = 65_536                                # one 64 KiB chunk > capacity - reserve
    c.refill(now=100.0)                       # full
    assert c.can_grant(n, Priority.BACKFILL)  # must be satisfiable at all
    c.take(n, Priority.BACKFILL)
    assert c.tokens == pytest.approx(50_000 - 65_536)   # debt, not a clamp
    assert c.granted[Priority.BACKFILL] == 65_536       # charged in FULL


def test_debt_is_repaid_by_refill_so_the_long_run_average_holds():
    c = _core(rate=100_000, burst=0.5)
    c.take(200_000, Priority.BACKFILL)        # 2 s of budget taken at once
    assert c.tokens < 0
    c.refill(now=1.0)
    assert c.tokens < 0                       # still repaying after 1 s
    c.refill(now=10.0)
    assert c.tokens == pytest.approx(50_000)  # fully repaid, capped at capacity


def test_charging_is_never_clamped():
    """Clamping the DEDUCTION would under-count bytes already on disk and let
    goodput exceed the configured rate (invariant 5)."""
    c = _core()
    total = 0
    for _ in range(5):
        c.take(400_000, Priority.BACKFILL)
        total += 400_000
    assert c.granted[Priority.BACKFILL] == total


# ---------------- rule 2: rate changes refill at the OLD rate ----------------

def test_rate_change_settles_elapsed_time_at_the_old_rate():
    c = _core(rate=1_000_000)
    c.tokens = 0.0
    c.set_rate(10_000, now=0.1)               # 0.1 s elapsed at the OLD 1 MB/s
    assert c.tokens == pytest.approx(5_000)   # 100_000 accrued, then capped at new capacity
    assert c.rate == pytest.approx(10_000)
    assert c.capacity == pytest.approx(5_000)


def test_rate_change_clamps_tokens_to_the_new_capacity():
    c = _core(rate=1_000_000)                 # capacity 500_000, full
    c.set_rate(100_000, now=0.0)              # capacity now 50_000
    assert c.tokens == pytest.approx(50_000)


def test_rate_zero_blocks_everything():
    """A schedule window of 'off' must actually stop bytes, not divide by zero
    or grant unbounded."""
    c = _core(rate=0)
    c.refill(now=1000.0)
    assert not c.can_grant(1, Priority.INTERACTIVE)
    assert not c.can_grant(1, Priority.SEEDING)
    assert c.wait_time(1, Priority.INTERACTIVE) > 0


def test_unlimited_rate_grants_immediately_and_still_counts():
    """No platform.toml -> unlimited -> zero behavior change, but the SSE
    counters must still see the bytes."""
    c = _core(rate=None)
    assert c.can_grant(10**9, Priority.SEEDING)
    c.take(4096, Priority.BACKFILL)
    assert c.granted[Priority.BACKFILL] == 4096


# ---------------- the interactive reserve ----------------

def test_reserve_floor_applies_to_everyone_except_interactive():
    c = _core()
    c.tokens = 100_000                        # exactly the reserve
    assert c.can_grant(50_000, Priority.INTERACTIVE)      # may drain to empty
    assert not c.can_grant(50_000, Priority.DELTA)
    assert not c.can_grant(50_000, Priority.BACKFILL)
    assert not c.can_grant(50_000, Priority.SEEDING)


def test_interactive_burst_preempts_backfill_immediately():
    """Acceptance: 'interactive burst preempts backfill within 200 ms
    simulated'. With the reserve held back, the wait is zero, not 200 ms."""
    c = _core()
    # backfill has drained everything it is allowed to touch
    while c.can_grant(50_000, Priority.BACKFILL):
        c.take(50_000, Priority.BACKFILL)
    assert not c.can_grant(50_000, Priority.BACKFILL)
    assert c.can_grant(50_000, Priority.INTERACTIVE)
    assert c.wait_time(50_000, Priority.INTERACTIVE) <= 0.2


def test_zero_reserve_means_interactive_has_no_head_start():
    c = _core(reserve=0.0)
    c.tokens = 0.0
    assert not c.can_grant(1, Priority.INTERACTIVE)


# ---------------- strict priority arbitration ----------------

def test_backlogged_higher_class_starves_every_lower_class():
    c = _core()
    granted = c.step(now=0.0, demands={
        Priority.INTERACTIVE: 10**7,          # far more than capacity: backlogged
        Priority.DELTA: 1000,
        Priority.BACKFILL: 1000,
        Priority.SEEDING: 1000,
    })
    assert granted[Priority.INTERACTIVE] == 500_000     # all of capacity
    assert granted[Priority.DELTA] == 0
    assert granted[Priority.BACKFILL] == 0
    assert granted[Priority.SEEDING] == 0


def test_satisfied_higher_class_lets_lower_classes_through():
    c = _core()
    granted = c.step(now=0.0, demands={
        Priority.INTERACTIVE: 1000,
        Priority.BACKFILL: 1000,
    })
    assert granted[Priority.INTERACTIVE] == 1000
    assert granted[Priority.BACKFILL] == 1000


def test_seeding_gets_only_what_the_others_leave():
    """§5.2: 'Seeding gets leftover bandwidth, never reserved bandwidth.'"""
    c = _core()
    busy = c.step(now=0.0, demands={Priority.BACKFILL: 10**7, Priority.SEEDING: 1000})
    assert busy[Priority.SEEDING] == 0

    idle = _core()
    quiet = idle.step(now=0.0, demands={Priority.SEEDING: 1000})
    assert quiet[Priority.SEEDING] == 1000                # whole link when nothing competes


def test_lower_classes_resume_within_one_refill_period_after_the_backlog_clears():
    """The honest restatement of 'class shares converge to policy': under
    strict priority the policy IS (higher takes all), so what must be asserted
    is that lower classes are not permanently wedged."""
    c = _core()
    for tick in range(10):                    # 1 s of saturation by interactive
        c.step(now=tick * 0.1, demands={Priority.INTERACTIVE: 10**7,
                                         Priority.BACKFILL: 100_000})
    assert c.granted[Priority.BACKFILL] == 0

    # interactive stops; backfill is served on the very next round
    granted = c.step(now=1.1, demands={Priority.BACKFILL: 50_000})
    assert granted[Priority.BACKFILL] > 0


# ---------------- goodput ----------------

def test_long_run_goodput_stays_within_5_percent_of_rate():
    """Acceptance: 'sum of goodput never exceeds bucket rate ±5%'. Measured as
    a long-run average, which is the honest form: accounting is pay-after, so
    an instantaneous ceiling is unachievable by any userspace shaper."""
    c = _core(rate=RATE)
    c.tokens = 0.0                            # start empty so the burst isn't counted
    duration, dt = 20.0, 0.01
    steps = int(duration / dt)
    for i in range(1, steps + 1):
        c.step(now=i * dt, demands={Priority.BACKFILL: 10**7})   # saturating demand
    delivered = c.granted[Priority.BACKFILL]
    expected = RATE * duration
    assert delivered == pytest.approx(expected, rel=0.05)


def test_goodput_bound_holds_across_a_schedule_step_down():
    """The one place careless refill ordering breaks the bound: a window flip
    must not retroactively reprice the elapsed interval."""
    c = _core(rate=RATE)
    c.tokens = 0.0
    for i in range(1, 1001):                  # 10 s at full rate
        c.step(now=i * 0.01, demands={Priority.BACKFILL: 10**7})
    c.set_rate(RATE * 0.3, now=10.0)          # 'else 30%' kicks in
    for i in range(1, 1001):                  # 10 s at 30%
        c.step(now=10.0 + i * 0.01, demands={Priority.BACKFILL: 10**7})
    expected = RATE * 10.0 + RATE * 0.3 * 10.0
    assert c.granted[Priority.BACKFILL] == pytest.approx(expected, rel=0.05)


# ---------------- leases (rsync) ----------------

def test_lease_lowers_the_effective_rate_and_release_restores_it():
    c = _core(rate=RATE)
    c.lease(400_000, now=0.0)
    assert c.rate == pytest.approx(600_000)
    assert c.leased == pytest.approx(400_000)
    c.release(400_000, now=1.0)
    assert c.rate == pytest.approx(RATE)
    assert c.leased == pytest.approx(0.0)


def test_lease_cannot_drive_the_rate_negative():
    c = _core(rate=RATE)
    c.lease(RATE * 5, now=0.0)
    assert c.rate == pytest.approx(0.0)
    assert not c.can_grant(1, Priority.BACKFILL)


def test_over_release_is_clamped_not_negative():
    c = _core(rate=RATE)
    c.lease(100_000, now=0.0)
    c.release(999_999_999, now=1.0)
    assert c.leased == pytest.approx(0.0)
    assert c.rate == pytest.approx(RATE)


def test_reserve_fraction_must_be_a_valid_fraction():
    for bad in (1.0, 1.5, -0.1):
        with pytest.raises(ValueError):
            BucketCore(RATE, reserve_fraction=bad)
