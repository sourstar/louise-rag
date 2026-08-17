"""TokenBucket — the blocking wrapper. Concurrency correctness only; policy is
proven in test_bucket_core.py with no threads at all.

EVERY test here carries its own watchdog. There is no pytest-timeout in this
repo, so a livelock or a lost wakeup would hang CI permanently rather than
fail — and this session has already lost one run to exactly that.
"""
import threading

import pytest

from app.transfer.bucket import Priority, TokenBucket, TransferAborted
from app.transfer.clock import FakeClock

WATCHDOG = 10.0


def run_bounded(fn, seconds: float = WATCHDOG):
    """Run fn in a thread; fail (never hang) if it does not finish in time."""
    box = {}

    def _target():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 - re-raised on the main thread
            box["error"] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"thread did not finish within {seconds}s — livelock or lost wakeup"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def wait_until(predicate, seconds: float = WATCHDOG):
    deadline = threading.Event()
    step = 0.005
    waited = 0.0
    while waited < seconds:
        if predicate():
            return True
        deadline.wait(step)
        waited += step
    return False


# ---------------- the unlimited default ----------------

def test_unlimited_bucket_never_blocks_and_still_counts():
    """No platform.toml -> unlimited. The existing acquisition suite runs
    through this path, so it must be free of blocking and of surprises."""
    b = TokenBucket(None)

    def _work():
        for _ in range(1000):
            b.acquire(1 << 16, Priority.BACKFILL)
        return True

    assert run_bounded(_work) is True
    _, granted = b.granted_snapshot()
    assert granted[Priority.BACKFILL] == 1000 * (1 << 16)


# ---------------- pacing, deterministic via FakeClock ----------------

def test_throttled_acquire_advances_the_clock_by_the_deficit():
    clock = FakeClock()
    b = TokenBucket(1000, clock=clock, burst_seconds=0.5, reserve_fraction=0.2)
    b.acquire(500, Priority.BACKFILL)             # drains the initial burst
    assert clock.monotonic() == 0.0               # was already affordable

    b.acquire(500, Priority.BACKFILL)             # must now wait for refill
    assert clock.monotonic() == pytest.approx(0.5, abs=1e-6)


def test_repeated_acquires_converge_on_the_configured_rate():
    clock = FakeClock()
    rate = 100_000
    b = TokenBucket(rate, clock=clock, burst_seconds=0.5, reserve_fraction=0.2)
    chunk, count = 1 << 16, 40
    for _ in range(count):
        b.acquire(chunk, Priority.BACKFILL)
    elapsed = clock.monotonic()
    observed = (chunk * count) / elapsed
    assert observed == pytest.approx(rate, rel=0.10)


def test_rate_zero_blocks_until_the_rate_changes():
    """A schedule 'off' window must actually stop bytes. The waiter is woken
    by set_rate's notify_all, not by a timeout."""
    b = TokenBucket(0, burst_seconds=0.5)
    started = threading.Event()
    done = threading.Event()

    def _waiter():
        started.set()
        b.acquire(1000, Priority.BACKFILL)
        done.set()

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    assert started.wait(WATCHDOG)
    assert not done.wait(0.2), "acquire must not succeed while the rate is zero"

    b.set_rate(1_000_000)
    assert done.wait(WATCHDOG), "set_rate must wake the waiter"
    t.join(WATCHDOG)
    assert not t.is_alive()


# ---------------- strict priority ----------------

def test_lower_class_cannot_jump_ahead_of_a_waiting_higher_class():
    """The property B-6 protects: a late low-priority arrival must not take
    tokens a sleeping interactive waiter is owed, even once refill has made
    the small request individually affordable."""
    # rate 10_000 B/s -> capacity 5_000, reserve 1_000. Drain to empty so the
    # interactive request genuinely parks (a request is granted the moment the
    # bucket is FULL, incurring debt — see BucketCore's rule 1), while leaving
    # the deficit small enough that the tiny backfill request below becomes
    # individually affordable well within the test window. Backfill staying
    # blocked therefore isolates PRIORITY, not mere token scarcity.
    b = TokenBucket(10_000, burst_seconds=0.5, reserve_fraction=0.2)
    b.acquire(5_000, Priority.BACKFILL)        # tokens -> 0
    stop_hi, stop_lo = threading.Event(), threading.Event()
    lo_result = {}

    def _interactive():
        try:
            b.acquire(10_000_000, Priority.INTERACTIVE, stop=stop_hi)   # long backlog
        except TransferAborted:
            pass

    def _backfill():
        try:
            b.acquire(10, Priority.BACKFILL, stop=stop_lo)
            lo_result["granted"] = True
        except TransferAborted:
            lo_result["aborted"] = True

    hi = threading.Thread(target=_interactive, daemon=True)
    hi.start()
    assert wait_until(lambda: b._waiting[Priority.INTERACTIVE] == 1), "interactive never parked"

    lo = threading.Thread(target=_backfill, daemon=True)
    lo.start()
    # 10 bytes above the 1_000 reserve needs ~0.101 s of refill; the parked
    # interactive request needs ~0.5 s. So at 0.3 s the backfill request is
    # affordable on its own and is still waiting only because a higher class is.
    stop_lo.wait(0.3)
    stop_lo.set()
    b.wake_all()
    lo.join(WATCHDOG)
    assert not lo.is_alive()
    assert lo_result.get("aborted"), "backfill jumped ahead of a waiting interactive class"

    stop_hi.set()
    b.wake_all()
    hi.join(WATCHDOG)
    assert not hi.is_alive()


