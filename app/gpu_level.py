"""The profile's GPU level, applied to the card (docs/notes/node-profiles-design.md,
"Who applies what"; operator, 2026-10-27).

WHAT. A level (`[gpu.levels.<name>]` in platform.toml, app/node_profile.py)
says three things; this module makes the card say them:
- `power_limit` -> the xe hwmon `power1_max` (microwatts), the standard Linux
  power limit — or `power1_cap`, the hwmon ABI's power cap, where the driver
  exposes that instead (the node's B70 on kernel 6.18: `power1_cap` 230 W
  rw, no `power1_max`, no `power1_rated_max`; found 2026-10-27): "max" is
  the card's own (as read at start), "N%" is N % of `power1_rated_max` (or
  of the card's own where there is none), "NW" watts;
- `max_clock` -> `max_freq`, ONLY where no writable power limit exists:
  "max" the card's own, "N%" of `rp0_freq`, "NMHz";
- `fan_max_rpm` -> a writable `fanN_max` (hwmon ABI, RPM) only. Where the
  driver exposes none, the status says "firmware-controlled" — xe exposes
  `fan1_input` read-only. A writable `pwmN` alone is a duty cycle, not an
  RPM: capping RPM with it needs a closed loop, which is not guessed here.

A LIMIT, NOT A KERNEL. A power limit or a clock ceiling changes how fast a
vector comes, never its bytes: the same build, flags and env run the same
kernels in the same order (invariant 12, docs/notes/approved-encoders.md
"Bitwise reproducibility, by vendor"); only the time per kernel moves. The
log line says so each time a level is applied.

RECONCILED. The governor's loop (app/gpu_governor.py) gives the level for
now every tick; `reconcile` compares it with what this module last wrote
and writes only a difference — Kubernetes' reconciliation loop. Nothing is
read from the card per tick: its files are read once at start, then only
written (the October hangs, docs/notes/STATUS.md: no new wakes, never a
forcewake).

UNDER THE HOLD. The driver refuses `min_freq > max_freq`. A `max_freq`
below the query hold's `min_freq` is not written while the hold is on: it
waits for the hold to end (`pending` in the status), and app/gpu_clock.py
clamps every later hold to the cap. A cap below the rest value is raised to
it (gpu-cap.sh did the same).

LEFT BEHIND. A governor killed while a reduced level was applied leaves it
on the card, and the next one would read it as the card's own. As the
min_freq hold does (app/gpu_clock.py), a start value equal to what a
declared level writes is taken for ours: the card's own is then
`power1_rated_max` (power) or `rp0_freq` (clock).

Imports nothing of the platform, as app/gpu_clock.py: the two are the whole
of what writes the host's sysfs.
"""
from __future__ import annotations

import errno
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

BYTES_NOTE = "a limit changes how fast a vector comes, never its bytes"
FIRMWARE = "firmware-controlled"
# a write refused for these means the knob is not writable here (a read-only
# attribute, a read-only mount, a locked limit), not that the value was bad
_NOT_WRITABLE = {errno.EACCES, errno.EPERM, errno.EROFS, errno.EOPNOTSUPP,
                 errno.ENOTSUP, errno.ENOENT}
_WATTS = re.compile(r"^(\d+(?:\.\d+)?)\s*w$", re.IGNORECASE)
_MHZ = re.compile(r"^(\d+(?:\.\d+)?)\s*mhz$", re.IGNORECASE)


class Level(Protocol):
    """app/node_profile.GpuLevel, by shape (this module imports no platform)."""
    name: str
    power_limit: str
    max_clock: str
    fan_max_rpm: int | None


class Holder(Protocol):
    """app/gpu_clock.Governor, by shape."""
    pinned: bool
    rest: int

    def current_min(self) -> int: ...
    def set_cap(self, mhz: int) -> None: ...


