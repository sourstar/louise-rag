"""Does this load fit — decided before anything is launched.

docs/notes/rage.md §7. llama-swap knows nothing of memory and loads a model
on its first request, so without this a query for a dormant collection
would launch 8 GB unchecked. The front admits every load, the first request
included.

TWO BUDGETS, AND EVERY LOAD IS CHECKED AGAINST THE HOST'S.
- the card's, for a model placed on it: its `memory.<card>`;
- the host's, for EVERY model: `memory.cpu` when it is placed on the CPU,
  and `host.<card>` when it is placed on a card — because a model on the
  GPU is not free on the host. Measured on the node, 2026-09-19, the eight
  GPU servers RAGE replaces held 6.1 GiB of host RAM between them at rest
  and 22.0 GiB at their load-time peaks (MADLAD alone 8.5, the chat model
  5.6). The first version of this module counted host RAM for CPU
  placements only, and would have let CPU models fill what the GPU models
  were already holding.

    fits  =  what is declared for everything loaded in that budget
             + this model + the reserve   <=   the budget's capacity

OUR OWN MODELS ARE COUNTED AS DECLARED, NEVER AS OBSERVED. A model that is
still loading holds less than it will, and one that spilled to host RAM —
which `xe` allows silently (rage.md §7) — holds less of the card than it
needs. The observed figures are recorded beside the declarations after
every load (placement.py) so a figure that drifts is seen. Declare the PEAK:
loads can overlap, and the peak is when the out-of-memory killer comes.

A STRANGER IS COUNTED AS OBSERVED (operator, 2026-09-20: "yes, definitely").
What a process OUTSIDE RAGE holds of the card — the driver's count of what is
used, less what our own children are seen to hold — is nobody's declaration,
so it enters the card's budget as read (`foreign`). It can only REFUSE a
load the ledger would have admitted, never admit one it would refuse. The
day this was missing, a container outside RAGE took 21.4 GiB of the card,
every resident model went to host RAM and the kernel killed three of them.
Where the card cannot be read (no reader for its driver, no capability)
`foreign` is 0 and the ledger decides alone, as before.

NEVER AN EVICTION FOR A LOAD. A load that does not fit is refused, with what
is loaded, how much is free and how much a stranger holds — and that answer
is Louise's notification. Which model to give up FOR ANOTHER OF OURS is
Louise's decision (docs/notes/embedder-pool-spec.md §3-4): she knows whose
batch is in flight. Giving up IDLE models because a stranger squeezes the
card is another matter, and placement.py's (`relieve`).

AN UNKNOWN CAPACITY REFUSES. A card's capacity is declared in `rage.toml`
and model.load requires it; the host's is the container's limit, read. Where
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
    device: str                         # the budget that decided: a card, or "cpu" = the host
    need_mib: int
    capacity_mib: int | None
    held_mib: int                       # declared, by what is loaded in that budget
    reserve_mib: int
    loaded: tuple[str, ...]             # the models counted in that budget
    reason: str | None = None
    foreign_mib: int = 0                # observed: held by processes outside RAGE

    @property
    def free_mib(self) -> int | None:
        if self.capacity_mib is None:
            return None
        return self.capacity_mib - self.reserve_mib - self.held_mib - self.foreign_mib

    def to_json(self) -> dict:
        return {"device": self.device, "need_mib": self.need_mib,
                "capacity_mib": self.capacity_mib, "held_mib": self.held_mib,
                "reserve_mib": self.reserve_mib, "foreign_mib": self.foreign_mib,
                "free_mib": self.free_mib,
                "loaded": list(self.loaded), "reason": self.reason}


def _budget(cfg: RageConfig, budget: str, what: str, model_id: str, need: int,
            holders: Mapping[str, int], capacity: CapacityReader,
            foreign: int = 0) -> Verdict:
    held = sum(holders.values())
    reserve = cfg.reserve_mib.get(budget, 0)
    total = cfg.capacity_mib.get(budget) or capacity(budget)
    base = dict(device=budget, need_mib=need, capacity_mib=total, held_mib=held,
                reserve_mib=reserve, loaded=tuple(sorted(holders)), foreign_mib=foreign)
    if total is None:
        return Verdict(ok=False, **base, reason=(
            f"the capacity of {what} cannot be read here; declare "
            f"[rage] capacity_mib.{budget} in rage.toml"))
    if held + foreign + need + reserve > total:
        stranger = (f", {foreign} held by processes outside RAGE" if foreign else "")
        return Verdict(ok=False, **base, reason=(
            f"{model_id} needs {need} MiB of {what}; {total - reserve - held - foreign} MiB "
            f"are free of {total} ({held} held by {sorted(holders) or 'nothing'}"
            f"{stranger}, {reserve} reserved)"))
    return Verdict(ok=True, **base)


def fits(cfg: RageConfig, loaded: Mapping[str, str], model_id: str, device: str,
         capacity: CapacityReader, foreign: Mapping[str, int] | None = None) -> Verdict:
    """`loaded` maps a model id to the device it is on (loading ones
    included: they hold their place from the moment they are admitted).
    The card is asked first, then the host; the first budget that says no
    is the answer, and a yes is the host's — the one every load passes.
    `foreign` maps a card to the MiB of it that processes outside RAGE are SEEN
    to hold; absent or 0 where the card is not read."""
    entry = cfg.models[model_id]
    others = {m: d for m, d in loaded.items() if m != model_id and m in cfg.models}
    if device != CPU:
        on_card = {m: cfg.models[m].memory[d] for m, d in others.items() if d == device}
        card = _budget(cfg, device, f"device {device!r}", model_id, entry.memory[device],
                       on_card, capacity, max(0, int((foreign or {}).get(device, 0))))
        if not card.ok:
            return card
    on_host = {m: cfg.models[m].host_need(d) for m, d in others.items()}
    return _budget(cfg, CPU, "host RAM", model_id, entry.host_need(device), on_host, capacity)


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
    """The host's capacity is the container's limit, read. A card's is
    DECLARED (`[rage] capacity_mib`, required by model.load for every card a
    model names): the card does not change, a declared figure lets the
    operator keep part of it for another tenant, and reading it would mean
    importing the GPU probe — the one module that sees the host's processes,
    which nothing else may import (tests/test_gpu_panel.py)."""
    return cgroup_capacity_mib() if device == CPU else None
