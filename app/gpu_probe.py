"""Reads the node's GPUs: each card's memory, and every process's share of it.

Runs as its own service (`python -m app.gpu_probe`, the `gpu-probe` service
of deploy/truenas-app.yaml) and answers GET /gpu with app/gpu.py's
snapshot. It is the one Louise process that may see the host's processes:
a per-process figure needs the host's PID namespace, and a container that
has it can read every other container's files and environment through
/proc. So it gets that and nothing else — no data, no config, no secrets,
no LAN port — and the web, which faces the LAN, asks it over HTTP and never
imports this module (tests/test_gpu_panel.py).

Where each figure comes from (measured on the node, 2026-09-18: kernel
6.18, xe, Arc Pro B70):
- a card's memory: the driver's own query (DRM_IOCTL_XE_DEVICE_QUERY, the
  stable uAPI; its `used` is reported only to CAP_PERFMON), amdgpu's sysfs
  files. No other driver is read;
- a process's memory: the kernel's DRM fdinfo (`drm-resident-vram0` …), one
  record per DRM client, summed per process. The eight model servers
  summed to the driver's count less 25 MB of 25.7 GB;
- what a process serves: its command line — llama-server's `-m`, a Python
  server's directory, Ollama's blob named through its manifest;
- a card's name: pci.ids, pinned in the image (Dockerfile). The node's own
  copy calls the Arc Pro B70 "Battlemage G21 [Intel Graphics]".
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import re
import signal
import struct
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

from .gpu import GpuDevice, GpuProcess, GpuSnapshot, to_json

PROC = Path("/proc")
SYS_DRM = Path("/sys/class/drm")
DEV_DRI = Path("/dev/dri")
#: the image's pinned copy first (Dockerfile), then the distribution's
PCI_IDS_PATHS = (Path("/usr/share/louise/pci.ids"), Path("/usr/share/misc/pci.ids"),
                 Path("/usr/share/hwdata/pci.ids"))

# include/uapi/drm/xe_drm.h. DRM_IOWR(DRM_COMMAND_BASE + DRM_XE_DEVICE_QUERY,
# struct drm_xe_device_query): dir 3, size 40, type 'd', nr 0x40.
DRM_IOCTL_XE_DEVICE_QUERY = 0xC0286440
XE_QUERY_MEM_REGIONS = 1
XE_MEM_REGION_CLASS_VRAM = 1
_XE_QUERY = struct.Struct("<QIIQQQ")          # extensions, query, size, data, reserved[2]
_XE_REGIONS = struct.Struct("<II")            # num_mem_regions, pad
_XE_REGION = struct.Struct("<HHIQQQQ48x")     # class, instance, min_page_size, total_size,
                                              # used, cpu_visible_size, cpu_visible_used, reserved[6]
_ioctl = fcntl.ioctl                          # tests replace it

CAP_SYS_ADMIN, CAP_PERFMON = 21, 38

_CARD = re.compile(r"card\d+$")
_MEM = re.compile(r"drm-(total|memory|resident)-([a-z0-9]+):\s*(\d+)(?:\s*([KMG])iB)?\s*$")
_UNIT = {None: 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
# a container's id as a cgroup path segment: /docker/<id> on the host, but
# /../<id> seen from the probe's own cgroup namespace (the node, 2026-09-18),
# /system.slice/docker-<id>.scope under systemd's driver
_CGROUP = re.compile(r"[/-]([0-9a-f]{64})(?:\.scope)?(?=/|$)", re.M)
_PYTHON = re.compile(r"python[\d.]*$")
#: a script named like this says nothing; its directory names the server
_GENERIC_SCRIPTS = {"server", "main", "app", "serve", "run", "__main__"}
_MODEL_SUFFIXES = {".gguf", ".bin", ".safetensors", ".onnx", ".xml", ".pt"}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------
# the cards

def cards() -> list[Path]:
    """/sys/class/drm/cardN — the cards, not their connectors (card0-DP-1)."""
    try:
        return sorted((p for p in SYS_DRM.iterdir() if _CARD.fullmatch(p.name)),
                      key=lambda p: int(p.name[4:]))
    except OSError:
        return []


def pci_name(vendor_id: str, device_id: str) -> tuple[str | None, str | None]:
    """(vendor, device) from the first pci.ids found; a device line only
    counts inside its own vendor's block."""
    for path in PCI_IDS_PATHS:
        try:
            lines = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            vendor = None
            for line in lines:
                if vendor is None:
                    if line[:4].lower() == vendor_id and line[4:6] == "  ":
                        vendor = line[6:].strip()
                    continue
                if not line.startswith("\t"):
                    if line.strip() and not line.startswith("#"):
                        break                       # the next vendor's block
                    continue
                if not line.startswith("\t\t") and line[1:5].lower() == device_id:
                    return vendor, line[7:].strip()
            if vendor is not None:
                return vendor, None
    return None, None


