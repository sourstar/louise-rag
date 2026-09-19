"""Does this load fit — decided before anything is launched.

docs/notes/rage.md §7. llama-swap knows nothing of memory and loads a model
on its first request, so without this a query for a dormant collection
would launch 8 GB unchecked. The front admits every load, the first request
included.

    fits  =  declared memory of what is loaded on the device
             + this model + the reserve   <=   the device's capacity

DECLARED, NOT OBSERVED. The sum is over `memory` as `rage.toml` states it:
a model that is still loading holds less than it will, and one that spilled
to host RAM — which `xe` allows silently (rage.md §7) — holds less than it
needs. The observed peak is recorded beside the declaration after every
load (front.py) so a figure that drifts is seen; it never decides.

NEVER AN EVICTION. A load that does not fit is refused, with what is loaded
and how much is free. Which model to give up is Louise's decision
(docs/notes/embedder-pool-spec.md §3-4): she knows whose batch is in flight.

AN UNKNOWN CAPACITY REFUSES. A card's capacity is declared in `rage.toml`
and model.load requires it; the CPU's is the container's limit, read. Where
neither gives a figure nothing is admitted there, and the reason names the
key to set. Admitting blind is how the out-of-memory killer took the CPU
encoder four times on 2026-09-15.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from .model import CPU, RageConfig

MIB = 1 << 20
CapacityReader = Callable[[str], int | None]      # device -> MiB, or None: unknown


@dataclass(frozen=True)
class Verdict:
    ok: bool
    device: str
    need_mib: int
    capacity_mib: int | None
    held_mib: int                       # declared, by what is loaded on the device
    reserve_mib: int
    loaded: tuple[str, ...]             # the models loaded on the device
    reason: str | None = None

    @property
    def free_mib(self) -> int | None:
        if self.capacity_mib is None:
            return None
        return self.capacity_mib - self.reserve_mib - self.held_mib

    def to_json(self) -> dict:
        return {"device": self.device, "need_mib": self.need_mib,
                "capacity_mib": self.capacity_mib, "held_mib": self.held_mib,
                "reserve_mib": self.reserve_mib, "free_mib": self.free_mib,
                "loaded": list(self.loaded), "reason": self.reason}


def fits(cfg: RageConfig, loaded: Mapping[str, str], model_id: str, device: str,
         capacity: CapacityReader) -> Verdict:
    """`loaded` maps a model id to the device it is on (loading ones
    included: they hold their place from the moment they are admitted)."""
    entry = cfg.models[model_id]
    need = entry.memory[device]
    on_device = tuple(sorted(m for m, d in loaded.items() if d == device and m != model_id))
    held = sum(cfg.models[m].memory[device] for m in on_device if m in cfg.models)
    reserve = cfg.reserve_mib.get(device, 0)
    total = cfg.capacity_mib.get(device) or capacity(device)
    base = dict(device=device, need_mib=need, capacity_mib=total, held_mib=held,
                reserve_mib=reserve, loaded=on_device)
    if total is None:
        return Verdict(ok=False, **base, reason=(
            f"the capacity of device {device!r} cannot be read here; declare "
            f"[rage] capacity_mib.{device} in rage.toml"))
    if held + need + reserve > total:
        return Verdict(ok=False, **base, reason=(
            f"{model_id} needs {need} MiB on {device}; {total - reserve - held} MiB are "
            f"free of {total} ({held} held by {list(on_device) or 'nothing'}, "
            f"{reserve} reserved)"))
    return Verdict(ok=True, **base)


# ---- reading a device's capacity --------------------------------------------

def cgroup_capacity_mib(root: Path = Path("/sys/fs/cgroup"),
                        meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """The container's memory limit; the host's RAM where there is none."""
    try:
        raw = (root / "memory.max").read_text().strip()
        if raw.isdigit():
            return int(raw) // MIB
    except OSError:
        pass
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def system_capacity(device: str) -> int | None:
    """The CPU's capacity is the container's limit, read. A card's is
    DECLARED (`[rage] capacity_mib`, required by model.load for every card a
    model names): the card does not change, a declared figure lets the
    operator keep part of it for another tenant, and reading it would mean
    importing the GPU probe — the one module that sees the host's processes,
    which nothing else may import (tests/test_gpu_panel.py)."""
    return cgroup_capacity_mib() if device == CPU else None
