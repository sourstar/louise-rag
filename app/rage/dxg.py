"""Which card a process is on under WSL2's paravirtual GPU — read from the
driver, never declared.

`/dev/dxg` (Docker on WSL2: the dev PC's RX 9060 XT) has no DRM fdinfo, so
`procfs.card_mib_by_device` cannot say which card a backend holds memory on
and VeGa refused every run there (66a3afa). AMD's dxg build of amd-smi
(`rocdxg-amd-smi-lib`, installed beside ROCm in /opt/rocm-wsl) reads the
Windows driver instead: `amd-smi process` lists, per card, the processes the
driver holds a GPU context for — by THREAD id (the thread that opened the
context, measured 2026-10-18: amd-smi's "pid" 1992 was `Tgid: 1971`, the
backend) — and `amd-smi static --bus --driver` gives each card's PCI address
and the Windows driver's version. Per-process VRAM is "N/A" there, so the
reading is "the driver lists this process as a client of that card", which
is still the driver's statement, not ours.

amd-smi is a parameter (a function from arguments to its standard output) so
the tests hand it recorded answers.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

#: amd-smi's arguments -> its standard output (JSON)
AmdSmi = Callable[[list[str]], str]

TIMEOUT_S = 20.0
_TGID = re.compile(r"^Tgid:\s*(\d+)\s*$", re.M)


class DxgUnreadable(Exception):
    """amd-smi did not answer, or answered something that is not its JSON."""


def runner(binary: Path, timeout: float = TIMEOUT_S) -> AmdSmi:
    """amd-smi at `binary` (the dxg build: <prefix>/bin/amd-smi), run with its
    own prefix's library and module paths and the system's python — never
    the calling backend's venv, which has no amdsmi module."""
    prefix = Path(binary).resolve().parent.parent
    env = {"PATH": f"{prefix / 'bin'}:/usr/local/bin:/usr/bin:/bin",
           "LD_LIBRARY_PATH": str(prefix / "lib"),
           "PYTHONPATH": f"{prefix / 'share' / 'amd_smi'}:{prefix / 'libexec' / 'amdsmi_cli'}",
           "HOME": os.environ.get("HOME", "/tmp")}

    def run(args: list[str]) -> str:
        try:
            out = subprocess.run([str(binary), *args], capture_output=True, text=True,
                                 env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DxgUnreadable(f"amd-smi {' '.join(args)}: {type(e).__name__}: {e}") from None
        if out.returncode != 0:
            raise DxgUnreadable(f"amd-smi {' '.join(args)} exited {out.returncode}: "
                                f"{out.stderr.strip()[-300:]}")
        return out.stdout
    return run


def _json(amd_smi: AmdSmi, args: list[str]) -> Any:
    text = amd_smi(args)
    try:
        return json.loads(text)
    except ValueError:
        raise DxgUnreadable(f"amd-smi {' '.join(args)} did not answer JSON: "
                            f"{text.strip()[:200]!r}") from None


def _gpus(doc: Any) -> list[dict[str, Any]]:
    """amd-smi 1.2's `static` wraps its list in {"gpu_data": [...]};
    `process` answers the bare list."""
    gpus = doc.get("gpu_data", []) if isinstance(doc, dict) else doc
    return [g for g in gpus if isinstance(g, dict)] if isinstance(gpus, list) else []


def addresses(amd_smi: AmdSmi) -> dict[int, dict[str, Any]]:
    """{amd-smi's card index: {"pci": address, "driver": version}}"""
    out: dict[int, dict[str, Any]] = {}
    for g in _gpus(_json(amd_smi, ["static", "--bus", "--driver", "--json"])):
        bdf = (g.get("bus") or {}).get("bdf")
        if isinstance(g.get("gpu"), int) and isinstance(bdf, str):
            out[g["gpu"]] = {"pci": bdf.lower(),
                             "driver": (g.get("driver") or {}).get("version")}
    return out


def tgid(tid: int, proc_root: Path = Path("/proc")) -> int | None:
    """The process a thread belongs to; None when it is gone. /proc/<tid>
    resolves for any thread of this PID namespace, listed or not."""
    try:
        found = _TGID.search((Path(proc_root) / str(tid) / "status").read_text())
    except OSError:
        return None
    return int(found.group(1)) if found else None


def clients(amd_smi: AmdSmi, proc_root: Path = Path("/proc")) -> dict[int, str]:
    """{process id: the PCI address of the card the driver lists it a client
    of}. A process listed on two cards maps to neither address alone: it is
    returned under the sentinel "several" so the caller refuses it."""
    cards = addresses(amd_smi)
    out: dict[int, str] = {}
    for g in _gpus(_json(amd_smi, ["process", "--json"])):
        pci = (cards.get(g.get("gpu")) or {}).get("pci")
        listed = g.get("process_list")
        if not pci or not isinstance(listed, list):
            continue                                   # "No running processes detected"
        for entry in listed:
            info = entry.get("process_info") if isinstance(entry, dict) else None
            tid = info.get("pid") if isinstance(info, dict) else None
            if not isinstance(tid, int):
                continue
            pid = tgid(tid, proc_root)
            if pid is None:
                continue                               # gone since amd-smi listed it
            out[pid] = pci if out.get(pid, pci) == pci else "several"
    return out


def driver_line(amd_smi: AmdSmi, pci: str) -> str:
    """The card's driver as `driver_string` states it: the paravirtual
    device and the Windows driver's version behind it."""
    for card in addresses(amd_smi).values():
        if card["pci"] == pci.lower():
            version = card.get("driver")
            if not isinstance(version, str) or not version:
                raise DxgUnreadable(f"amd-smi names no driver version for the card at {pci}")
            return f"dxg {version}"
    raise DxgUnreadable(f"amd-smi lists no card at {pci}")
