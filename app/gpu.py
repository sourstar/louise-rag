"""The System page's GPU panel: each card's memory and every process's share.

The figures are read by app/gpu_probe.py, which runs in a container of its
own — the one Louise process that may see the host's processes — and the
web asks it over HTTP (GPU_PROBE_URL), never reading /proc itself. This
module is the half both sides share: the snapshot's shape and JSON, the
figures the page derives from it, and the web's bounded fetch.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass

import httpx

#: the fetch's bound: the panel reloads every 5 s and must never hold a
#: worker thread of the web on a probe that is gone
TIMEOUT_S = 1.5


@dataclass(frozen=True)
class GpuProcess:
    """One process's buffers on one card, summed over its DRM clients."""
    pid: int
    label: str        # what it serves, read from its command line
    command: str      # the executable's name
    container: str    # the container's hostname or short id; "host" outside one
    vram: int         # bytes resident in the card's memory
    host: int         # bytes of its GPU buffers held in host RAM (GTT, system)


@dataclass(frozen=True)
class GpuDevice:
    card: str
    pdev: str
    driver: str
    vendor_id: str
    device_id: str
    vendor: str | None
    name: str | None
    vram_total: int | None
    vram_used: int | None    # the driver's own count; None where it gives none
    processes: tuple[GpuProcess, ...] = ()

    @property
    def attributed(self) -> int:
        return sum(p.vram for p in self.processes)

    @property
    def used(self) -> int | None:
        """The driver's count, else the processes' sum (on the node the two
        differed by 25 MB of 25.7 GB, 2026-09-18)."""
        if self.vram_total is None:
            return None
        return self.vram_used if self.vram_used is not None else self.attributed

    @property
    def free(self) -> int | None:
        used = self.used
        return None if used is None or self.vram_total is None else max(0, self.vram_total - used)

    @property
    def used_pct(self) -> float:
        used = self.used
        return 0.0 if not self.vram_total or used is None else min(100.0, 100.0 * used / self.vram_total)

    @property
    def unattributed(self) -> int:
        """What the driver counts and no process holds: its own buffers, and
        the processes the probe may not read."""
        return 0 if self.vram_used is None else max(0, self.vram_used - self.attributed)

    def share(self, bytes_: int) -> float:
        return 0.0 if not self.vram_total else 100.0 * bytes_ / self.vram_total


@dataclass(frozen=True)
class GpuSnapshot:
    devices: tuple[GpuDevice, ...]
    notes: tuple[str, ...] = ()


def to_json(snapshot: GpuSnapshot) -> dict:
    return asdict(snapshot)


def from_json(data: dict) -> GpuSnapshot:
    return GpuSnapshot(
        devices=tuple(
            GpuDevice(**{**d, "processes": tuple(GpuProcess(**p) for p in d.get("processes", ()))})
            for d in data.get("devices", ())),
        notes=tuple(data.get("notes", ())))


def size(n: int | None) -> str:
    """Binary units, as the driver counts: '23.96 GiB', '266 MiB'."""
    if n is None:
        return "—"
    for unit, scale, digits in (("GiB", 1 << 30, 2), ("MiB", 1 << 20, 0), ("KiB", 1 << 10, 0)):
        if n >= scale:
            return f"{n / scale:.{digits}f} {unit}"
    return f"{n} B"


def _get_json(url: str) -> dict:
    """Split out so tests stub the network without patching httpx, which
    TestClient itself runs on."""
    response = httpx.get(url, timeout=TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def panel(url: str) -> dict:
    """The fragment's context: unset, unreachable (with why), or the cards."""
    if not url:
        return {"state": "unset", "url": url}
    try:
        snapshot = from_json(_get_json(url.rstrip("/") + "/gpu"))
    except Exception as e:   # noqa: BLE001 — any failure is the panel's
        return {"state": "unreachable", "url": url,   # state, never the page's
                "error": f"{type(e).__name__}: {e}"}
    return {"state": "ok", "url": url, "devices": snapshot.devices, "notes": snapshot.notes}


def governor() -> dict:
    """What the GPU governor last said (app/gpu_governor.py), for the
    System tab: `running` (with whether the clock is pinned now), `error`
    (it runs and cannot set the clock), or `absent` (no heartbeat within
    GOVERNOR_STALE_S). Absent is allowed — chat is slower on an idle card,
    and the tab says so — so this never raises."""
    from . import config, db, node_state
    try:
        if not node_state.path().is_file():
            return {"state": "absent"}
        with closing(node_state.connect()) as con:
            payload, age = db._read_sample(con, "gpu_governor", config.GOVERNOR_STALE_S)
    except Exception:   # noqa: BLE001 — a bad reading must not break the page
        return {"state": "absent"}
    if payload is None or payload.get("stopped"):
        return {"state": "absent"}
    if payload.get("error"):
        return {"state": "error", "error": payload["error"]}
    return {"state": "running", "pinned": bool(payload.get("pinned")),
            "rest_mhz": payload.get("rest_mhz"), "max_mhz": payload.get("max_mhz")}