def perfmon_capable(status: str | None = None) -> bool:
    """Whether the driver will tell us what is used: CAP_PERFMON or
    CAP_SYS_ADMIN in this process's effective set."""
    status = status if status is not None else (_read(PROC / "self" / "status") or "")
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            caps = int(line.split()[1], 16)
            return bool(caps & (1 << CAP_PERFMON) or caps & (1 << CAP_SYS_ADMIN))
    return False


def _xe_query(fd: int, query: int) -> bytes:
    """The two-call protocol: the kernel says the size, then fills it."""
    head = bytearray(_XE_QUERY.pack(0, query, 0, 0, 0, 0))
    _ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, head, True)
    size = _XE_QUERY.unpack(head)[2]
    data = ctypes.create_string_buffer(size)
    head = bytearray(_XE_QUERY.pack(0, query, size, ctypes.addressof(data), 0, 0))
    _ioctl(fd, DRM_IOCTL_XE_DEVICE_QUERY, head, True)
    return data.raw


def xe_vram(render: Path) -> tuple[int, int]:
    """(total, used) over the card's VRAM regions — one per tile."""
    fd = os.open(render, os.O_RDWR | os.O_CLOEXEC)
    try:
        raw = _xe_query(fd, XE_QUERY_MEM_REGIONS)
    finally:
        os.close(fd)    # before the process scan: the probe never lists itself
    count, _ = _XE_REGIONS.unpack_from(raw)
    total = used = 0
    for i in range(count):
        mem_class, _inst, _page, size, in_use, _vis, _vis_used = _XE_REGION.unpack_from(
            raw, _XE_REGIONS.size + i * _XE_REGION.size)
        if mem_class == XE_MEM_REGION_CLASS_VRAM:
            total += size
            used += in_use
    return total, used


def _int(path: Path) -> int | None:
    text = _read(path)
    try:
        return int(text.strip()) if text is not None else None
    except ValueError:
        return None


def card_vram(card: Path, driver: str, notes: list[str]) -> tuple[int | None, int | None]:
    """(total, used); used is None where the driver will not say."""
    if driver == "xe":
        renders = sorted((card / "device" / "drm").glob("renderD*"))
        if not renders:
            notes.append(f"{card.name}: no render node")
            return None, None
        try:
            total, used = xe_vram(DEV_DRI / renders[0].name)
        except OSError as e:
            notes.append(f"{card.name}: the driver's memory query failed ({e})")
            return None, None
        if not perfmon_capable():
            notes.append(f"{card.name}: the driver reports used memory only to CAP_PERFMON — "
                         f"used is the processes' sum")
            return total, None
        return total, used
    if driver == "amdgpu":
        return (_int(card / "device" / "mem_info_vram_total"),
                _int(card / "device" / "mem_info_vram_used"))
    return None, None


# --------------------------------------------------------------------------
# the processes

