"""What a model's process really is — read from /proc, never from a declaration.

docs/09-formats.md §9.12: the platform states a run only from what was
launched. LocalAI's backends are RAGE's children, in its own PID namespace,
so this needs no privilege: no host PID namespace, no capability. What the
observer reads (app/rage/observer.py): the argument vector, the environment,
the working directory, when the process started, and which card it holds
memory on.

The root is a parameter so the tests hand it a directory of files.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_RESIDENT_VRAM = re.compile(r"^drm-resident-vram\d*:\s*(\d+)(?:\s*([KMG])iB)?\s*$", re.M)
_CLIENT = re.compile(r"^drm-client-id:\s*(\S+)", re.M)
_PDEV = re.compile(r"^drm-pdev:\s*(\S+)", re.M)
_UNIT = {None: 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
MIB = 1 << 20


@dataclass(frozen=True)
class ProcFS:
    root: Path = Path("/proc")

    def _bytes(self, pid: int, name: str) -> bytes | None:
        try:
            return (self.root / str(pid) / name).read_bytes()
        except OSError:                   # the process is gone, or is not ours
            return None

    def cmdline(self, pid: int) -> list[str]:
        raw = self._bytes(pid, "cmdline") or b""
        return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]

    def environ(self, pid: int) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in (self._bytes(pid, "environ") or b"").split(b"\0"):
            name, sep, value = item.partition(b"=")
            if sep:
                out[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        return out

    def cwd(self, pid: int) -> str | None:
        """The directory the process runs in, as the kernel links it."""
        try:
            return os.readlink(self.root / str(pid) / "cwd")
        except OSError:
            return None

    def start_ticks(self, pid: int) -> int | None:
        """When the process started, in clock ticks since boot (`stat` field
        22). With the pid it names ONE process for good: a pid reused later
        starts later. None when the process is gone."""
        raw = (self._bytes(pid, "stat") or b"").decode("ascii", "replace")
        fields = raw.rpartition(")")[2].split()     # the name may hold spaces
        try:
            return int(fields[19])                  # field 22, counted from field 3
        except (IndexError, ValueError):
            return None

    def start_time(self, pid: int) -> float | None:
        """The same instant as Unix seconds: the boot time `stat` gives
        (`btime`) plus the ticks, at the kernel's tick rate."""
        ticks = self.start_ticks(pid)
        if ticks is None:
            return None
        try:
            stat = (self.root / "stat").read_text(errors="replace")
        except OSError:
            return None
        btime = re.search(r"^btime\s+(\d+)", stat, re.M)
        if btime is None:
            return None
        return int(btime.group(1)) + ticks / os.sysconf("SC_CLK_TCK")

    def card_mib_by_device(self, pid: int) -> dict[str | None, int] | None:
        """What the process holds on each card, keyed by the card's PCI
        address as its driver reports it (`drm-pdev`), None where the driver
        names none; once per DRM client — a process can hold one client
        through several file descriptors, and counting each would multiply
        what it holds. None when nothing can be read: no fdinfo, or not ours.
        An empty mapping is a process with no client on any card — a CPU
        process."""
        try:
            infos = list((self.root / str(pid) / "fdinfo").iterdir())
        except OSError:
            return None
        clients: dict[str, tuple[str | None, int]] = {}
        for info in infos:
            try:
                text = info.read_text(errors="replace")
            except OSError:
                continue
            client = _CLIENT.search(text)
            if client is None:
                continue
            pdev = _PDEV.search(text)
            clients[client.group(1)] = (
                pdev.group(1) if pdev else None,
                sum(int(v) * _UNIT[u or None] for v, u in _RESIDENT_VRAM.findall(text)))
        out: dict[str | None, int] = {}
        for pdev, held in clients.values():
            out[pdev] = out.get(pdev, 0) + held // MIB
        return out
