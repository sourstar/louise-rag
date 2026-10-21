"""The run identity — computed from what is running, never from a declaration.

docs/03-checking.md §3.3; docs/09-formats.md §9.3 (the engine, the weights
digest) and §9.12 (the run identity), as amended 8bbc66a:

    run_id = H( canonical JSON of {"driver", "engine", "weights"} )

THREE RULES MAKE IT COMPUTABLE, and this module is the three of them.
- `image` comes from a deployment pinned by digest (`boot_image`): a process
  cannot see the digest of the image it runs in, and a tag is not an image.
- `args`, `env` and `options` are read from what LocalAI launched, with the
  port and the paths as placeholders (the wrapper, loadopts.py): the
  raw argument vector carries a port and a path, and hashed, every restart
  would be another run and re-gate every space.
- `weights` are hashed at every launch, from the bytes on disk.

The canonical JSON and the hash are the Codex's own (app/hold/codex.py):
there is one definition of each, and an identity is never computed by a
second one.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from ..hold.codex import canonical_json, digest
from .model import CPU

MIB = 1 << 20
#: kernel modules that drive a card. Read ONLY where no card address is declared
#: and exactly one of them is loaded (`driver_string`): a list is no way to tell
#: two manufacturers' cards apart
GPU_MODULES = ("xe", "i915", "amdgpu", "nvidia")


class WeightsError(RuntimeError):
    """The weights cannot be hashed as 09 §9.3 requires."""


# ---- the weights digest (09 §9.3) ------------------------------------------

def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(MIB), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def weights_digest(path: Path) -> str:
    """One file: `H(bytes)`. A directory: `H` of the canonical JSON mapping
    every regular file's path, relative and `/`-separated, to its digest.
    A symbolic link is refused; code the model loads from its directory is
    a file like any other, because it is part of the function."""
    path = Path(path)
    if path.is_symlink():
        raise WeightsError(f"{path} is a symbolic link; the weights are named by the "
                           f"bytes they are (09 §9.3)")
    if path.is_file():
        return _file_digest(path)
    if not path.is_dir():
        raise WeightsError(f"{path} is neither a file nor a directory")
    manifest: dict[str, str] = {}
    for p in sorted(path.rglob("*")):
        if p.is_symlink():
            raise WeightsError(f"{p} is a symbolic link; a model directory holds its "
                               f"files (09 §9.3)")
        if p.is_file():
            manifest[p.relative_to(path).as_posix()] = _file_digest(p)
    if not manifest:
        raise WeightsError(f"{path} holds no file")
    return digest(canonical_json(manifest))


# ---- the engine, the driver, the run ----------------------------------------

def engine_id(engine: Mapping[str, Any]) -> str:
    return digest(canonical_json(dict(engine)))


def run_object(*, driver: str, engine: Mapping[str, Any], weights: str) -> dict[str, Any]:
    return {"driver": driver, "engine": dict(engine), "weights": weights}


def run_id(run: Mapping[str, Any]) -> str:
    return digest(canonical_json(dict(run)))


class DriverUnknown(ValueError):
    """The card's kernel driver cannot be read, so no run can be stated for a
    model placed on it (09 §9.12: the driver is part of what a run IS)."""


def card_driver(pci: str, *, sys_root: Path = Path("/sys")) -> str:
    """The kernel driver the device at this PCI address is BOUND to. The PCI
    bus is every manufacturer's: `xe`, `i915`, `amdgpu` and `nvidia` cards
    alike are a directory under `/sys/bus/pci/devices` with a `driver` link."""
    device = sys_root / "bus" / "pci" / "devices" / pci
    if not device.is_dir():
        raise DriverUnknown(f"there is no PCI device {pci} on this host "
                            f"(the wrapper's `cards` names the card's address, as `lspci -D` prints it)")
    try:
        return os.path.basename(os.readlink(device / "driver"))
    except OSError:
        raise DriverUnknown(f"PCI device {pci} is bound to no kernel driver") from None


def driver_string(device: str, *, pci: str | None = None, sys_root: Path = Path("/sys"),
                  proc_root: Path = Path("/proc"),
                  runtime_versions: Path = Path("/etc/vega/runtime-versions"),
                  runtime_lines: list[str] | None = None,
                  card_driver_line: str | None = None) -> str:
    """The device's kernel driver and compute runtime, with versions, as
    this platform reads them; `"cpu"` on a CPU (09 §9.12).

    THE DRIVER IS THE ONE THE CARD IS BOUND TO (`pci`, the card's address,
    declared in the wrapper's `cards`; `card_driver`). It used to be "the first GPU
    module loaded" from a fixed list, which is right only on a host with one
    manufacturer's driver: the reference node loads `xe` for its Arc AND
    `amdgpu` for the processor's own graphics, and read `xe` because `xe`
    came first in the tuple — an NVIDIA card beside that AMD graphics would
    have been stated as `amdgpu` (operator, 2026-09-20: no manufacturer in
    RAGE). Without a declared address the old reading stands ONLY where it
    cannot be wrong — exactly one GPU module loaded; with several, no run is
    stated and the reason says what to declare.

    The kernel and the module are the host's, so they are read. The
    compute runtime is the image's: its package versions are written into
    the image when it is built, one `name version` a line, and read back here — no
    package manager runs at start. `runtime_lines` gives those lines instead,
    for a caller that reads them elsewhere: VeGa's wrapper, in LocalAI's
    image, which nobody here built (deploy/vega/vega_wrapper.py reads dpkg's
    status file).

    `card_driver_line` is the card's driver as read where sysfs has no PCI
    bus: under WSL2 the card is `/dev/dxg` and its driver is Windows',
    which app/vega/dxg.py reads from amd-smi (`dxg <version>`). It stands in
    for the bound module, never beside it."""
    if device == CPU:
        return CPU

    def first_line(p: Path) -> str | None:
        try:
            return p.read_text(errors="replace").splitlines()[0].strip() or None
        except (OSError, IndexError):
            return None

    parts = [f"kernel {first_line(proc_root / 'sys/kernel/osrelease') or 'unknown'}"]
    module: str | None
    if card_driver_line:
        parts.append(card_driver_line)
        module = None
    elif pci:
        module = card_driver(pci, sys_root=sys_root)
    else:
        loaded = [m for m in GPU_MODULES if (sys_root / "module" / m).is_dir()]
        if len(loaded) > 1:
            raise DriverUnknown(
                f"this host loads several GPU drivers ({', '.join(loaded)}) and the wrapper's "
                f"`cards` does not say which card `{device}` is: map its PCI address "
                f"(`lspci -D`) to it, and the driver is read from there")
        module = loaded[0] if loaded else None
    if module:
        version = (first_line(sys_root / "module" / module / "version")
                   or first_line(sys_root / "module" / module / "srcversion"))
        parts.append(f"{module} {version}" if version else module)
    try:
        lines = (runtime_versions.read_text(errors="replace").splitlines()
                 if runtime_lines is None else runtime_lines)
        for line in lines:
            if line.strip():
                parts.append(" ".join(line.split()))
    except OSError:
        pass
    return "; ".join(parts)
