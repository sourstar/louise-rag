"""A card's busy share as its DRIVER counts it, for the whole card at once.

The probe's per-client counters (app/gpu_busy.py, DRM fdinfo) need ptrace
read access to every process holding the card, which a container gets only
with CAP_SYS_PTRACE — with the host's PID namespace, the power to read and
write every process's memory, the worker's node key among them (invariant
8). On the node the probe had CAP_PERFMON alone and read 0 % of a card
gpu_busy.py read at 34–37 % (München, 2026-11-01): every model server's
fdinfo was "Permission denied". So the CARD's figure comes from the driver:

- xe: its perf PMU (Documentation/gpu/xe/xe_pmu.rst, kernel 6.15+), events
  `engine-active-ticks` and `engine-total-ticks` per engine instance —
  what igt's intel_gpu_top reads. A system-wide PMU: perf_event_open with
  pid -1 on a CPU of its `cpumask`, allowed by CAP_PERFMON, which the probe
  already holds. The PMU's own `format/` and `events/` say how an event's
  config is built; nothing here hard-codes a bit position.
- amdgpu: `device/gpu_busy_percent` in sysfs, the firmware's own average,
  readable by anyone.

Per-process attribution stays fdinfo's, where it can be read.
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

SYS = Path("/sys")
#: xe's engine classes (uapi DRM_XE_ENGINE_CLASS_*), named as fdinfo names them
XE_CLASSES = {0: "rcs", 1: "bcs", 2: "vcs", 3: "vecs", 4: "ccs"}
XE_MAX_GT = 4
XE_MAX_INSTANCE = 16
#: rounding the driver's two counters can produce; above it a share is dropped
SLACK = 0.02

_TERM = re.compile(r"^config:(\d+)(?:-(\d+))?$")
PERF_FLAG_FD_CLOEXEC = 8
#: sizeof(struct perf_event_attr), PERF_ATTR_SIZE_VER8
PERF_ATTR_SIZE = 136
_SYSCALL = {"x86_64": 298, "aarch64": 241}


class Source(Protocol):
    kind: str

    def sample(self) -> dict[str, float] | None:
        """Each engine class's busy share since the last call; None when
        there is none yet (a baseline) or the driver could not be read."""


# ---------------------------------------------------------------- perf

def perf_open(pmu_type: int, config: int, cpu: int) -> int:
    """perf_event_open(2) for a system-wide counter: the fd, or OSError."""
    nr = _SYSCALL.get(platform.machine())
    if nr is None:
        raise OSError(f"perf_event_open: no syscall number for {platform.machine()}")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    attr = ctypes.create_string_buffer(
        struct.pack("<IIQ", pmu_type, PERF_ATTR_SIZE, config) + bytes(PERF_ATTR_SIZE - 16),
        PERF_ATTR_SIZE)
    fd = libc.syscall(ctypes.c_long(nr), attr, ctypes.c_int(-1), ctypes.c_int(cpu),
                      ctypes.c_int(-1), ctypes.c_ulong(PERF_FLAG_FD_CLOEXEC))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(fd)


def perf_read(fd: int) -> int:
    return struct.unpack("<Q", os.read(fd, 8))[0]


def _terms(text: str) -> dict[str, int]:
    """`event=0x02,foo=3,bar` → {"event": 2, "foo": 3, "bar": 1}."""
    out: dict[str, int] = {}
    for term in text.strip().split(","):
        if not term:
            continue
        name, sep, value = term.partition("=")
        out[name.strip()] = int(value, 0) if sep else 1
    return out


@dataclass
class XePmu:
    """The xe PMU of one card: busy share per engine class, summed over its
    instances and divided by their count (a class tops at 1.0)."""
    pmu_dir: Path
    open_event: Callable[[int, int, int], int] = perf_open
    read_event: Callable[[int], int] = perf_read
    kind: str = "pmu"
    _pairs: list[tuple[str, int, int]] = field(default_factory=list)   # class, active fd, total fd
    _last: dict[str, tuple[int, int]] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.pmu_type = int((self.pmu_dir / "type").read_text().strip())
        cpus = (self.pmu_dir / "cpumask").read_text().strip() if (self.pmu_dir / "cpumask").exists() else "0"
        self.cpu = int(re.split(r"[,-]", cpus)[0] or 0)
        self.formats: dict[str, tuple[int, int]] = {}
        for f in (self.pmu_dir / "format").iterdir():
            m = _TERM.match(f.read_text().strip())
            if m is None:
                raise ValueError(f"{f.name}: a format this reader does not build ({f.read_text().strip()!r})")
            lo = int(m.group(1))
            self.formats[f.name] = (lo, int(m.group(2) or lo))
        self.events = {name: _terms((self.pmu_dir / "events" / name).read_text())
                       for name in ("engine-active-ticks", "engine-total-ticks")}

    def config(self, terms: dict[str, int]) -> int:
        out = 0
        for name, value in terms.items():
            lo, hi = self.formats[name]
            width = hi - lo + 1
            if value >> width:
                raise ValueError(f"{name}={value} does not fit {width} bits")
            out |= value << lo
        return out

    def open(self) -> int:
        """Open both counters of every engine instance the driver accepts;
        an instance it refuses ends that class on that GT. Returns how many."""
        for gt in range(XE_MAX_GT):
            for cls, name in XE_CLASSES.items():
                for inst in range(XE_MAX_INSTANCE):
                    where = {"gt": gt, "engine_class": cls, "engine_instance": inst}
                    if not set(where) <= set(self.formats):
                        raise ValueError("the PMU's format names no gt/engine_class/engine_instance")
                    try:
                        active = self.open_event(self.pmu_type, self.config(
                            {**self.events["engine-active-ticks"], **where}), self.cpu)
                    except OSError:
                        break
                    try:
                        total = self.open_event(self.pmu_type, self.config(
                            {**self.events["engine-total-ticks"], **where}), self.cpu)
                    except OSError:
                        os.close(active)
                        break
                    self._pairs.append((name, active, total))
        return len(self._pairs)

    def sample(self) -> dict[str, float] | None:
        now: dict[str, tuple[int, int]] = {}
        try:
            for name, active, total in self._pairs:
                a, t = now.get(name, (0, 0))
                now[name] = (a + self.read_event(active), t + self.read_event(total))
        except OSError as e:
            self.error = f"{type(e).__name__}: {e}"
            self._last = None
            return None
        before, self._last = self._last, now
        if before is None:
            return None
        out: dict[str, float] = {}
        for name, (a1, t1) in now.items():
            a0, t0 = before.get(name, (a1, t1))
            if t1 <= t0 or a1 < a0:
                continue                      # no time passed, or a reset: no figure
            share = (a1 - a0) / (t1 - t0)
            if share > 1.0 + SLACK:
                continue                      # impossible: dropped, never averaged in
            out[name] = min(share, 1.0)
        return out

    def close(self) -> None:
        for _, active, total in self._pairs:
            for fd in (active, total):
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._pairs.clear()


# ---------------------------------------------------------------- amdgpu

@dataclass
class AmdgpuBusy:
    """amdgpu's `gpu_busy_percent`: the firmware's own average, now."""
    path: Path
    kind: str = "sysfs"

    def sample(self) -> dict[str, float] | None:
        try:
            pct = int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None
        return {"gfx": max(0, min(pct, 100)) / 100}


# ---------------------------------------------------------------- finding them

def xe_pmu_dir(pdev: str, sys_root: Path = SYS) -> Path:
    """xe names its PMU `xe_<PCI address>` with ':' as '_' (xe_pmu.c)."""
    return sys_root / "bus" / "event_source" / "devices" / ("xe_" + pdev.replace(":", "_"))


def sources(cards: list[tuple[str, str, Path]], sys_root: Path = SYS,
            open_event: Callable[[int, int, int], int] = perf_open,
            read_event: Callable[[int], int] = perf_read) -> tuple[dict[str, Source], dict[str, str]]:
    """For each card (pdev, driver, its sysfs device dir): its driver-level
    source where one can be opened, and for the others why not."""
    found: dict[str, Source] = {}
    why: dict[str, str] = {}
    for pdev, driver, device in cards:
        if driver == "xe":
            d = xe_pmu_dir(pdev, sys_root)
            if not (d / "events" / "engine-active-ticks").exists():
                why[pdev] = f"no xe PMU engine activity at {d} (kernel 6.15+, GuC support)"
                continue
            try:
                pmu = XePmu(d, open_event=open_event, read_event=read_event)
                if pmu.open() == 0:
                    why[pdev] = "the xe PMU opened no engine counter (CAP_PERFMON?)"
                    continue
            except (OSError, ValueError, KeyError) as e:
                why[pdev] = f"the xe PMU: {type(e).__name__}: {e}"
                continue
            found[pdev] = pmu
        elif driver == "amdgpu":
            p = device / "gpu_busy_percent"
            if p.exists():
                found[pdev] = AmdgpuBusy(p)
            else:
                why[pdev] = f"no {p}"
        else:
            why[pdev] = f"no driver-level busy source for {driver!r}"
    return found, why