def _num(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _writable(path: Path | None) -> bool:
    """What the driver declares: sysfs gives a read-only attribute mode 0444.
    A stat, never a probe write."""
    try:
        return path is not None and bool(path.stat().st_mode & 0o222)
    except OSError:
        return False


def find_hwmon(root: Path | None) -> Path | None:
    """The card's hwmon: `root` itself, or the `hwmonN` under it (the card's
    `device/hwmon/`; N changes across boots) — the xe one first."""
    if root is None:
        return None
    if (root / "name").is_file():
        return root
    try:
        kids = sorted(p for p in root.iterdir() if p.name.startswith("hwmon"))
    except OSError:
        return None
    for k in kids:
        try:
            if (k / "name").read_text().strip() == "xe":
                return k
        except OSError:
            continue
    return kids[0] if kids else None


def power_uw(spec: str, own: int, base: int) -> int:
    """`power_limit` in microwatts: "max" the card's own, "N%" of `base`,
    "NW" watts (hwmon's unit is the microwatt)."""
    t = spec.strip()
    if t.lower() == "max":
        return own
    if t.endswith("%"):
        return int(base * int(t[:-1]) // 100)
    m = _WATTS.match(t)
    if m:
        return int(round(float(m.group(1)) * 1_000_000))
    raise ValueError(f"power_limit {spec!r} is not \"max\", \"N%\" or watts")


def clock_mhz(spec: str, own: int, top: int, floor: int) -> int:
    """`max_clock` in MHz: "max" the card's own, "N%" of `rp0_freq`, "NMHz";
    never above the card's top nor below its floor."""
    t = spec.strip()
    if t.lower() == "max":
        v = own
    elif t.endswith("%"):
        v = top * int(t[:-1]) // 100
    elif (m := _MHZ.match(t)):
        v = int(float(m.group(1)))
    else:
        raise ValueError(f"max_clock {spec!r} is not \"max\", \"N%\" or MHz")
    return max(floor, min(top, v))


def _w(uw: int | None) -> str:
    return "?" if uw is None else f"{uw / 1e6:g} W"


class LevelApplier:
    def __init__(self, freq_dir: Path, hwmon: Path | None, *,
                 levels: Iterable[Level] = (), log: Callable[[str], None] = print) -> None:
        self.log = log
        self.freq = freq_dir
        self.hwmon = hwmon
        # -- the power limit
        self.pmax = None
        if hwmon is not None:
            self.pmax = next((hwmon / n for n in ("power1_max", "power1_cap")
                              if (hwmon / n).is_file()), hwmon / "power1_max")
        self.plimit = self.pmax.name if self.pmax is not None else "power1_max"
        self.power_start = _num(self.pmax)
        rated = _num(hwmon / "power1_rated_max") if hwmon is not None else None
        self.power_rated = rated if rated and rated > 0 else None
        if self.power_start is None:
            self.power_ok, self.power_why = False, "no power1_max nor power1_cap"
        elif not _writable(self.pmax):
            self.power_ok, self.power_why = False, f"{self.plimit} is read-only"
        else:
            self.power_ok, self.power_why = True, None
        # -- the clock ceiling
        self.top = _num(freq_dir / "rp0_freq")
        self.floor = _num(freq_dir / "rpn_freq") or 0
        self.clock_start = _num(freq_dir / "max_freq")
        self.clock_ok = (self.clock_start is not None and self.top is not None
                         and _writable(freq_dir / "max_freq"))
        # -- the fan
        self.fan_file: Path | None = None
        self.fan_mode = FIRMWARE
        if hwmon is not None:
            for f in sorted(hwmon.glob("fan*_max")):
                if _writable(f) and _num(f) is not None:
                    self.fan_file = f
                    break
            if self.fan_file is not None:
                self.fan_mode = self.fan_file.name
            elif any(_writable(p) for p in hwmon.glob("pwm[0-9]")):
                self.fan_mode = "pwm only: an RPM cap needs a closed loop, not applied"
        self.fan_start = _num(self.fan_file)
        # -- the card's own, unless a governor stopped without restoring left ours
        self.power_own = self.power_start
        self.clock_own = self.clock_start
        self._adopt(list(levels))
        self.power_now, self.clock_now, self.fan_now = (
            self.power_start, self.clock_start, self.fan_start)
        self.applied: tuple | None = None       # the level last applied whole
        self.name: str | None = None
        self.profile: str | None = None
        self.spec: dict[str, Any] = {}
        self.pending: str | None = None
        self.error: str | None = None

    # ---------------------------------------------------------------- start
    def _adopt(self, levels: list[Level]) -> None:
        if self.power_start is not None and self.power_rated is not None \
                and self.power_start < self.power_rated:
            for lv in levels:
                try:
                    ours = power_uw(lv.power_limit, self.power_rated, self.power_rated)
                except ValueError:
                    continue
                if lv.power_limit.lower() != "max" and ours == self.power_start:
                    self.power_own = self.power_rated
                    self.log(f"{self.plimit} {_w(self.power_start)} at start is what level "
                             f"{lv.name!r} writes, left by a governor that did not restore it: "
                             f"the card's own is power1_rated_max {_w(self.power_rated)}")
                    break
        if self.clock_start is not None and self.top is not None and self.clock_start < self.top:
            for lv in levels:
                try:
                    ours = clock_mhz(lv.max_clock, self.top, self.top, self.floor)
                except ValueError:
                    continue
                if lv.max_clock.lower() != "max" and ours == self.clock_start:
                    self.clock_own = self.top
                    self.log(f"max_freq {self.clock_start} MHz at start is what level {lv.name!r} "
                             f"writes, left behind: the card's own is rp0_freq {self.top} MHz")
                    break

    # ------------------------------------------------------------ reconcile
    def reconcile(self, level: Level | None, holder: Holder,
                  profile: str | None = None) -> bool:
        """Make the card say `level` (None: no profile declared — the card's
        own). True when the status changed."""
        key = None if level is None else (level.name, level.power_limit,
                                          level.max_clock, level.fan_max_rpm)
        if key == self.applied and self.pending is None and self.error is None:
            return False
        try:
            want_power = (self.power_own if level is None or self.power_own is None else
                          power_uw(level.power_limit, self.power_own,
                                   self.power_rated or self.power_own))
            want_clock = (self.clock_own if level is None or not self.clock_ok else
                          clock_mhz(level.max_clock, self.clock_own, self.top, self.floor))
        except ValueError as e:
            return self._refuse(f"level {level.name!r}: {e}")
        wrote: list[str] = []
        pending = None
        # 1. the power limit, where writable
        if self.power_ok and want_power is not None and want_power != self.power_now:
            try:
                self.pmax.write_text(str(want_power))
                self.power_now = want_power
                wrote.append(f"{self.plimit} {_w(want_power)}")
            except OSError as e:
                if e.errno not in _NOT_WRITABLE:
                    return self._refuse(f"cannot write {self.plimit} {want_power}: {type(e).__name__}: {e}")
                self.power_ok = False
                self.power_why = f"{self.plimit} refused the write: {type(e).__name__}: {e}"
                self.log(f"{self.power_why}; the level goes to max_freq instead")
        # 2. else the clock ceiling, never under the query hold
        if not self.power_ok:
            if not self.clock_ok:
                if level is not None and (level.power_limit.lower() != "max"
                                          or level.max_clock.lower() != "max"):
                    return self._refuse(f"level {level.name!r}: {self.power_why}, and no writable "
                                        f"max_freq: not applied")
            elif want_clock != self.clock_now:
                floor_now = holder.current_min()
                if want_clock < floor_now and holder.pinned:
                    pending = (f"max_freq {want_clock} MHz waits for the query hold "
                               f"({floor_now} MHz) to end")
                else:
                    want_clock = max(want_clock, floor_now)
                    try:
                        (self.freq / "max_freq").write_text(str(want_clock))
                    except OSError as e:
                        return self._refuse(f"cannot write max_freq {want_clock}: {type(e).__name__}: {e}")
                    self.clock_now = want_clock
                    holder.set_cap(want_clock)
                    wrote.append(f"max_freq {want_clock} MHz")
        # 3. the fan, only where the driver lets software cap it
        if self.fan_file is not None:
            want_fan = (level.fan_max_rpm if level is not None and level.fan_max_rpm
                        else self.fan_start)
            if want_fan is not None and want_fan != self.fan_now:
                try:
                    self.fan_file.write_text(str(want_fan))
                    self.fan_now = want_fan
                    wrote.append(f"{self.fan_file.name} {want_fan} rpm")
                except OSError as e:
                    return self._refuse(f"cannot write {self.fan_file.name}: {type(e).__name__}: {e}")
        name = level.name if level is not None else None
        moved = (name, profile) != (self.name, self.profile) or bool(wrote) \
            or pending != self.pending or self.error is not None
        self.name, self.profile, self.error, self.pending = name, profile, None, pending
        self.spec = ({} if level is None else
                     {"power_limit": level.power_limit, "max_clock": level.max_clock,
                      "fan_max_rpm": level.fan_max_rpm})
        if pending is None:
            self.applied = key
        if wrote or pending:
            what = name if name is not None else "none (no profile declared: the card's own limits)"
            if profile:
                what += f" (profile {profile})"
            parts = ", ".join(wrote) or "nothing written yet"
            if pending:
                parts += f"; {pending}"
            if self.fan_file is None:
                parts += f"; fan {self.fan_mode}"
            self.log(f"GPU level {what}: {parts} — {BYTES_NOTE}")
        return moved

    def _refuse(self, why: str) -> bool:
        """A level that cannot be applied: logged once, the last one kept."""
        if why == self.error:
            return False
        self.error = why
        kept = self.name or "the card's own limits"
        self.log(f"GPU level not applied, the last one kept ({kept}): {why}")
        return True

    def fail(self, why: str) -> bool:
        """The profile itself did not load (platform.toml refused): the last
        level is kept, the reason shown."""
        return self._refuse(why)

    # -------------------------------------------------------------- restore
    def restore(self, holder: Holder) -> None:
        """On a clean stop: the card's own limits back. Call after the hold
        is released, so `max_freq` never goes under a held `min_freq`."""
        done = []
        if self.power_ok and self.power_own is not None and self.power_now != self.power_own:
            try:
                self.pmax.write_text(str(self.power_own))
                self.power_now = self.power_own
                done.append(f"{self.plimit} {_w(self.power_own)}")
            except OSError as e:
                self.log(f"cannot restore {self.plimit}: {type(e).__name__}: {e}")
        if self.clock_ok and self.clock_own is not None and self.clock_now != self.clock_own:
            target = max(self.clock_own, holder.current_min())
            try:
                (self.freq / "max_freq").write_text(str(target))
                self.clock_now = target
                holder.set_cap(target)
                done.append(f"max_freq {target} MHz")
            except OSError as e:
                self.log(f"cannot restore max_freq: {type(e).__name__}: {e}")
        if self.fan_file is not None and self.fan_start is not None and self.fan_now != self.fan_start:
            try:
                self.fan_file.write_text(str(self.fan_start))
                self.fan_now = self.fan_start
                done.append(f"{self.fan_file.name} {self.fan_start} rpm")
            except OSError as e:
                self.log(f"cannot restore {self.fan_file.name}: {type(e).__name__}: {e}")
        if done:
            self.log(f"GPU level: the card's own limits restored: {', '.join(done)}")

    # --------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        via = self.plimit if self.power_ok else ("max_freq" if self.clock_ok else None)
        return {"name": self.name, "profile": self.profile, **self.spec,
                "via": via,
                "power_w": None if self.power_now is None else self.power_now / 1e6,
                "power_own_w": None if self.power_own is None else self.power_own / 1e6,
                "power_note": self.power_why,
                "max_freq_mhz": self.clock_now,
                "fan": f"{self.fan_mode} {self.fan_now} rpm" if self.fan_file is not None else self.fan_mode,
                "pending": self.pending, "error": self.error}
