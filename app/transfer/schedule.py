"""Rate strings and cron-style schedule windows (architecture-v2.md §5.2).

Pure functions — no clock, no locks, no I/O. The manager asks
`schedule.factor_at(clock.wall_minutes())` and multiplies the configured
ceiling by the result.

Two grammars:

  rate    "400Mbit" | "40 Mbit" | "500KB" | "2MiB" | "1000000"
          Bit units are SI (Mbit = 10^6 bits); byte units come in both SI
          (KB/MB/GB = 10^3) and binary (KiB/MiB/GiB = 2^10) so neither
          convention is guessed at. A bare number is bytes/second.

  window  "01:00-07:00 full; else 30%"
          Semicolon-separated clauses, first match wins in declaration order.
          A clause is "HH:MM-HH:MM <factor>" or "else <factor>". A window
          whose start is after its end wraps midnight ("22:00-06:00").
          Factors: "full" (1.0), "off" (0.0), or "N%".

Everything here is expressed in BYTES PER SECOND, matching the token bucket.

DST caveat, deliberately not papered over: windows are wall-clock local time,
so on a spring-forward day a 01:00-07:00 window loses an hour and on
fall-back it replays one. That is the correct reading of a wall-clock
schedule (an operator writing "01:00-07:00" means those local hours, whatever
the offset), and it is why refill uses monotonic() while only the window
lookup uses wall time — a DST step can never mint or destroy tokens.
"""
import re
from dataclasses import dataclass

_MINUTES_PER_DAY = 24 * 60

_RATE_UNITS = {
    "": 1,
    "b": 1, "byte": 1, "bytes": 1,
    "kb": 1_000, "mb": 1_000_000, "gb": 1_000_000_000,
    "kib": 1 << 10, "mib": 1 << 20, "gib": 1 << 30,
}
# bit-per-second units, converted to bytes/s by /8
_BIT_UNITS = {
    "bit": 1, "bps": 1,
    "kbit": 1_000, "mbit": 1_000_000, "gbit": 1_000_000_000,
}

_RATE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")
_WINDOW_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s+(.+?)\s*$")
_ELSE_RE = re.compile(r"^\s*else\s+(.+?)\s*$", re.IGNORECASE)


class ScheduleError(ValueError):
    """Malformed rate or schedule string. Raised at config-load time so a
    typo in platform.toml surfaces at boot, not at 01:00 six weeks later."""


