"""A card's memory as its kernel driver counts it — (total, used), in bytes.

ONE VERB, ONE READER PER KERNEL DRIVER. Whoever asks names a PCI device and
gets two numbers or none; which driver answered is this module's business and
nobody else's. The GPU probe asks through here and never learns a
manufacturer's name (operator, 2026-09-20, docs/notes/rage.md §18: no
manufacturer in the platform — a second manufacturer is a second reader behind the same verb, as with the
clock, `app/gpu_clock.py`). A driver with no reader here gives (None, None,
why), and the asker goes on with what it declares, as it did before this
existed.

WHY IT EXISTS (node, 2026-09-20). RAGE admitted loads against what ITS OWN
models declare. A container outside RAGE took 21.4 GiB of the 32 GiB card;
the driver moved every resident model into host RAM without an error to
anyone; the host ran out of memory and the kernel killed three models.
Admission cannot see a stranger on the card unless somebody reads the card.

`xe` reports what is USED only to CAP_PERFMON (or CAP_SYS_ADMIN); without it
the total is read and `used` is None. `amdgpu` publishes both in sysfs.

Read by the GPU probe (`app/gpu_probe.py`) and by tools/profile_models.py;
like `app/gpu_clock.py`, it imports nothing of the platform.
"""
from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from pathlib import Path
from typing import Callable

# include/uapi/drm/xe_drm.h. DRM_IOWR(DRM_COMMAND_BASE + DRM_XE_DEVICE_QUERY,
# struct drm_xe_device_query): dir 3, size 40, type 'd', nr 0x40.
DRM_IOCTL_XE_DEVICE_QUERY = 0xC0286440
XE_QUERY_MEM_REGIONS = 1
XE_MEM_REGION_CLASS_VRAM = 1
_XE_QUERY = struct.Struct("<QIIQQQ")          # extensions, query, size, data, reserved[2]
_XE_REGIONS = struct.Struct("<II")            # num_mem_regions, pad
_XE_REGION = struct.Struct("<HHIQQQQ48x")     # class, instance, min_page_size, total_size,
                                              # used, cpu_visible_size, cpu_visible_used, reserved[6]
CAP_SYS_ADMIN, CAP_PERFMON = 21, 38

Ioctl = Callable[..., object]


def perfmon_capable(status: str | None = None, *,
                    status_path: Path = Path("/proc/self/status")) -> bool:
    """Whether the driver will tell this process what is used: CAP_PERFMON or
    CAP_SYS_ADMIN in its effective set."""
    if status is None:
        try:
            status = status_path.read_text()
        except OSError:
            return False
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            caps = int(line.split()[1], 16)
            return bool(caps & (1 << CAP_PERFMON) or caps & (1 << CAP_SYS_ADMIN))
    return False


def xe_query(fd: int, query: int, *, ioctl: Ioctl = fcntl.ioctl) -> bytes:
    """The two-call protocol: the kernel says the size, then fills it."""
    head = bytearray(_XE_QUERY.pack(0, query, 0, 0, 0, 0))
    ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, head, True)
    size = _XE_QUERY.unpack(head)[2]
    data = ctypes.create_string_buffer(size)
    head = bytearray(_XE_QUERY.pack(0, query, size, ctypes.addressof(data), 0, 0))
    ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, head, True)
    return data.raw


def xe_vram_regions(raw: bytes) -> tuple[int, int]:
    """(total, used) over the card's VRAM regions — one per tile."""
    count, _ = _XE_REGIONS.unpack_from(raw)
    total = used = 0
    for i in range(count):
        mem_class, _inst, _page, size, in_use, _vis, _vis_used = _XE_REGION.unpack_from(
            raw, _XE_REGIONS.size + i * _XE_REGION.size)
        if mem_class == XE_MEM_REGION_CLASS_VRAM:
            total += size
            used += in_use
    return total, used


def xe_vram(render: Path, *, ioctl: Ioctl = fcntl.ioctl) -> tuple[int, int]:
    fd = os.open(render, os.O_RDWR | os.O_CLOEXEC)
    try:
        return xe_vram_regions(xe_query(fd, XE_QUERY_MEM_REGIONS, ioctl=ioctl))
    finally:
        os.close(fd)


def _int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _xe(device: Path, dev_dri: Path, ioctl: Ioctl,
        capable: Callable[[], bool]) -> tuple[int | None, int | None, str | None]:
    renders = sorted(p.name for p in (device / "drm").glob("renderD*"))
    if not renders:
        return None, None, "the card has no render node"
    try:
        total, used = xe_vram(dev_dri / renders[0], ioctl=ioctl)
    except OSError as e:
        return None, None, f"the driver's memory query failed ({e})"
    if not capable():
        return total, None, ("the driver tells what is used only to CAP_PERFMON: add it to "
                             "this container (cap_add: PERFMON)")
    return total, used, None


def _amdgpu(device: Path, _dev_dri: Path, _ioctl: Ioctl,
            _capable: Callable[[], bool]) -> tuple[int | None, int | None, str | None]:
    total, used = _int(device / "mem_info_vram_total"), _int(device / "mem_info_vram_used")
    return total, used, None if total is not None else "the driver publishes no VRAM total"


#: kernel driver -> its reader. The ONLY place a driver is named.
READERS = {"xe": _xe, "amdgpu": _amdgpu}


def driver_of(device: Path) -> str | None:
    """The kernel driver the PCI device is bound to, as the bus says it."""
    try:
        return os.path.basename(os.readlink(device / "driver"))
    except OSError:
        return None


def read(pci: str, *, sys_root: Path = Path("/sys"), dev_dri: Path = Path("/dev/dri"),
         ioctl: Ioctl = fcntl.ioctl, capable: Callable[[], bool] = perfmon_capable,
         ) -> tuple[int | None, int | None, str | None]:
    """(total, used, why not) for the card at this PCI address, in bytes.
    Either number may be None, and then the third says why."""
    device = Path(sys_root) / "bus" / "pci" / "devices" / pci
    driver = driver_of(device)
    if driver is None:
        return None, None, f"no device at {pci}, or it is bound to no driver"
    reader = READERS.get(driver)
    if reader is None:
        return None, None, (f"no memory reader for the {driver} driver: the card's memory is "
                            f"not read, and what the models declare decides alone")
    return reader(device, Path(dev_dri), ioctl, capable)
