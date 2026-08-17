"""TransferManager — rate composition, etiquette reconciliation, EWMA mirror
steering, and rsync lease sizing. FakeClock throughout: no threads needed for
any of these properties.
"""
import threading

import pytest

from app import platform_config
from app.transfer import schedule as sched
from app.transfer.bucket import Priority
from app.transfer.clock import FakeClock
from app.transfer.manager import Direction, TransferManager, current_handle

MB = 1_000_000


def _platform(down="10MB", up="1MB", schedule_text="", reserve=0.2,
              workers=8, per_endpoint=4):
    return platform_config.PlatformConfig(
        bandwidth=platform_config.BandwidthConfig(
            down_max_bps=sched.parse_rate(down),
            up_max_bps=sched.parse_rate(up),
            reserve_interactive=reserve,
            schedule=sched.parse_schedule(schedule_text),
            schedule_text=schedule_text),
        transfer=platform_config.TransferConfig(workers=workers, per_endpoint=per_endpoint))


def _manager(platform=None, clock=None):
    return TransferManager(platform=platform or _platform(), clock=clock or FakeClock())


class _Endpoint:
    def __init__(self, max_conns=4, url="http://mirror/"):
        self.max_conns = max_conns
        self.url = url


class _Etiquette:
    def __init__(self, max_conns=4):
        self.max_conns = max_conns


class _Spec:
    def __init__(self, max_conns=4):
        self.etiquette = _Etiquette(max_conns)


# ---------------- rate composition ----------------

def test_configured_ceiling_applies_to_both_directions():
    m = _manager(_platform(down="10MB", up="1MB"))
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)
    assert m.bucket(Direction.UP).core.rate == pytest.approx(1 * MB)


def test_absent_config_means_unlimited_in_both_directions():
    m = TransferManager(platform=platform_config.PlatformConfig(), clock=FakeClock())
    assert m.bucket(Direction.DOWN).core.rate is None
    assert m.bucket(Direction.UP).core.rate is None


def test_schedule_factor_multiplies_the_ceiling():
    clock = FakeClock(start_wall_minutes=3 * 60)          # 03:00, inside the window
    m = _manager(_platform(down="10MB", schedule_text="01:00-07:00 full; else 30%"), clock)
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)

    clock_day = FakeClock(start_wall_minutes=13 * 60)     # 13:00, the else branch
    m2 = _manager(_platform(down="10MB", schedule_text="01:00-07:00 full; else 30%"), clock_day)
    assert m2.bucket(Direction.DOWN).core.rate == pytest.approx(3 * MB)


def test_effective_rate_is_the_minimum_of_schedule_and_override():
    m = _manager(_platform(down="10MB"))
    m.set_override(2 * MB)
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(2 * MB)

    m.set_override(50 * MB)          # an override ABOVE the ceiling cannot raise it
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)

    m.set_override(None)             # cleared -> back to the configured ceiling
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)


def test_override_alone_caps_an_otherwise_unlimited_link():
    m = TransferManager(platform=platform_config.PlatformConfig(), clock=FakeClock())
    m.set_override(5 * MB)
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(5 * MB)


def test_maybe_refresh_picks_up_a_schedule_window_flip():
    """The manager must notice 01:00 arriving without a background thread."""
    clock = FakeClock(start_wall_minutes=0)               # 00:00 -> else branch
    m = _manager(_platform(down="10MB", schedule_text="01:00-07:00 full; else 30%"), clock)
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(3 * MB)

    clock.advance(61 * 60)                                # 01:01
    m.maybe_refresh()
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)


def test_maybe_refresh_is_cheap_between_checks():
    clock = FakeClock(start_wall_minutes=0)
    m = _manager(_platform(down="10MB", schedule_text="01:00-07:00 full; else 30%"), clock)
    clock.advance(61 * 60)
    m.maybe_refresh()                                     # picks up the flip
    clock.advance(0.1)                                    # under the recheck interval
    m.maybe_refresh()
    assert m.bucket(Direction.DOWN).core.rate == pytest.approx(10 * MB)


