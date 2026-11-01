"""The card's busy time, second by second, from the kernel's DRM fdinfo.

Instrument I1 of docs/notes/gpu-saturation-plan.md: how much of each second
the card's engines worked, and for which process. It runs inside the
`gpu-probe` service (app/gpu_probe.py), the one Louise process that sees the
host's processes; nothing here touches a model server, so measuring changes
no run (the VeGa wrapper's code is part of every run's engine, 09 §9.3).

THE STANDARD, not ours: the kernel's DRM client usage stats
(Documentation/gpu/drm-usage-stats.rst), which every driver Louise meets
prints in `/proc/<pid>/fdinfo/<fd>`:
- `drm-cycles-<engine>` over `drm-total-cycles-<engine>` (xe): the engine's
  busy ticks and the GPU's own timestamp, both per client. Busy is the
  ratio of their changes — each client against ITS OWN time base (a server
  can hold two clients; borrowing an idle one's time base once printed
  2225 %, docs/notes/STATUS.md);
- `drm-engine-<engine>: <n> ns` (amdgpu, i915, msm, panfrost …): busy
  nanoseconds, against the wall clock between two reads;
- `drm-engine-capacity-<engine>`: how many identical engines the name
  covers; busy is divided by it, so one engine class tops at 1.0.
The same counters are what igt's gputop, nvtop and qmassa read; none of
them publishes a time series a program can poll, so the probe keeps one.

WHAT A SAMPLE SAYS, per card (its PCI address, `drm-pdev`):
- `engines`: each engine class's busy share over the interval, summed over
  the card's clients and capped at 1.0;
- `busy`: the busiest COMPUTE engine's share (COMPUTE_ENGINES: xe's `ccs`
  and `rcs`, amdgpu's `gfx` and `compute`). Not the busiest of all: on the
  B70 the copy engine reads as busy as compute, a resident context counted
  active (perf, 2026-11-01);
- `processes`: who worked, with the label the System page's panel uses.
An impossible reading — a counter that went backwards (a reset read
−821,239 % once), a share above 1 beyond rounding — is DROPPED and counted
(`dropped`), never averaged in. A client seen for the first time gives no
share until its second reading (`new`). `dt` is the interval actually
covered, so a probe that stalled shows as a long one.

A MISSED READ LOSES NOTHING BUT RESOLUTION: the kernel's counters are
cumulative, so the next interval covers it.

THE CARD'S FIGURE COMES FROM ITS DRIVER WHERE IT CAN (app/gpu_pmu.py: xe's
perf PMU, amdgpu's `gpu_busy_percent`; `source` says which). Reading another
process's fdinfo is a ptrace-read check, and on the node every model
server's was "Permission denied" to the probe (CAP_PERFMON only): it printed
0 % of a card that was 34–37 % busy (München, 2026-11-01). fdinfo is then
for WHO, as far as it can be read; it gives the card's figure only when no
process was unreadable. A card with neither has `busy: null` and says why
(`blind`) — never 0, which is a measurement.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

#: one sample a second: the resolution the scheduling measurements used
PERIOD_S = 1.0
#: an hour of samples per card, in memory (a sample is a few hundred bytes)
KEEP_S = 3600.0
#: how often the probe looks for new DRM clients across /proc. Between two
#: looks it reads only the fdinfo files it knows; a model server loaded
#: meanwhile is picked up at the next look, its first interval lost
RESCAN_S = 10.0
#: rounding the kernel's two counters can produce; above it a reading is
#: impossible and dropped
SLACK = 0.02
#: THE ENGINES THAT COMPUTE: a card is as busy as the busiest of these.
#: On the B70 the copy engine (bcs) reads as busy as ccs — 78.4 against 78.3,
#: 92.7 against 91.9, 0 at idle (perf, München, 2026-11-01): the Level Zero
#: runtime keeps a copy context resident while compute runs, and the driver
#: counts it active. xe: ccs, and rcs (it can run compute); amdgpu: gfx,
#: compute; a driver naming none of these is read by its busiest engine.
COMPUTE_ENGINES = frozenset({"ccs", "rcs", "compute", "gfx", "render"})


def card_busy(engines: Mapping[str, float]) -> float:
    """The card's share: its busiest COMPUTE engine; the busiest engine of
    any kind only when it names none of them."""
    compute = [v for e, v in engines.items() if e in COMPUTE_ENGINES]
    return max(compute if compute else engines.values(), default=0.0)

_CYCLES = re.compile(r"^drm-cycles-([A-Za-z0-9_-]+):\s*(\d+)\s*$")
_TOTAL = re.compile(r"^drm-total-cycles-([A-Za-z0-9_-]+):\s*(\d+)\s*$")
_CAPACITY = re.compile(r"^drm-engine-capacity-([A-Za-z0-9_-]+):\s*(\d+)\s*$")
_ENGINE_NS = re.compile(r"^drm-engine-([A-Za-z0-9_-]+):\s*(\d+)\s*ns\s*$")


@dataclass(frozen=True)
class Counters:
    """One DRM client's engine counters at one read."""
    pdev: str
    client: str
    #: engine -> (busy ticks, the client's own GPU timestamp) — xe
    cycles: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    #: engine -> busy nanoseconds — amdgpu, i915 and the others
    engine_ns: Mapping[str, int] = field(default_factory=dict)
    capacity: Mapping[str, int] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.cycles and not self.engine_ns