def parse_fdinfo(text: str) -> tuple[str, str, dict[str, int]] | None:
    """(pdev, client id, bytes per memory region) of a DRM file, else None.
    A region's resident figure wins over amdgpu's legacy `drm-memory-`,
    which wins over `drm-total-`."""
    pdev = client = None
    found: dict[str, dict[str, int]] = {"total": {}, "memory": {}, "resident": {}}
    for line in text.splitlines():
        if line.startswith("drm-pdev:"):
            pdev = line.split(":", 1)[1].strip()
        elif line.startswith("drm-client-id:"):
            client = line.split(":", 1)[1].strip()
        else:
            m = _MEM.match(line)
            if m:
                kind, region, value, unit = m.groups()
                found[kind][region] = int(value) * _UNIT[unit]
    if pdev is None or client is None:
        return None
    regions: dict[str, int] = {}
    for kind in ("total", "memory", "resident"):
        regions.update(found[kind])
    return pdev, client, regions


def _argv(pid_dir: Path) -> list[str]:
    raw = _read(pid_dir / "cmdline") or ""
    return [a for a in raw.split("\0") if a]


def _flag(args: list[str], names: tuple[str, ...]) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                return arg[len(name) + 1:]
    return None


def ollama_name(root: Path, blob: PurePosixPath) -> str | None:
    """The tag whose manifest names this blob as its model: Ollama's runner
    is started on `…/models/blobs/sha256-<hex>` and nothing else says which
    model that is. Read through the process's own root."""
    manifests = root / str(blob.parent.parent).lstrip("/") / "manifests"
    digest = "sha256:" + blob.name[len("sha256-"):]
    for dirpath, _dirs, files in sorted(os.walk(manifests)):
        for fname in sorted(files):
            path = Path(dirpath) / fname
            try:
                layers = json.loads(path.read_text()).get("layers", [])
            except (OSError, ValueError, AttributeError):
                continue
            if any(isinstance(layer, dict) and layer.get("digest") == digest
                   and str(layer.get("mediaType", "")).endswith(".model") for layer in layers):
                parts = list(path.relative_to(manifests).parts)
                if len(parts) < 2:
                    continue
                repo, tag = parts[:-1], parts[-1]
                if repo[0] == "registry.ollama.ai":
                    repo = repo[1:]
                if repo and repo[0] == "library":
                    repo = repo[1:]
                return "/".join(repo) + ":" + tag
    return None


def label(argv: list[str], root: Path) -> str:
    """What the process serves, from its command line."""
    if not argv:
        return "?"
    exe, args = PurePosixPath(argv[0]).name, argv[1:]
    if _PYTHON.fullmatch(exe):
        skip = False
        for i, arg in enumerate(args):
            if skip:
                skip = False
                continue
            if arg == "-m" and i + 1 < len(args):
                return args[i + 1]
            if arg == "-c":
                return exe
            if arg in ("-X", "-W"):       # options that take a value
                skip = True
                continue
            if arg.startswith("-"):
                continue
            script = PurePosixPath(arg)
            return (script.parent.name if script.stem in _GENERIC_SCRIPTS and script.parent.name
                    else script.stem)
        return exe
    model = _flag(args, ("-m", "--model"))
    if model is None:
        return exe
    path = PurePosixPath(model)
    if path.name.startswith("sha256-") and path.parent.name == "blobs":
        name = ollama_name(root, path)
        return f"{name} (ollama)" if name else f"{path.name[:19]}… (ollama)"
    return path.stem if path.suffix in _MODEL_SUFFIXES else path.name


def container(pid_dir: Path) -> str:
    """The container's hostname when one was given, else its short id;
    "host" for a process outside any container."""
    m = _CGROUP.search(_read(pid_dir / "cgroup") or "")
    if not m:
        return "host"
    full = m.group(1)
    hostname = (_read(pid_dir / "root" / "etc" / "hostname") or "").strip()
    return hostname if hostname and not full.startswith(hostname) else full[:12]