def test_reserve_fraction_reaches_the_buckets():
    m = _manager(_platform(down="10MB", reserve=0.25))
    core = m.bucket(Direction.DOWN).core
    assert core.reserve == pytest.approx(core.capacity * 0.25)


# ---------------- etiquette: concurrency, honoring BOTH max_conns fields ----------------

def test_endpoint_slots_takes_the_minimum_of_all_three_limits():
    m = _manager(_platform(per_endpoint=4))
    assert m.endpoint_slots(_Endpoint(max_conns=4), _Spec(max_conns=4)) == 4
    assert m.endpoint_slots(_Endpoint(max_conns=2), _Spec(max_conns=4)) == 2   # Endpoint wins
    assert m.endpoint_slots(_Endpoint(max_conns=4), _Spec(max_conns=1)) == 1   # RateLimit wins

    tight = _manager(_platform(per_endpoint=1))
    assert tight.endpoint_slots(_Endpoint(max_conns=8), _Spec(max_conns=8)) == 1  # global wins


def test_endpoint_slots_never_returns_zero():
    m = _manager(_platform(per_endpoint=4))
    assert m.endpoint_slots(_Endpoint(max_conns=0), _Spec(max_conns=0)) >= 1


def test_endpoint_semaphore_is_stable_and_sized():
    m = _manager()
    sem1 = m.endpoint_semaphore("mirror-a", 2)
    sem2 = m.endpoint_semaphore("mirror-a", 2)
    assert sem1 is sem2                                   # same key -> same semaphore
    assert sem1.acquire(blocking=False)
    assert sem1.acquire(blocking=False)
    assert not sem1.acquire(blocking=False)               # capped at 2
    sem1.release()
    sem1.release()


# ---------------- EWMA mirror steering ----------------

def test_ewma_prefers_the_faster_mirror():
    m = _manager()
    m.record_throughput("slow", byte_count=1_000, seconds=1.0)      # 1 KB/s
    m.record_throughput("fast", byte_count=1_000_000, seconds=1.0)  # 1 MB/s
    ranked = m.rank_endpoints([("slow", _Endpoint()), ("fast", _Endpoint())])
    assert [k for k, _ in ranked] == ["fast", "slow"]


def test_unmeasured_endpoints_keep_declared_order_behind_measured_ones():
    """Cold start must stay polite: spec order is mirrors-first."""
    m = _manager()
    m.record_throughput("measured", byte_count=500, seconds=1.0)
    ranked = m.rank_endpoints([("cold-a", _Endpoint()), ("measured", _Endpoint()),
                                ("cold-b", _Endpoint())])
    assert ranked[0][0] == "measured"
    assert [k for k, _ in ranked[1:]] == ["cold-a", "cold-b"]


def test_ewma_smooths_rather_than_jumping():
    m = _manager()
    m.record_throughput("e", byte_count=1_000_000, seconds=1.0)
    m.record_throughput("e", byte_count=0, seconds=1.0)       # ignored: no bytes
    assert m.throughput("e") == pytest.approx(1_000_000)
    m.record_throughput("e", byte_count=2_000_000, seconds=1.0)
    assert 1_000_000 < m.throughput("e") < 2_000_000          # blended, not replaced


def test_throughput_ignores_degenerate_samples():
    m = _manager()
    m.record_throughput("e", byte_count=100, seconds=0)
    m.record_throughput("", byte_count=100, seconds=1)
    assert m.throughput("e") is None


# ---------------- rsync leases ----------------

def test_rsync_lease_lowers_the_rate_and_restores_it():
    m = _manager(_platform(down="10MB"))
    core = m.bucket(Direction.DOWN).core
    with m.rsync_lease("mirror") as bps:
        assert bps == pytest.approx(10 * MB)          # sole consumer -> whole budget
        assert core.rate == pytest.approx(0.0)
    assert core.rate == pytest.approx(10 * MB)


