"""The run identity — computed from what is running, never from a declaration.

docs/03-checking.md §3.3; docs/09-formats.md §9.3 (the engine, the weights
digest) and §9.12 (the run identity), as amended 8bbc66a:

    run_id = H( canonical JSON of {"driver", "engine", "weights"} )

THREE RULES MAKE IT COMPUTABLE, and this module is the three of them.
- `image` comes from a deployment pinned by digest (`boot_image`): a process
  cannot see the digest of the image it runs in, and a tag is not an image.
- `args` and `env` are the ENTRY'S — the template with `${WEIGHTS}` and
  `${PORT}` unsubstituted, and the declared variables only. The raw argument
  vector carries a port and a path, and the raw environment the container's
  host name: hashed, every restart would be another run and re-gate every
  space. What keeps this honest is `match`: the process is checked against
  the template before a run is stated, and one that does not match states none.
- `weights` are hashed at every launch, from the bytes on disk.

The canonical JSON and the hash are the Codex's own (app/hold/codex.py):
there is one definition of each, and an identity is never computed by a
second one.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..hold.codex import canonical_json, digest
from .model import CPU, PORT, WEIGHTS, ModelEntry
from .procfs import ProcFS

MIB = 1 << 20
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_VARIABLE = "RAGE_IMAGE"
#: kernel modules that drive a card; the one loaded names the driver
GPU_MODULES = ("xe", "i915", "amdgpu", "nvidia")


class BootError(RuntimeError):
    """RAGE refuses to start. The message names the variable and the rule."""


class WeightsError(RuntimeError):
    """The weights cannot be hashed as 09 §9.3 requires."""


def boot_image(environ: Mapping[str, str]) -> str:
    """The digest of the image this process runs in, from a deployment
    pinned by digest — or a refusal. A digest-pinned deployment IS the
    observation: the container runtime runs no other image."""
    value = environ.get(IMAGE_VARIABLE, "")
    if not _DIGEST.fullmatch(value):
        raise BootError(
            f"{IMAGE_VARIABLE} must be the digest of the image RAGE runs in, written "
            f"sha256:<64 hex digits>, the same digest the deployment pins in `image:`. "
            f"It is {'unset' if not value else 'not a digest — a tag is not an image'}; "
            f"a run identity cannot be stated without it (docs/09-formats.md §9.12).")
    return value


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

def engine_object(entry: ModelEntry, device: str, *, image: str,
                  device_name: str) -> dict[str, Any]:
    """09 §9.3's engine, as launched: the entry's template for the device
    that ran, its declared environment, the image actually running."""
    return {"name": entry.engine.name, "build": entry.engine.build,
            "variant": entry.engine.variant, "image": image,
            "args": list(entry.template(device)), "env": dict(entry.env),
            "device": device_name}


def engine_id(engine: Mapping[str, Any]) -> str:
    return digest(canonical_json(dict(engine)))


def run_object(*, driver: str, engine: Mapping[str, Any], weights: str) -> dict[str, Any]:
    return {"driver": driver, "engine": dict(engine), "weights": weights}


def run_id(run: Mapping[str, Any]) -> str:
    return digest(canonical_json(dict(run)))


def driver_string(device: str, *, sys_root: Path = Path("/sys"),
                  proc_root: Path = Path("/proc"),
                  runtime_versions: Path = Path("/etc/rage/runtime-versions")) -> str:
    """The device's kernel driver and compute runtime, with versions, as
    this platform reads them; `"cpu"` on a CPU (09 §9.12).

    The kernel and its `xe` module are the host's, so they are read. The
    compute runtime is the image's: its package versions are written into
    the image when it is built (`deploy/rage/Dockerfile`), one `name
    version` a line, and read back here — no package manager runs at start."""
    if device == CPU:
        return CPU

    def first_line(p: Path) -> str | None:
        try:
            return p.read_text(errors="replace").splitlines()[0].strip() or None
        except (OSError, IndexError):
            return None

    parts = [f"kernel {first_line(proc_root / 'sys/kernel/osrelease') or 'unknown'}"]
    for module in GPU_MODULES:            # the first one loaded is the card's
        if (sys_root / "module" / module).is_dir():
            version = (first_line(sys_root / "module" / module / "version")
                       or first_line(sys_root / "module" / module / "srcversion"))
            parts.append(f"{module} {version}" if version else module)
            break
    try:
        for line in runtime_versions.read_text(errors="replace").splitlines():
            if line.strip():
                parts.append(" ".join(line.split()))
    except OSError:
        pass
    return "; ".join(parts)


# ---- is the process the engine we are about to name -------------------------

@dataclass(frozen=True)
class Match:
    pid: int | None
    port: int | None
    reason: str | None        # why no run can be stated; None when one can

    @property
    def ok(self) -> bool:
        return self.reason is None


def _argv_matches(argv: list[str], template: tuple[str, ...], weights: str) -> int | None:
    """The port when `argv` is the template with `${WEIGHTS}` → the path and
    `${PORT}` → an integer, else None."""
    if len(argv) != len(template):
        return None
    port: int | None = None
    for got, want in zip(argv, template):
        if want == PORT:
            if not got.isdigit():
                return None
            port = int(got)
        elif got != (weights if want == WEIGHTS else want):
            return None
    return port


def match(procfs: ProcFS, entry: ModelEntry, device: str) -> Match:
    """Exactly one process whose argument vector is the entry's template and
    whose environment carries every declared variable at its declared value.
    None, or two: no run is stated, and 03 §3.3 already says what that
    means — search with it, write nothing through it."""
    template = entry.template(device)
    found: list[tuple[int, int]] = []
    wrong_env: list[str] = []
    for pid in procfs.pids():
        port = _argv_matches(procfs.cmdline(pid), template, entry.weights)
        if port is None:
            continue
        env = procfs.environ(pid)
        missing = sorted(k for k, v in entry.env.items() if env.get(k) != v)
        if missing:
            wrong_env.append(f"pid {pid}: {', '.join(missing)}")
            continue
        found.append((pid, port))
    if len(found) == 1:
        return Match(pid=found[0][0], port=found[0][1], reason=None)
    if found:
        return Match(None, None, f"{len(found)} processes match the engine of "
                                 f"{entry.variant_id(device)} (pids "
                                 f"{[p for p, _ in found]}); a run is one process")
    if wrong_env:
        return Match(None, None, f"a process has the engine's arguments but not its "
                                 f"declared environment ({'; '.join(wrong_env)})")
    return Match(None, None, f"no process runs the engine of {entry.variant_id(device)} "
                             f"as rage.toml declares it")
