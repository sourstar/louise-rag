"""What a child process really is — read from /proc, never from a declaration.

docs/09-formats.md §9.12: the platform states a run only after checking
that the process it launched matches the engine it is about to name. The
children are RAGE's own, in its own PID namespace, so this needs no
privilege: no host PID namespace, no capability.

The root is a parameter so the tests hand it a directory of files.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

_VMRSS = re.compile(r"^VmRSS:\s+(\d+)\s+kB", re.M)
_RSS_PARTS = re.compile(r"^(RssAnon|RssFile|RssShmem):\s+(\d+)\s+kB", re.M)
_RESIDENT_VRAM = re.compile(r"^drm-resident-vram\d*:\s*(\d+)(?:\s*([KMG])iB)?\s*$", re.M)
_RESIDENT = re.compile(r"^drm-resident-([a-z0-9]+):\s*(\d+)(?:\s*([KMG])iB)?\s*$", re.M)
_CLIENT = re.compile(r"^drm-client-id:\s*(\S+)", re.M)
_PDEV = re.compile(r"^drm-pdev:\s*(\S+)", re.M)
_UNIT = {None: 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
MIB = 1 << 20


@dataclass(frozen=True)
class ProcFS:
    root: Path = Path("/proc")

    def pids(self) -> list[int]:
        try:
            return sorted(int(p.name) for p in self.root.iterdir() if p.name.isdigit())
        except OSError:
            return []

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

    def exe_digest(self, pid: int) -> str | None:
        """The digest of the program that is running — evidence for the log
        beside a stated run, not part of the run (09 §9.12)."""
        h = hashlib.sha256()
        try:
            with (self.root / str(pid) / "exe").open("rb") as f:
                for block in iter(lambda: f.read(MIB), b""):
                    h.update(block)
        except OSError:
            return None
        return "sha256:" + h.hexdigest()

    def rss_mib(self, pid: int) -> int | None:
        m = _VMRSS.search((self._bytes(pid, "status") or b"").decode("ascii", "replace"))
        return int(m.group(1)) // 1024 if m else None

    def ram_and_mapped_mib(self, pid: int) -> tuple[int | None, int | None]:
        """(what the process owns, what it has mapped from files) — the kernel's
        own split of the resident set: RssAnon + RssShmem, and RssFile, which is
        page cache the kernel reclaims at will. (None, None) with no split."""
        found = {k: int(v) for k, v in _RSS_PARTS.findall(
            (self._bytes(pid, "status") or b"").decode("ascii", "replace"))}
        if "RssAnon" not in found or "RssFile" not in found:
            return None, None
        return (found["RssAnon"] + found.get("RssShmem", 0)) // 1024, found["RssFile"] // 1024

    def vram_mib(self, pid: int) -> int | None:
        """The card memory the process holds: the kernel's DRM fdinfo, summed
        ONCE per DRM client — a process can hold one client through several
        file descriptors, and counting each would multiply what it holds."""
        seen: dict[str, int] = {}
        try:
            infos = list((self.root / str(pid) / "fdinfo").iterdir())
        except OSError:
            return None
        for info in infos:
            try:
                text = info.read_text(errors="replace")
            except OSError:
                continue
            client = _CLIENT.search(text)
            if client is None:
                continue
            seen[client.group(1)] = sum(int(v) * _UNIT[u or None]
                                        for v, u in _RESIDENT_VRAM.findall(text))
        return sum(seen.values()) // MIB if seen else None

    def gpu_mib(self, pid: int) -> tuple[int | None, int | None]:
        """(on the card, in host RAM) — what the process's GPU buffers occupy, by
        the kernel's DRM fdinfo, once per DRM client. A region named `vram…` is
        the card; every other region (`gtt`, `system`, …) is HOST memory the
        driver holds for the process, and it is REAL. A model whose buffers are
        mostly there has been pushed off the card: `xe` does that silently when
        the card is overcommitted (the node, 2026-09-18 and 2026-09-20)."""
        card: dict[str, int] = {}
        host: dict[str, int] = {}
        try:
            infos = list((self.root / str(pid) / "fdinfo").iterdir())
        except OSError:
            return None, None
        for info in infos:
            try:
                text = info.read_text(errors="replace")
            except OSError:
                continue
            client = _CLIENT.search(text)
            if client is None:
                continue
            regions = [(name, int(v) * _UNIT[u or None]) for name, v, u in _RESIDENT.findall(text)]
            card[client.group(1)] = sum(b for name, b in regions if name.startswith("vram"))
            host[client.group(1)] = sum(b for name, b in regions if not name.startswith("vram"))
        if not card:
            return None, None
        return sum(card.values()) // MIB, sum(host.values()) // MIB

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
        names none; once per DRM client. None when nothing can be read: no
        fdinfo, or not ours. An empty mapping is a process with no client
        on any card — a CPU process."""
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

    def set_oom_score_adj(self, pid: int, value: int) -> bool:
        """Raise a child's score so an out-of-memory kill takes the model that
        grew and not the front (docs/notes/rage.md §9). Raising needs no
        capability; lowering would (CAP_SYS_RESOURCE, not in Docker's
        default set), which is why the front stays at 0 and the children go up."""
        try:
            (self.root / str(pid) / "oom_score_adj").write_text(str(int(value)))
            return True
        except OSError:
            return False
