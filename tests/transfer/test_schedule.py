"""Rate strings and schedule windows — pure functions, no clock, no threads."""
import pytest

from app.transfer import schedule as sched


# ---------------- rate strings ----------------

@pytest.mark.parametrize("text,expected", [
    ("400Mbit", 50_000_000),        # 400e6 bits / 8
    ("40Mbit", 5_000_000),
    ("1Gbit", 125_000_000),
    ("8bit", 1),
    ("1000000", 1_000_000),         # bare number = bytes/s
    ("500KB", 500_000),             # SI byte units
    ("2MB", 2_000_000),
    ("1KiB", 1024),                 # binary byte units
    ("2MiB", 2 * 1024 * 1024),
    ("  400 Mbit  ", 50_000_000),   # whitespace tolerated
    ("1.5Mbit", 187_500),
])
def test_parse_rate(text, expected):
    assert sched.parse_rate(text) == expected


def test_parse_rate_none_and_empty_mean_unlimited():
    assert sched.parse_rate(None) is None
    assert sched.parse_rate("") is None
    assert sched.parse_rate("   ") is None


@pytest.mark.parametrize("bad", ["400Furlongs", "Mbit", "-5", "-5Mbit", "4 0 0"])
def test_parse_rate_rejects_garbage(bad):
    with pytest.raises(sched.ScheduleError):
        sched.parse_rate(bad)


def test_bit_and_byte_units_are_not_confused():
    """The classic bandwidth bug: 400Mbit is 50 MB/s, not 400 MB/s."""
    assert sched.parse_rate("400Mbit") == sched.parse_rate("50MB")


# ---------------- schedule windows ----------------

def _at(text, hh, mm):
    return sched.parse_schedule(text).factor_at(hh * 60 + mm)


def test_doc_example_schedule():
    s = "01:00-07:00 full; else 30%"
    assert _at(s, 0, 59) == 0.30
    assert _at(s, 1, 0) == 1.0      # window is half-open: start inclusive
    assert _at(s, 6, 59) == 1.0
    assert _at(s, 7, 0) == 0.30     # ...end exclusive
    assert _at(s, 23, 59) == 0.30


def test_absent_schedule_is_always_full():
    for value in (None, "", "   "):
        assert sched.parse_schedule(value).factor_at(0) == 1.0
        assert sched.parse_schedule(value).factor_at(13 * 60) == 1.0


def test_window_wrapping_midnight():
    s = "22:00-06:00 full; else 25%"
    assert _at(s, 22, 0) == 1.0
    assert _at(s, 23, 59) == 1.0
    assert _at(s, 0, 0) == 1.0      # across the boundary
    assert _at(s, 5, 59) == 1.0
    assert _at(s, 6, 0) == 0.25
    assert _at(s, 12, 0) == 0.25


def test_no_else_clause_defaults_to_full():
    s = "01:00-07:00 30%"
    assert _at(s, 2, 0) == 0.30
    assert _at(s, 12, 0) == 1.0


def test_first_matching_window_wins():
    s = "01:00-07:00 full; 02:00-03:00 off; else 50%"
    assert _at(s, 2, 30) == 1.0     # earlier clause shadows the later one
    assert _at(s, 8, 0) == 0.50


def test_off_and_percentage_factors():
    s = "00:00-06:00 off; 06:00-12:00 10%; else full"
    assert _at(s, 3, 0) == 0.0
    assert _at(s, 7, 0) == 0.10
    assert _at(s, 18, 0) == 1.0


def test_factor_at_normalizes_out_of_range_minutes():
    s = sched.parse_schedule("01:00-07:00 full; else 30%")
    assert s.factor_at(24 * 60 + 2 * 60) == 1.0    # wraps to 02:00 next day
    assert s.factor_at(-60) == 0.30                # wraps to 23:00


@pytest.mark.parametrize("bad", [
    "01:00-07:00",                  # missing factor
    "1am-7am full",                 # not HH:MM
    "25:00-07:00 full",             # hour out of range
    "01:70-07:00 full",             # minute out of range
    "03:00-03:00 full",             # zero-length window
    "01:00-07:00 sideways",         # unparseable factor
    "01:00-07:00 150%",             # percentage out of range
    "else 10%; else 20%",           # two else clauses
])
def test_parse_schedule_rejects_garbage(bad):
    with pytest.raises(sched.ScheduleError):
        sched.parse_schedule(bad)


def test_dst_behavior_is_wall_clock_by_design():
    """Windows are wall-clock local time, so a spring-forward day genuinely
    loses an hour of the window and fall-back replays one. This is the correct
    reading of an operator writing '01:00-07:00' and is why token REFILL uses
    monotonic() — a DST step must never mint or destroy tokens. This test
    documents the intent; the guarantee it protects lives in the bucket.
    """
    s = sched.parse_schedule("01:00-07:00 full; else 30%")
    # 02:30 is inside the window whether or not that local hour existed twice
    assert s.factor_at(2 * 60 + 30) == 1.0
