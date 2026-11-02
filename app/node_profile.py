"""The node's profiles — quiet, day, night — declared once in platform.toml
(docs/notes/node-profiles-design.md; operator, 2026-10-27: "1 and 2 your
recommendations, 3 full local/p2p, the whole point of quiet gpu is to use it").

    [schedule]
    profile = "06:00-08:00 quiet; 08:00-12:00 day; 12:00-13:30 quiet; 13:30-16:30 day; 16:30-22:30 quiet; else night"
    sat = "..."                       # optional, per weekday (mon..sun)

    [profiles.quiet]  cpu_cores = 0   gpu = "full"  mode = "acquire-only"
    [profiles.day]    cpu_cores = 12  gpu = "full"
    [profiles.night]  cpu_cores = 12  gpu = "full"

    [gpu.levels.full]   power_limit = "max"
    [gpu.levels.quiet]  power_limit = "50%"  max_clock = "50%"  fan_max_rpm = 1200

A profile limits BACKGROUND CPU work (the records build's and the worker's
parse pools: `cpu_cores`, 0 = paused at the end of the current unit), sets
the card's level, and may hold the node to a stricter MODE than its own
(`mode`, one of app/node_state.py's MODE_RUNS: running, acquire-only,
seed-only, paused). The operator, 2026-11-02: "During the day keep gpu at
full power, suspend processing during the quiet time frames in the morning,
at lunch time and in the evening" — so the quiet profile is
`mode = "acquire-only"`: nothing is parsed, indexed or embedded, downloads
and seeding go on (node-profiles-design.md decision 6, superseding decision
3's "embedding runs in every profile"). A profile never limits a query.

Desired state, Tier 2 (invariant 10): nothing here is written to a
database; whoever applies a profile reads it with `active()`. Precedence:
the STRICTER of the node's mode (`node pause`, the Library's choice) and the
profile's mode wins — `node_state.runs` asks both, so every gate agrees.

The window grammar and its parser are `[bandwidth] schedule`'s
(app/transfer/schedule.py), wall-clock local time; the standard alternatives
— `tuned` profiles, power-profiles-daemon — are written down in the design
note with why they do not fit this host.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .transfer import schedule as schedule_mod

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
#: a level's limit: "max" (the card's own), "N%" of the card's maximum, or watts / MHz
_LIMIT = re.compile(r"^(max|\d{1,3}%|\d+(\.\d+)?\s*(W|MHz))$", re.IGNORECASE)


class ProfileError(schedule_mod.ScheduleError):
    """A malformed [schedule] / [profiles] / [gpu.levels]: refused at load."""


@dataclass(frozen=True)
class GpuLevel:
    name: str
    power_limit: str = "max"            # hwmon power1_max: "max", "N%", "NW"
    max_clock: str = "max"              # max_freq, where no power limit is writable: "max", "N%", "NMHz"
    fan_max_rpm: int | None = None      # only where the driver lets software set the fan


@dataclass(frozen=True)
class Profile:
    name: str
    cpu_cores: int | None               # None: no limit below the cgroup quota
    gpu: str                            # a GpuLevel's name
    mode: str = "running"               # node_state.MODE_RUNS key: the most this profile lets run


@dataclass(frozen=True)
class Active:
    profile: Profile
    level: GpuLevel
    until: datetime | None              # the next change, None if the profile never changes
    next: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"profile": self.profile.name, "cpu_cores": self.profile.cpu_cores,
                "mode": self.profile.mode,
                "gpu": {"level": self.level.name, "power_limit": self.level.power_limit,
                        "max_clock": self.level.max_clock, "fan_max_rpm": self.level.fan_max_rpm},
                "until": self.until.isoformat(timespec="minutes") if self.until else None,
                "next": self.next}


@dataclass(frozen=True)
class Profiles:
    schedule: schedule_mod.NamedSchedule
    days: dict[int, schedule_mod.NamedSchedule] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    levels: dict[str, GpuLevel] = field(default_factory=dict)

    def name_at(self, when: datetime) -> str:
        sched = self.days.get(when.weekday(), self.schedule)
        return sched.value_at(when.hour * 60 + when.minute)

    def active(self, now: datetime | None = None) -> Active:
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        name = self.name_at(now)
        until = nxt = None
        t = now
        for _ in range(8 * 24 * 60):         # a week and a day: every weekday line seen
            t += timedelta(minutes=1)
            other = self.name_at(t)
            if other != name:
                until, nxt = t, other
                break
        p = self.profiles[name]
        return Active(profile=p, level=self.levels[p.gpu], until=until, next=nxt)


def _limit(value: Any, key: str, unit: str) -> str:
    """`unit` is the one absolute unit this key takes: W for power_limit,
    MHz for max_clock. A power limit in MHz was accepted until the applier
    (app/gpu_level.py) had to read it, 2026-10-27; refused at load since."""
    text = str(value).strip()
    m = _LIMIT.match(text)
    if not m or (m.group(3) and m.group(3).lower() != unit.lower()):
        raise ProfileError(f"{key} is \"max\", \"N%\" or a number of {unit}, got {value!r}")
    if text.endswith("%") and not 1 <= int(text[:-1]) <= 100:
        raise ProfileError(f"{key} percentage is 1-100, got {value!r}")
    return text


def parse(raw: dict[str, Any]) -> Profiles | None:
    """platform.toml's tables -> Profiles; None when the file declares no
    `[schedule]` (no profile: nothing limited, today's behaviour)."""
    sched_raw = raw.get("schedule")
    if not sched_raw:
        return None
    if not isinstance(sched_raw, dict) or not isinstance(sched_raw.get("profile"), str):
        raise ProfileError("[schedule] profile is a window string: \"06:00-08:00 quiet; else night\"")
    levels: dict[str, GpuLevel] = {"full": GpuLevel("full")}
    for name, lv in ((raw.get("gpu") or {}).get("levels") or {}).items():
        if not isinstance(lv, dict):
            raise ProfileError(f"[gpu.levels.{name}] is a table")
        unknown = set(lv) - {"power_limit", "max_clock", "fan_max_rpm"}
        if unknown:
            raise ProfileError(f"[gpu.levels.{name}] takes power_limit, max_clock, fan_max_rpm; not {sorted(unknown)}")
        rpm = lv.get("fan_max_rpm")
        if rpm is not None and (not isinstance(rpm, int) or isinstance(rpm, bool) or rpm <= 0):
            raise ProfileError(f"[gpu.levels.{name}] fan_max_rpm is a positive number of RPM")
        levels[name] = GpuLevel(name, _limit(lv.get("power_limit", "max"), f"[gpu.levels.{name}] power_limit", "W"),
                                _limit(lv.get("max_clock", "max"), f"[gpu.levels.{name}] max_clock", "MHz"), rpm)
    from .node_state import MODE_RUNS       # here: node_state's imports reach the database
    modes = tuple(MODE_RUNS)
    profiles: dict[str, Profile] = {}
    for name, p in (raw.get("profiles") or {}).items():
        if not isinstance(p, dict):
            raise ProfileError(f"[profiles.{name}] is a table")
        unknown = set(p) - {"cpu_cores", "gpu", "mode"}
        if unknown:
            raise ProfileError(f"[profiles.{name}] takes cpu_cores, gpu and mode; not {sorted(unknown)}")
        cores = p.get("cpu_cores")
        if cores is not None and (not isinstance(cores, int) or isinstance(cores, bool) or cores < 0):
            raise ProfileError(f"[profiles.{name}] cpu_cores is 0 or more (0 = background CPU work paused)")
        gpu = p.get("gpu", "full")
        if gpu not in levels:
            raise ProfileError(f"[profiles.{name}] gpu = {gpu!r} is not a level; the levels are {sorted(levels)}")
        mode = p.get("mode", "running")
        if mode not in modes:
            raise ProfileError(f"[profiles.{name}] mode = {mode!r} is not a node mode; the modes are "
                               f"{', '.join(modes)}")
        profiles[name] = Profile(name, cores, gpu, mode)
    if not profiles:
        raise ProfileError("[schedule] names profiles, and there is no [profiles.<name>] table")
    names = frozenset(profiles)
    days = {}
    for key, text in sched_raw.items():
        if key == "profile":
            continue
        if key not in DAYS:
            raise ProfileError(f"[schedule] takes profile and the weekdays {', '.join(DAYS)}; not {key!r}")
        days[DAYS.index(key)] = _named(text, names, f"[schedule] {key}")
    return Profiles(_named(sched_raw["profile"], names, "[schedule] profile"), days, profiles, levels)


def _named(text: Any, names: frozenset[str], key: str) -> schedule_mod.NamedSchedule:
    if not isinstance(text, str):
        raise ProfileError(f"{key} is a window string: \"06:00-08:00 quiet; else night\"")
    try:
        return schedule_mod.parse_named(text, names)
    except schedule_mod.ScheduleError as e:
        raise ProfileError(f"{key}: {e}") from e


def load() -> Profiles | None:
    """The node's profiles as platform.toml declares them now (re-read on
    every call, as platform_config.load is)."""
    from . import platform_config
    return platform_config.load().profiles


def active(now: datetime | None = None) -> Active | None:
    """The profile in force now, or None when the node declares none."""
    p = load()
    return p.active(now) if p is not None else None


def cpu_cores(now: datetime | None = None) -> int | None:
    """The background CPU work's core limit now: None = no limit, 0 = paused."""
    a = active(now)
    return a.profile.cpu_cores if a is not None else None