def parse_rate(value: str | int | float | None) -> int | None:
    """Rate string -> bytes/second. None/"" -> None (meaning unlimited)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value < 0:
            raise ScheduleError(f"rate cannot be negative: {value!r}")
        return int(value)
    text = value.strip()
    if not text:
        return None
    m = _RATE_RE.match(text)
    if not m:
        raise ScheduleError(f"cannot parse rate {value!r} (try '400Mbit', '50MB', '1000000')")
    number, unit = float(m.group(1)), m.group(2).lower()
    if number < 0:
        raise ScheduleError(f"rate cannot be negative: {value!r}")
    if unit in _BIT_UNITS:
        return int(number * _BIT_UNITS[unit] / 8)
    if unit in _RATE_UNITS:
        return int(number * _RATE_UNITS[unit])
    raise ScheduleError(
        f"unknown rate unit {m.group(2)!r} in {value!r} — "
        f"expected one of: bit/kbit/Mbit/Gbit, B/KB/MB/GB, KiB/MiB/GiB")


def _parse_factor(text: str) -> float:
    t = text.strip().lower()
    if t == "full":
        return 1.0
    if t == "off":
        return 0.0
    if t.endswith("%"):
        try:
            pct = float(t[:-1])
        except ValueError:
            raise ScheduleError(f"cannot parse percentage {text!r}") from None
        if not 0.0 <= pct <= 100.0:
            raise ScheduleError(f"percentage out of range in {text!r} (expected 0-100)")
        return pct / 100.0
    raise ScheduleError(
        f"cannot parse schedule factor {text!r} — expected 'full', 'off', or 'N%'")


def _parse_hhmm(hours: str, minutes: str, source: str) -> int:
    h, m = int(hours), int(minutes)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ScheduleError(f"invalid time {h:02d}:{m:02d} in {source!r}")
    return h * 60 + m


@dataclass(frozen=True)
class Schedule:
    """Ordered windows plus the fallback factor for uncovered times."""
    windows: tuple[tuple[int, int, float], ...] = ()   # (start_min, end_min, factor)
    default_factor: float = 1.0

    def factor_at(self, wall_minutes: int) -> float:
        minute = wall_minutes % _MINUTES_PER_DAY
        for start, end, factor in self.windows:
            if start <= end:
                if start <= minute < end:
                    return factor
            else:
                # wraps midnight, e.g. 22:00-06:00
                if minute >= start or minute < end:
                    return factor
        return self.default_factor


ALWAYS_FULL = Schedule()


def parse_schedule(value: str | None) -> Schedule:
    """'01:00-07:00 full; else 30%' -> Schedule. None/"" -> always full."""
    if value is None or not value.strip():
        return ALWAYS_FULL
    windows: list[tuple[int, int, float]] = []
    default_factor = 1.0
    seen_else = False
    for clause in value.split(";"):
        if not clause.strip():
            continue
        m_else = _ELSE_RE.match(clause)
        if m_else:
            if seen_else:
                raise ScheduleError(f"more than one 'else' clause in {value!r}")
            default_factor = _parse_factor(m_else.group(1))
            seen_else = True
            continue
        m_win = _WINDOW_RE.match(clause)
        if not m_win:
            raise ScheduleError(
                f"cannot parse schedule clause {clause.strip()!r} in {value!r} — "
                f"expected 'HH:MM-HH:MM <factor>' or 'else <factor>'")
        start = _parse_hhmm(m_win.group(1), m_win.group(2), clause)
        end = _parse_hhmm(m_win.group(3), m_win.group(4), clause)
        if start == end:
            raise ScheduleError(
                f"zero-length window in {clause.strip()!r} — a window covering the "
                f"whole day should be written as 'else <factor>'")
        windows.append((start, end, _parse_factor(m_win.group(5))))
    return Schedule(windows=tuple(windows), default_factor=default_factor)


@dataclass(frozen=True)
class NamedSchedule:
    """The window grammar above with NAMES in place of factors — the node's
    profiles (platform.toml `[schedule] profile`, docs/notes/node-profiles-design.md):
    "06:00-08:00 quiet; 08:00-12:00 day; else night"."""
    windows: tuple[tuple[int, int, str], ...] = ()
    default: str | None = None

    def value_at(self, wall_minutes: int) -> str | None:
        minute = wall_minutes % _MINUTES_PER_DAY
        for start, end, name in self.windows:
            if (start <= minute < end) if start <= end else (minute >= start or minute < end):
                return name
        return self.default


def parse_named(value: str, names: frozenset[str] | set[str]) -> NamedSchedule:
    """'06:00-08:00 quiet; else night' -> NamedSchedule; every name must be one
    of `names`, and the windows plus `else` must cover the whole day."""
    windows: list[tuple[int, int, str]] = []
    default: str | None = None
    for clause in (value or "").split(";"):
        if not clause.strip():
            continue
        m_else = _ELSE_RE.match(clause)
        m_win = None if m_else else _WINDOW_RE.match(clause)
        name = (m_else or m_win).group(1 if m_else else 5).strip() if (m_else or m_win) else None
        if name is None:
            raise ScheduleError(f"cannot parse clause {clause.strip()!r} in {value!r} — "
                                f"expected 'HH:MM-HH:MM <name>' or 'else <name>'")
        if name not in names:
            raise ScheduleError(f"{name!r} in {value!r} is not a profile; the profiles are "
                                f"{', '.join(sorted(names))}")
        if m_else:
            if default is not None:
                raise ScheduleError(f"more than one 'else' clause in {value!r}")
            default = name
            continue
        start = _parse_hhmm(m_win.group(1), m_win.group(2), clause)
        end = _parse_hhmm(m_win.group(3), m_win.group(4), clause)
        if start == end:
            raise ScheduleError(f"zero-length window in {clause.strip()!r}")
        windows.append((start, end, name))
    out = NamedSchedule(windows=tuple(windows), default=default)
    if default is None and any(out.value_at(m) is None for m in range(_MINUTES_PER_DAY)):
        raise ScheduleError(f"{value!r} leaves part of the day without a profile: add 'else <name>'")
    return out
