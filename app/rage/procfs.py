"""What a child process really is — read from /proc, never from a declaration.

docs/09-formats.md §9.12: the platform states a run only after checking
that the process it launched matches the engine it is about to name. The
children are RAGE's own, in its own PID namespace, so this needs no
privilege: no host PID namespace, no capability.

The root is a parameter so the tests hand it a directory of files.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

_VMRSS = re.compile(r"^VmRSS:\s+(\d+)\s+kB", re.M)
_RSS_PARTS = re.compile(r"^(RssAnon|RssFile|RssShmem):\s+(\d+)\s+kB", re.M)
_RESIDENT_VRAM = re.compile(r"^drm-resident-vram\d*:\s*(\d+)(?:\s*([KMG])iB)?\s*$", re.M)
_CLIENT = re.compile(r"^drm-client-id:\s*(\S+)", re.M)
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