def test_rsync_lease_is_sized_by_measured_throughput_not_fair_share():
    """The under-utilisation trap: leasing a 10 MB/s fair share to a mirror
    that only delivers 300 KB/s would withhold and waste the difference."""
    m = _manager(_platform(down="10MB"))
    m.record_throughput("slow-mirror", byte_count=300_000, seconds=1.0)
    with m.rsync_lease("slow-mirror") as bps:
        assert bps == pytest.approx(300_000 * 1.25)   # measured + headroom, not 10 MB/s
        assert m.bucket(Direction.DOWN).core.rate > 9 * MB   # rest stays available


def test_rsync_lease_never_rounds_down_to_an_unlimited_bwlimit():
    """--bwlimit=0 means UNLIMITED to rsync, so the lease has a hard floor."""
    m = _manager(_platform(down="10MB"))
    m.record_throughput("glacial", byte_count=1, seconds=1000.0)
    with m.rsync_lease("glacial") as bps:
        assert bps >= 64 * 1024


def test_rsync_lease_is_released_even_when_the_body_raises():
    m = _manager(_platform(down="10MB"))
    core = m.bucket(Direction.DOWN).core
    with pytest.raises(RuntimeError):
        with m.rsync_lease("mirror"):
            raise RuntimeError("transport blew up")
    assert core.rate == pytest.approx(10 * MB)
    assert core.leased == pytest.approx(0.0)


def test_unlimited_budget_leases_nothing():
    m = TransferManager(platform=platform_config.PlatformConfig(), clock=FakeClock())
    with m.rsync_lease("mirror") as bps:
        assert bps is None                            # -> no --bwlimit flag at all


def test_concurrent_rsyncs_split_the_budget():
    m = _manager(_platform(down="10MB"))
    with m.rsync_lease("a") as first:
        with m.rsync_lease("b") as second:
            assert first == pytest.approx(10 * MB)
            assert second == pytest.approx(5 * MB)    # second sees one already active


# ---------------- priority binding ----------------

def test_bind_sets_and_restores_the_handle():
    m = _manager()
    assert current_handle() is None
    with m.bind(Priority.INTERACTIVE, Direction.DOWN):
        outer = current_handle()
        assert outer.priority is Priority.INTERACTIVE
        with m.bind(Priority.SEEDING, Direction.UP):
            inner = current_handle()
            assert inner.priority is Priority.SEEDING
            assert inner.bucket is m.bucket(Direction.UP)
        assert current_handle() is outer                # nesting composes
    assert current_handle() is None


def test_bind_carries_stop_and_endpoint():
    m = _manager()
    stop = threading.Event()
    with m.bind(Priority.BACKFILL, stop=stop, endpoint_key="mirror-a"):
        handle = current_handle()
        assert handle.stop is stop
        assert handle.endpoint_key == "mirror-a"


def test_bind_restores_on_exception():
    m = _manager()
    with pytest.raises(ValueError):
        with m.bind(Priority.DELTA):
            raise ValueError("boom")
    assert current_handle() is None


# ---------------- live rates ----------------

def test_rates_reports_per_class_bytes_per_second():
    clock = FakeClock()
    m = _manager(_platform(down="10MB"), clock)
    m.rates()                                          # establish the first sample
    m.bucket(Direction.DOWN).acquire(1_000_000, Priority.BACKFILL)
    clock.advance(1.0)
    payload = m.rates()
    assert payload["down"]["backfill"] == pytest.approx(1_000_000, rel=0.05)
    assert payload["down"]["interactive"] == 0.0


def test_rates_exposes_the_schedule_for_the_dashboard():
    clock = FakeClock(start_wall_minutes=13 * 60)
    m = _manager(_platform(down="10MB", schedule_text="01:00-07:00 full; else 30%"), clock)
    limits = m.rates()["limits"]
    assert limits["schedule"] == "01:00-07:00 full; else 30%"
    assert limits["schedule_factor"] == pytest.approx(0.30)
    assert limits["down"] == pytest.approx(3 * MB)


def test_rates_first_call_is_zeros_not_a_spike():
    m = _manager(_platform(down="10MB"))
    payload = m.rates()
    assert all(v == 0.0 for v in payload["down"].values())