def scan() -> dict[str, list[GpuProcess]]:
    """Every process holding GPU memory, per card (pdev), largest first.

    Reads only the fdinfo of descriptors that point into /dev/dri; a
    process the probe may not read is left out, and its memory shows as
    the driver's unattributed remainder. A client's figures are counted
    once however many descriptors share it (dup, fork)."""
    clients: dict[tuple[str, int], dict[str, dict[str, int]]] = {}
    try:
        entries = [e.name for e in os.scandir(PROC) if e.name.isdigit()]
    except OSError:
        return {}
    for name in entries:
        pid_dir = PROC / name
        try:
            fds = os.listdir(pid_dir / "fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if not os.readlink(pid_dir / "fd" / fd).startswith("/dev/dri/"):
                    continue
            except OSError:
                continue
            text = _read(pid_dir / "fdinfo" / fd)
            parsed = parse_fdinfo(text) if text else None
            if parsed is not None:
                pdev, client, regions = parsed
                clients.setdefault((pdev, int(name)), {})[client] = regions
    out: dict[str, list[GpuProcess]] = {}
    for (pdev, pid), per_client in clients.items():
        vram = sum(v for r in per_client.values() for k, v in r.items() if k.startswith("vram"))
        host = sum(v for r in per_client.values() for k, v in r.items() if not k.startswith("vram"))
        if vram == 0 and host == 0:
            continue          # a client that holds nothing (an enumeration, a probe)
        pid_dir = PROC / str(pid)
        argv = _argv(pid_dir)
        out.setdefault(pdev, []).append(GpuProcess(
            pid=pid, label=label(argv, pid_dir / "root"),
            command=PurePosixPath(argv[0]).name if argv else "?",
            container=container(pid_dir), vram=vram, host=host))
    for procs in out.values():
        procs.sort(key=lambda p: (-p.vram, -p.host, p.pid))
    return out


# --------------------------------------------------------------------------
# the snapshot, and the service

def snapshot() -> GpuSnapshot:
    notes: list[str] = []
    found = []
    for card in cards():
        dev = card / "device"
        try:
            driver = Path(os.readlink(dev / "driver")).name
        except OSError:
            continue
        vendor_id = (_read(dev / "vendor") or "").strip().lower().removeprefix("0x")
        device_id = (_read(dev / "device") or "").strip().lower().removeprefix("0x")
        vendor, name = pci_name(vendor_id, device_id)
        total, used = card_vram(card, driver, notes)
        found.append((card.name, dev.resolve().name, driver, vendor_id, device_id,
                      vendor, name, total, used))
    per_card = scan()     # after every query has closed its descriptor
    return GpuSnapshot(
        devices=tuple(GpuDevice(card=c, pdev=pdev, driver=drv, vendor_id=vid, device_id=did,
                                vendor=ven, name=nm, vram_total=tot, vram_used=use,
                                processes=tuple(per_card.get(pdev, ())))
                      for c, pdev, drv, vid, did, ven, nm, tot, use in found),
        notes=tuple(notes))


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:   # noqa: N802 — http.server's name
        if self.path == "/health":
            self._send(200, b"ok", "text/plain")
        elif self.path == "/gpu":
            try:
                body, status = json.dumps(to_json(snapshot())).encode(), 200
            except Exception as e:   # noqa: BLE001 — said to the web, which shows it
                body, status = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), 500
            self._send(status, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:   # noqa: A002
        pass     # the panel polls every 5 s; a line per poll says nothing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.gpu_probe", description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--once", action="store_true", help="print one snapshot as JSON and exit")
    ns = parser.parse_args(argv)
    if ns.once:
        print(json.dumps(to_json(snapshot()), indent=2))
        return 0
    # a stateless reader: nothing to finish on a stop but the reply in flight
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("0.0.0.0", ns.port), _Handler)
    print(f"gpu-probe: serving /gpu on :{server.server_address[1]}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
