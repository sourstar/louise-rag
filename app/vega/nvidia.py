"""Which NVIDIA card a backend is on, and its driver — read from the driver
(NVML, through `nvidia-smi`), never declared.

A CUDA process holds `/dev/nvidia<minor>` (with `/dev/nvidiactl` and
`/dev/nvidia-uvm`), not a DRM render node, so it has NO DRM fdinfo:
`procfs.card_mib_by_device` reads nothing there, and before this module a
backend computing on an NVIDIA card would have been stated as a CPU run —
the wrong run, every vector of it under the wrong identity (2026-10-21, the
side task "support for NVIDIA" before any NVIDIA peer existed).

Per-process VRAM is NVML's (`--query-compute-apps`), but it lists HOST pids,
and a backend in LocalAI's container is in its own PID namespace. So the
reading is the one `dxg.py` makes under WSL: which cards the backend's
processes hold (the device minors of the `/dev/nvidia<N>` they opened, mapped
to PCI addresses through `/proc/driver/nvidia/gpus/*/information`), and what
the load did to each card's used memory, read just before and just after it.
Under WSL2 an NVIDIA card is `/dev/dxg` like an AMD one, NVML answers through
`/usr/lib/wsl/lib/nvidia-smi`, and no process can be matched to a card: there
the card is the one `cards` names, and must be exactly one.

nvidia-smi is a parameter (a function from arguments to its standard output)
so the tests hand it recorded answers.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Callable

#: nvidia-smi's arguments -> its standard output (CSV, no header, no units)
NvidiaSmi = Callable[[list[str]], str]

TIMEOUT_S = 20.0
#: the device files a CUDA context holds; the per-card one carries its minor
DEVICE = re.compile(r"^/dev/nvidia(\d+)$")
_MINOR = re.compile(r"^Device Minor:\s*(\d+)\s*$", re.M)
_BUS = re.compile(r"^(?:[0-9A-Fa-f]{4})?([0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7])$")


class NvidiaUnreadable(Exception):
    """nvidia-smi did not answer, or answered something that is not its CSV."""


def runner(binary: Path, timeout: float = TIMEOUT_S) -> NvidiaSmi:
    """nvidia-smi at `binary` — `/usr/bin/nvidia-smi` where the NVIDIA
    Container Toolkit mounts it into LocalAI's container,
    `/usr/lib/wsl/lib/nvidia-smi` under WSL2 — with its own directory on the
    library path (WSL's libnvidia-ml lives beside it)."""
    env = {"PATH": f"{Path(binary).parent}:/usr/local/bin:/usr/bin:/bin",
           "LD_LIBRARY_PATH": ":".join(p for p in (str(Path(binary).parent),
                                                   os.environ.get("LD_LIBRARY_PATH", "")) if p),
           "HOME": os.environ.get("HOME", "/tmp")}

    def run(args: list[str]) -> str:
        try:
            out = subprocess.run([str(binary), *args], capture_output=True, text=True,
                                 env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NvidiaUnreadable(f"nvidia-smi {' '.join(args)}: {type(e).__name__}: {e}") from None
        if out.returncode != 0:
            raise NvidiaUnreadable(f"nvidia-smi {' '.join(args)} exited {out.returncode}: "
                                   f"{(out.stderr or out.stdout).strip()[-300:]}")
        return out.stdout
    return run


def pci_of(bus_id: str) -> str:
    """NVML's bus id (`00000000:01:00.0`, an 8-digit domain) as `lspci -D`
    prints it and `cards` keys it (`0000:01:00.0`), lower case."""
    found = _BUS.match(bus_id.strip())
    if not found:
        raise NvidiaUnreadable(f"nvidia-smi gave a bus id that is not one: {bus_id!r}")
    return found.group(1).lower()


def _rows(smi: NvidiaSmi, fields: list[str]) -> list[list[str]]:
    text = smi([f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(fields):
            raise NvidiaUnreadable(f"nvidia-smi answered {line.strip()!r} for {', '.join(fields)}")
        rows.append(cells)
    return rows


def gpus(smi: NvidiaSmi) -> dict[str, dict[str, str]]:
    """{PCI address: {"name", "driver"}} for every card NVML sees."""
    return {pci_of(bus): {"name": name, "driver": driver}
            for bus, name, driver in _rows(smi, ["pci.bus_id", "name", "driver_version"])}


def used_vram_mib(smi: NvidiaSmi) -> dict[str, int]:
    """{PCI address: memory in use on the card, MiB}, card-wide — what a
    load moved is read from this before and after it (`device_on_nvidia`)."""
    out: dict[str, int] = {}
    for bus, used in _rows(smi, ["pci.bus_id", "memory.used"]):
        try:
            out[pci_of(bus)] = int(float(used))
        except ValueError:
            raise NvidiaUnreadable(f"nvidia-smi gave memory.used {used!r} for {bus}") from None
    return out


def mig_enabled(smi: NvidiaSmi) -> set[str]:
    """The cards whose MIG mode is on. A process on a MIG slice holds its
    whole card's /dev/nvidia<N> and moves that card's memory, so it would be
    stated as the whole card — fewer SMs, other sums. Refused until a slice
    is measured (docs/notes/nvidia-support.md); a card without MIG says
    `[N/A]`."""
    return {pci_of(bus) for bus, mode in _rows(smi, ["pci.bus_id", "mig.mode.current"])
            if mode.strip().lower() == "enabled"}


def driver_line(smi: NvidiaSmi, pci: str) -> str:
    """The card's driver as `runs.driver_string` states it where sysfs has no
    PCI bus (WSL2: the paravirtual device and the Windows driver's version)."""
    card = gpus(smi).get(pci.lower())
    if card is None:
        raise NvidiaUnreadable(f"nvidia-smi lists no card at {pci}")
    if not card["driver"]:
        raise NvidiaUnreadable(f"nvidia-smi names no driver version for the card at {pci}")
    return f"dxg nvidia {card['driver']}"


def minors(proc_root: Path = Path("/proc")) -> dict[int, str]:
    """{device minor: PCI address} from the kernel driver's own listing,
    `/proc/driver/nvidia/gpus/<address>/information` ("Device Minor: N"),
    which the NVIDIA Container Toolkit mounts for the cards it exposes."""
    base = Path(proc_root) / "driver" / "nvidia" / "gpus"
    out: dict[int, str] = {}
    try:
        entries = list(base.iterdir())
    except OSError:
        return out
    for d in entries:
        try:
            found = _MINOR.search((d / "information").read_text(errors="replace"))
        except OSError:
            continue
        if found:
            out[int(found.group(1))] = d.name.lower()
    return out