def test_departing_waiter_releases_lower_classes():
    """A waiter leaving for ANY reason must decrement its class count and
    notify_all, or lower classes sleep out their full timeouts."""
    b = TokenBucket(1_000_000, burst_seconds=0.5, reserve_fraction=0.2)
    b.acquire(500_000, Priority.BACKFILL)      # drain, so the waiter below parks
    stop_hi = threading.Event()
    hi_parked = threading.Event()

    def _interactive():
        hi_parked.set()
        try:
            b.acquire(10_000_000, Priority.INTERACTIVE, stop=stop_hi)
        except TransferAborted:
            pass

    hi = threading.Thread(target=_interactive, daemon=True)
    hi.start()
    assert hi_parked.wait(WATCHDOG)
    assert wait_until(lambda: b._waiting[Priority.INTERACTIVE] == 1)

    stop_hi.set()
    b.wake_all()
    hi.join(WATCHDOG)
    assert not hi.is_alive()
    assert b._waiting[Priority.INTERACTIVE] == 0

    # with the higher class gone, backfill proceeds
    assert run_bounded(lambda: b.acquire(1000, Priority.BACKFILL) or True) is True


# ---------------- stop event ----------------

def test_stop_event_aborts_a_blocked_acquire_promptly():
    """On SIGTERM most threads are asleep INSIDE the bucket, not in a
    transport — so the stop check has to live in the wait predicate."""
    b = TokenBucket(1000, burst_seconds=0.5, reserve_fraction=0.2)
    b.acquire(500, Priority.BACKFILL)          # drain
    stop = threading.Event()
    outcome = {}

    def _waiter():
        try:
            b.acquire(10_000_000, Priority.BACKFILL, stop=stop)
        except TransferAborted:
            outcome["aborted"] = True

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    assert wait_until(lambda: b._waiting[Priority.BACKFILL] == 1)
    stop.set()
    b.wake_all()
    t.join(WATCHDOG)
    assert not t.is_alive(), "stop event did not wake a blocked waiter"
    assert outcome.get("aborted")


def test_already_set_stop_event_aborts_without_waiting():
    b = TokenBucket(1000, burst_seconds=0.5)
    b.acquire(500, Priority.BACKFILL)
    stop = threading.Event()
    stop.set()

    def _work():
        with pytest.raises(TransferAborted):
            b.acquire(10_000_000, Priority.BACKFILL, stop=stop)
        return True

    assert run_bounded(_work, seconds=2.0) is True


def test_abort_does_not_strand_the_waiter_count():
    b = TokenBucket(1000, burst_seconds=0.5)
    b.acquire(500, Priority.BACKFILL)
    stop = threading.Event()
    stop.set()

    def _work():
        with pytest.raises(TransferAborted):
            b.acquire(10_000_000, Priority.DELTA, stop=stop)
        return True

    run_bounded(_work, seconds=2.0)
    assert b._waiting[Priority.DELTA] == 0, "aborted waiter left its class count incremented"


# ---------------- leases ----------------

def test_rate_lease_reduces_the_effective_rate_and_restores_on_exit():
    b = TokenBucket(1_000_000, burst_seconds=0.5)
    with b.lease_rate(400_000) as lease:
        assert lease.bps == 400_000
        assert b.core.rate == pytest.approx(600_000)
    assert b.core.rate == pytest.approx(1_000_000)


def test_lease_release_is_idempotent():
    b = TokenBucket(1_000_000, burst_seconds=0.5)
    lease = b.lease_rate(250_000)
    lease.release()
    lease.release()                            # a double release must not over-credit
    assert b.core.rate == pytest.approx(1_000_000)
    assert b.core.leased == pytest.approx(0.0)


def test_zero_and_negative_acquires_are_no_ops():
    b = TokenBucket(1000, burst_seconds=0.5)
    assert run_bounded(lambda: (b.acquire(0, Priority.BACKFILL),
                                 b.acquire(-5, Priority.BACKFILL), True)[-1], seconds=2.0)