def parse_counters(text: str) -> Counters | None:
    """A DRM file's engine counters; None for a file that is no DRM client
    (no `drm-pdev` or no `drm-client-id`)."""
    pdev = client = None
    busy: dict[str, int] = {}
    total: dict[str, int] = {}
    ns: dict[str, int] = {}
    capacity: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("drm-pdev:"):
            pdev = line.split(":", 1)[1].strip()
        elif line.startswith("drm-client-id:"):
            client = line.split(":", 1)[1].strip()
        elif line.startswith("drm-total-cycles-"):
            m = _TOTAL.match(line)
            if m:
                total[m.group(1)] = int(m.group(2))
        elif line.startswith("drm-cycles-"):
            m = _CYCLES.match(line)
            if m:
                busy[m.group(1)] = int(m.group(2))
        elif line.startswith("drm-engine-capacity-"):     # before drm-engine-<name>
            m = _CAPACITY.match(line)
            if m and int(m.group(2)) > 0:
                capacity[m.group(1)] = int(m.group(2))
        elif line.startswith("drm-engine-"):
            m = _ENGINE_NS.match(line)
            if m:
                ns[m.group(1)] = int(m.group(2))
    if pdev is None or client is None:
        return None
    cycles = {e: (busy[e], total[e]) for e in busy if e in total}
    return Counters(pdev=pdev, client=client, cycles=cycles, engine_ns=ns, capacity=capacity)


def shares(before: Counters, after: Counters, wall_ns: int) -> dict[str, float] | None:
    """Each engine's busy share between two reads of ONE client, divided by
    the engine's capacity; None when a reading is impossible (a counter went
    backwards, a share above 1 + SLACK). An engine whose time base did not
    move (no GPU timestamp change) gives nothing."""
    out: dict[str, float] = {}
    for engine, (b1, t1) in after.cycles.items():
        if engine not in before.cycles:
            continue
        b0, t0 = before.cycles[engine]
        if b1 < b0 or t1 < t0:
            return None
        if t1 == t0:
            continue
        out[engine] = (b1 - b0) / (t1 - t0) / after.capacity.get(engine, 1)
    for engine, n1 in after.engine_ns.items():
        if engine not in before.engine_ns or engine in out:
            continue
        n0 = before.engine_ns[engine]
        if n1 < n0:
            return None
        if wall_ns <= 0:
            continue
        out[engine] = (n1 - n0) / wall_ns / after.capacity.get(engine, 1)
    if any(v > 1.0 + SLACK for v in out.values()):
        return None
    return {e: min(v, 1.0) for e, v in out.items()}


@dataclass(frozen=True)
class Holder:
    """Who holds a client: what the panel calls the process."""
    pid: int
    label: str
    container: str


@dataclass
class _Known:
    path: Path          # /proc/<pid>/fdinfo/<fd>
    holder: Holder
    last: Counters | None = None
    last_ns: int = 0


#: (pdev, client id): a client's identity on its card — the kernel numbers
#: clients once per device, so two descriptors (a dup, a fork) of one client
#: are one key and counted once
ClientKey = tuple[str, str]


