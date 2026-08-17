"""Injectable clock. The dangerous parts of a fake clock are the ways it can
silently hang a suite, so those are what's asserted hardest here — there is no
pytest-timeout in this repo, so a spin is a permanently hung CI job.
"""
import threading

import pytest

from app.transfer.clock import FakeClock, RealClock


def test_real_clock_monotonic_advances_and_wall_minutes_in_range():
    c = RealClock()
    t0 = c.monotonic()
    assert c.monotonic() >= t0
    assert 0 <= c.wall_minutes() < 24 * 60


def test_fake_clock_monotonic_only_moves_when_told():
    c = FakeClock(start=100.0)
    assert c.monotonic() == 100.0
    assert c.monotonic() == 100.0        # no wall-clock leakage
    c.advance(2.5)
    assert c.monotonic() == 102.5


def test_fake_clock_wall_minutes_derives_from_the_same_counter():
    """Both time bases share one counter, so simulated time-travel genuinely
    crosses schedule boundaries — otherwise window tests are unwritable."""
    c = FakeClock(start=0.0, start_wall_minutes=59)       # 00:59
    assert c.wall_minutes() == 59
    c.advance(60)                                          # +1 min -> 01:00
    assert c.wall_minutes() == 60
    c.advance(3 * 3600)                                    # +3h -> 04:00
    assert c.wall_minutes() == 4 * 60


def test_fake_clock_wall_minutes_wraps_at_midnight():
    c = FakeClock(start=0.0, start_wall_minutes=23 * 60 + 59)
    assert c.wall_minutes() == 23 * 60 + 59
    c.advance(120)                                         # +2 min past midnight
    assert c.wall_minutes() == 1


def test_fake_clock_cannot_go_backwards():
    c = FakeClock()
    with pytest.raises(ValueError):
        c.advance(-1)


def test_fake_clock_wait_advances_time():
    c = FakeClock()
    cv = threading.Condition()
    with cv:
        c.wait(cv, 0.25)
    assert c.monotonic() == 0.25
    assert c.waits == 1


def test_fake_clock_wait_rejects_non_advancing_timeouts():
    """A zero/negative/None/inf timeout would spin at zero simulated time
    forever. Each must raise rather than hang."""
    c = FakeClock()
    cv = threading.Condition()
    with cv:
        with pytest.raises(TypeError):
            c.wait(cv, None)
        for bad in (0, -1.0, float("inf")):
            with pytest.raises(ValueError):
                c.wait(cv, bad)


def test_fake_clock_wait_budget_catches_livelock():
    """A caller that never makes progress trips a ceiling instead of hanging
    the suite."""
    c = FakeClock(max_waits=10)
    cv = threading.Condition()
    with cv:
        with pytest.raises(AssertionError, match="not making progress"):
            for _ in range(50):
                c.wait(cv, 0.001)