class BusySampler:
    """Reads every DRM client's counters once a PERIOD_S and keeps KEEP_S of
    samples per card. `discover` finds the clients (the probe's /proc scan),
    `read` reads one fdinfo file; both are parameters so the tests drive a
    fake /proc and a fake clock."""

    def __init__(self, discover: Callable[[], Iterable[tuple[Path, Holder]]],
                 cards: Callable[[], Iterable[str]] = lambda: (), *,
                 devices: Callable[[], tuple[Mapping[str, Any], Mapping[str, str]]] | None = None,
                 read: Callable[[Path], str | None] | None = None,
                 period_s: float = PERIOD_S, keep_s: float = KEEP_S,
                 rescan_s: float = RESCAN_S,
                 wall: Callable[[], float] = time.time,
                 mono_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._discover = discover
        self._cards = cards
        self._open_devices = devices
        self._devices: dict[str, Any] | None = None     # pdev -> a gpu_pmu.Source
        self._blind: dict[str, str] = {}                 # pdev -> why it has none
        self._unreadable = 0                             # processes whose fds were refused
        self._read = read or _read_text
        self.period_s = period_s
        self._keep = max(1, int(keep_s / period_s))
        self._rescan_ns = int(rescan_s * 1e9)
        self._wall = wall
        self._mono_ns = mono_ns
        self._known: dict[ClientKey, _Known] = {}
        self._paths: dict[Path, ClientKey] = {}
        self._pdevs: set[str] = set()
        self._last_scan_ns: int | None = None
        self._last_sample_ns: int | None = None
        self._samples: dict[str, deque[dict]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ reading

    def _rescan(self, now_ns: int) -> None:
        self._last_scan_ns = now_ns
        self._pdevs.update(p for p in self._cards() if p)
        if self._devices is None:
            self._devices, self._blind = {}, {}
            if self._open_devices is not None:
                try:
                    found, why = self._open_devices()
                    self._devices, self._blind = dict(found), dict(why)
                except Exception as e:   # noqa: BLE001 — said per card, never raised
                    self._blind = {"*": f"{type(e).__name__}: {e}"}
            self._pdevs.update(self._devices)
        seen: set[ClientKey] = set()
        found_files = self._discover()
        self._unreadable = int(getattr(found_files, "unreadable", 0))
        for path, holder in found_files:
            text = self._read(path)
            counters = parse_counters(text) if text else None
            if counters is None or counters.empty:
                continue                  # an enumeration, a probe: no engine work
            key = (counters.pdev, counters.client)
            self._pdevs.add(counters.pdev)
            if key in seen:
                continue                  # a second descriptor of one client
            seen.add(key)
            known = self._known.get(key)
            if known is None:
                self._known[key] = _Known(path=path, holder=holder)
            elif known.path != path and not self._readable(known.path, key):
                known.path, known.holder = path, holder
        for key in [k for k in self._known if k not in seen]:
            del self._known[key]          # gone: the process ended or closed it
        self._paths = {k.path: key for key, k in self._known.items()}

    def _readable(self, path: Path, key: ClientKey) -> bool:
        text = self._read(path)
        counters = parse_counters(text) if text else None
        return counters is not None and (counters.pdev, counters.client) == key

    def sample(self) -> None:
        """One interval: read every known client, turn the changes into
        shares, append one sample per card."""
        now_ns = self._mono_ns()
        if self._last_scan_ns is None or now_ns - self._last_scan_ns >= self._rescan_ns:
            self._rescan(now_ns)
        t = self._wall()
        dt = None if self._last_sample_ns is None else (now_ns - self._last_sample_ns) / 1e9
        self._last_sample_ns = now_ns
        engines: dict[str, dict[str, float]] = {p: {} for p in self._pdevs}
        procs: dict[str, dict[Holder, dict[str, float]]] = {p: {} for p in self._pdevs}
        new = {p: 0 for p in self._pdevs}
        dropped = {p: 0 for p in self._pdevs}
        for key, known in list(self._known.items()):
            pdev = key[0]
            text = self._read(known.path)
            counters = parse_counters(text) if text else None
            if counters is None or (counters.pdev, counters.client) != key:
                del self._known[key]      # the descriptor closed or now names another client
                continue
            read_ns = self._mono_ns()
            before, before_ns = known.last, known.last_ns
            known.last, known.last_ns = counters, read_ns
            if before is None:
                new[pdev] += 1
                continue
            got = shares(before, counters, read_ns - before_ns)
            if got is None:
                dropped[pdev] += 1
                continue
            mine = procs[pdev].setdefault(known.holder, {})
            for engine, share in got.items():
                engines[pdev][engine] = engines[pdev].get(engine, 0.0) + share
                mine[engine] = mine.get(engine, 0.0) + share
        device: dict[str, dict[str, float] | None] = {}
        for pdev, src in (self._devices or {}).items():
            try:
                device[pdev] = src.sample()
            except Exception:   # noqa: BLE001 — this second has no figure
                device[pdev] = None
        with self._lock:
            for pdev in self._pdevs:
                source, blind = "fdinfo", None
                if pdev in device:
                    source = getattr(self._devices[pdev], "kind", "device")
                    got = device[pdev]
                    per_engine = ({e: round(min(v, 1.0), 4) for e, v in sorted(got.items())}
                                  if got is not None else None)
                elif self._unreadable:
                    per_engine = None
                    blind = (self._blind.get(pdev) or self._blind.get("*") or
                             "no driver-level source") + (
                        f"; fdinfo of {self._unreadable} process(es) unreadable "
                        f"(ptrace read access)")
                else:
                    per_engine = {e: round(min(v, 1.0), 4)
                                  for e, v in sorted(engines[pdev].items())}
                sample: dict = {"t": round(t, 3),
                                "dt": None if dt is None else round(dt, 3),
                                "busy": (None if per_engine is None
                                         else card_busy(per_engine)),
                                "source": source,
                                "engines": per_engine or {},
                                "processes": sorted(
                                    ({"pid": h.pid, "label": h.label, "container": h.container,
                                      "busy": round(min(max(e.values(), default=0.0), 1.0), 4)}
                                     for h, e in procs[pdev].items()),
                                    key=lambda p: (-p["busy"], p["pid"]))}
                if new[pdev]:
                    sample["new"] = new[pdev]
                if dropped[pdev]:
                    sample["dropped"] = dropped[pdev]
                if blind:
                    sample["blind"] = blind
                self._samples.setdefault(pdev, deque(maxlen=self._keep)).append(sample)

    # ------------------------------------------------------------ serving

    def since(self, t: float | None = None) -> dict:
        """Every kept sample after `t` (Unix seconds), per card — what
        GET /busy answers. A first sample has `dt` None: it only set the
        baseline."""
        with self._lock:
            cards = {pdev: [s for s in samples if t is None or s["t"] > t]
                     for pdev, samples in self._samples.items()}
        return {"period_s": self.period_s, "now": round(self._wall(), 3), "cards": cards}

    def run(self, stop: threading.Event) -> None:
        """Sample every PERIOD_S until `stop`, on a fixed beat: a slow read
        delays the next sample, never shifts every later one."""
        next_ns = self._mono_ns()
        period_ns = int(self.period_s * 1e9)
        while not stop.is_set():
            try:
                self.sample()
            except Exception:   # noqa: BLE001 — a bad read never ends the series
                pass
            next_ns += period_ns
            now_ns = self._mono_ns()
            if next_ns <= now_ns:                  # fell behind: restart the beat
                next_ns = now_ns + period_ns
            stop.wait((next_ns - now_ns) / 1e9)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


class DrmFiles(list):
    """`drm_files`' answer: the descriptors, and how many processes would not
    show theirs (`unreadable`) — a figure summed over what could be read is
    a card's busy only when that is 0."""
    unreadable: int = 0


def drm_files(proc: Path, label_of: Callable[[Path], Holder | None]) -> DrmFiles:
    """Every descriptor under /proc that points into /dev/dri, with who holds
    it — what `BusySampler` reads. `label_of` names a process from its
    /proc directory (None: leave it out)."""
    out = DrmFiles()
    try:
        pids = [e.name for e in os.scandir(proc) if e.name.isdigit()]
    except OSError:
        return out
    for name in pids:
        pid_dir = proc / name
        try:
            fds = os.listdir(pid_dir / "fd")
        except PermissionError:
            out.unreadable += 1           # not ours to read: its share is unknown
            continue
        except OSError:
            continue
        holder: Holder | None = None
        for fd in fds:
            try:
                if not os.readlink(pid_dir / "fd" / fd).startswith("/dev/dri/"):
                    continue
            except OSError:
                continue
            if holder is None:
                holder = label_of(pid_dir)
                if holder is None:
                    break
            out.append((pid_dir / "fdinfo" / fd, holder))
    return out
