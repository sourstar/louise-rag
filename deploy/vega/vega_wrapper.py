"""VeGa's wrapper: a LocalAI backend in front of a real one (docs/notes/vega.md).

LocalAI installs and launches it like any backend, as `vega-<backend>`:
`run.sh --addr 127.0.0.1:<port>`. It launches the REAL backend — LocalAI's own
install of it, the directory `vega.json` names — on a private loopback port,
as its child, and proxies gRPC to it. Two methods are intercepted:

- `LoadModel`: the ModelOptions exactly as LocalAI sent them, the weights
  hashed WHILE the backend loads them, and the run computed from what this
  process launched — the same identity as RAGE's (app/rage/runs.py,
  loadopts.py: 09 §9.12), published to VeGa's receipts service.
- `Embedding`: refused without a valid tag (app/vega/tag.py); the tag
  stripped and the bare text — or the pair, in the form the backend reads —
  forwarded; the receipt {id, run_id, input_digest, vector_digest} delivered
  to VeGa and ACKNOWLEDGED BEFORE the vector goes back. No acknowledgement
  within RECEIPT_TIMEOUT_S fails the call UNAVAILABLE: never a vector without
  its receipt.

EVERY OTHER METHOD IS FORWARDED AS OPAQUE BYTES, as a bidirectional stream —
on the wire every gRPC call is one, whatever the proto calls it — so a LocalAI
release that adds methods passes through unchanged. A release that adds LOAD
OPTIONS does not: options this file's backend.proto cannot name would be
missing from the run, so such a load is refused until the wrapper is rebuilt
against that release.

WHAT THE WRAPPER KNOWS THAT RAGE HAD TO READ. It is the backend's parent: the
argument vector is the one it passed, the environment the one it gave, the
options the ones it received — no `/proc` matching, no LocalAI patch (vega.md
§3). Only the card is read, from the child's DRM clients, as RAGE reads it,
and the environment run.sh gave the program it started (the driver it loads).

THE ENGINE'S `image` IS THE BACKEND'S INSTALLED FILES, hashed at every load
(`tree_digest`) — never the digest LocalAI's gallery wrote into
`metadata.json`, which is the registry's word for a tag, not the bytes'
(vega.md §4, operator 2026-10-08: no pinning; §10 finding 3). An upgrade is
therefore a new run. The engine's driver is the one the backend loads: the
driver library its environment points Level Zero at, or the one it carries
(`driver_libraries`; §10 finding 4). PENDING THE AMENDMENT OF 09 §9.3 (vega.md
§4): today `image` names the image the platform runs in.

THE PACKAGE (what LocalAI's backends path holds as `vega-<backend>/`):
    run.sh  vega_wrapper.py  vega.json  metadata.json  venv/
    backend_pb2.py                      generated from LocalAI's backend.proto
                                        of the release LocalAI runs
    grpc_auth.py  model_identity.py  parent_watch.py   LocalAI's, same release
    app/__init__.py  app/hold/{__init__,codex}.py
    app/rage/{__init__,model,procfs,runs,loadopts}.py  app/vega/{__init__,tag}.py
The `app/` files are Louise's, copied as RAGE's image copies the codex: ONE
definition of canonical JSON, the run and the tag. None imports more than the
standard library; the venv holds grpcio and protobuf (requirements.txt).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.hold.codex import canonical_json, digest
from app.rage import loadopts, runs
from app.rage.model import CPU
from app.rage.procfs import ProcFS
from app.vega import tag as vtag

LOAD = "/backend.Backend/LoadModel"
EMBED = "/backend.Backend/Embedding"
#: How long a receipt may take to be acknowledged, reconnects included. The
#: receipts service answers in well under a millisecond on the same host; two
#: seconds is a hung or absent service, and the call then fails UNAVAILABLE —
#: a deferral to Louise, never a vector without its receipt.
RECEIPT_TIMEOUT_S = 2.0
#: The weights are hashed while the backend loads them; the load waits at most
#: this long for the hash. 8 GB hash in about 5 s from a warm SSD (rage-over-
#: localai §8.3); half an hour is a disk that is failing.
WEIGHTS_TIMEOUT_S = 1800.0
#: A stop lets the call in progress finish within this, as LocalAI's backends do.
GRACE_S = 30.0
#: As LocalAI's own Python backends.
MAX_MESSAGE = 50 * 1024 * 1024
#: Calls in flight at once. A stream holds its thread while it lasts.
WORKERS = 16
#: What a model's YAML declares for a pair model (app/rage/pairs.py OPTION).
PAIRS_OPTION = "pairs:true"
#: The compute runtime the run's driver names, read from the image's dpkg
#: status (RAGE's list, deploy/rage/Dockerfile.localai).
RUNTIME_PACKAGES = ("libze-intel-gpu1", "intel-opencl-icd", "libze1")
PORT_PLACEHOLDER = "${PORT}"
#: The backend's directory in a backend engine's `args`, whose bytes `image`
#: names (09 §9.3, a backend engine): the vector is relative to it.
BACKEND_PLACEHOLDER = "${BACKEND}"
CONFIG_VARIABLE = "VEGA_CONFIG"
#: LocalAI's gallery writes it into a backend's directory at install: its
#: record of the install (name, uri, the registry's digest, the time), not a
#: byte the backend runs. Left out of the tree, so a reinstall of the same
#: bytes is the same engine.
INSTALL_RECORD = "metadata.json"
#: Level Zero's override of driver discovery, a comma-separated list of driver
#: libraries; LocalAI's run.sh exports it for a backend that carries its own.
ALT_DRIVERS_VARIABLE = "ZE_ENABLE_ALT_DRIVERS"
#: The driver library a backend carries in its `lib/`, as LocalAI's run.sh
#: names it (backend/cpp/llama-cpp/run.sh, read on the node 2026-10-08).
BUNDLED_DRIVER_GLOB = "libze_intel_gpu.so.*"
_PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")


def log(message: str) -> None:
    print(f"[vega] {message}", file=sys.stderr, flush=True)


class ConfigError(ValueError):
    """vega.json is not valid; the wrapper refuses to start and says why."""


class Refusal(Exception):
    """A call the wrapper fails, with the gRPC status code's name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Unavailable(Exception):
    """The receipts service did not acknowledge in time."""


class ReceiptRefused(Exception):
    """The receipts service answered, and refused."""


# ---- configuration and the real backend -------------------------------------------

@dataclass(frozen=True)
class Config:
    backend: Path                         # the real backend's directory
    socket: Path                          # VeGa's intake
    cards: Mapping[str, str] = field(default_factory=dict)   # PCI address -> device name
    runtime_packages: tuple[str, ...] = RUNTIME_PACKAGES


def load_config(path: Path, backends_root: Path) -> Config:
    """vega.json: `backend` (a directory name under LocalAI's backends path,
    or absolute), `receipts_socket`, `cards` ({PCI address: the device as its
    driver names it, 09 §9.3}) and optionally `runtime_packages`."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {type(e).__name__}: {e}") from None
    if not isinstance(doc, dict):
        raise ConfigError(f"{path} is a JSON object")
    unknown = set(doc) - {"backend", "receipts_socket", "cards", "runtime_packages"}
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {sorted(unknown)}")
    backend, sock = doc.get("backend"), doc.get("receipts_socket")
    if not isinstance(backend, str) or not backend:
        raise ConfigError(f"{path}: `backend` names the real backend's directory")
    if not isinstance(sock, str) or not sock.startswith("/"):
        raise ConfigError(f"{path}: `receipts_socket` is the absolute path of VeGa's socket")
    cards = doc.get("cards", {})
    if not isinstance(cards, dict) or not all(
            isinstance(k, str) and _PCI.fullmatch(k) and isinstance(v, str) and v
            for k, v in cards.items()):
        raise ConfigError(f"{path}: `cards` maps a PCI address (`lspci -D`, like "
                          f"\"0000:03:00.0\") to the device as its driver names it")
    packages = doc.get("runtime_packages", list(RUNTIME_PACKAGES))
    if not isinstance(packages, list) or not all(isinstance(p, str) and p for p in packages):
        raise ConfigError(f"{path}: `runtime_packages` is a list of package names")
    directory = Path(backend) if backend.startswith("/") else Path(backends_root) / backend
    return Config(backend=directory, socket=Path(sock), cards=dict(cards),
                  runtime_packages=tuple(packages))


@dataclass(frozen=True)
class RealBackend:
    """The real backend's directory and what its install record says. The
    record names the engine (`name`) and, when it declares one, its `build`
    (`version`); `uri` and `registry_digest` are logged at each load and are
    NEVER identity: the engine's image is the tree on disk (`tree_digest`)."""
    directory: Path
    run_sh: Path
    name: str
    version: str | None = None
    uri: str | None = None
    registry_digest: str | None = None


def _text(meta: Mapping[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def real_backend(directory: Path) -> RealBackend:
    """The real backend: its directory, its run.sh, and what LocalAI's gallery
    wrote at its install (`metadata.json`: name, version, uri, digest —
    core/gallery/backend_types.go). A backend without that record — installed
    by hand — is named by its directory; its identity is its bytes either way."""
    directory = Path(directory).resolve()
    run_sh = directory / "run.sh"
    if not run_sh.is_file():
        raise loadopts.Unstatable(f"{directory} holds no run.sh: not a LocalAI backend")
    record = directory / INSTALL_RECORD
    if not record.exists():
        return RealBackend(directory, run_sh, directory.name)
    try:
        meta = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise loadopts.Unstatable(f"{record} cannot be read ({type(e).__name__}): the "
                                  f"engine's name and build are unknown")
    if not isinstance(meta, dict):
        raise loadopts.Unstatable(f"{record} is not an object")
    return RealBackend(directory, run_sh, _text(meta, "name") or directory.name,
                       _text(meta, "version"), _text(meta, "uri"), _text(meta, "digest"))


# ---- the backend's bytes: the tree, its driver ---------------------------------------

class FileDigests:
    """The sha256 of files, read again only when a file is not the one read
    before. Keyed on the PHYSICAL path (`os.path.realpath`), never a name, and
    held to the file's identity: (st_dev, st_ino, st_size, st_mtime_ns,
    st_ctime_ns). An upgrade that replaces a file gives it a new inode; a
    write in place moves its ctime, which no tool sets back (`tar` restores
    mtimes, so mtime alone is not enough). A file that changes while it is
    read is refused, never named."""

    def __init__(self) -> None:
        self._known: dict[str, tuple[tuple[int, ...], int, str]] = {}
        self._lock = threading.Lock()
        self.reads: list[str] = []                    # physical paths read, in order

    @staticmethod
    def _identity(st: os.stat_result) -> tuple[int, ...]:
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    def read(self, path: Path | str) -> tuple[int, str]:
        """(size, sha256 hex) of the regular file at `path`."""
        physical = os.path.realpath(path)
        before = os.stat(physical)
        if not stat.S_ISREG(before.st_mode):
            raise loadopts.Unstatable(f"{physical} is not a regular file")
        ident = self._identity(before)
        with self._lock:
            known = self._known.get(physical)
        if known is not None and known[0] == ident:
            return known[1], known[2]
        h = hashlib.sha256()
        with open(physical, "rb") as f:
            for block in iter(lambda: f.read(runs.MIB), b""):
                h.update(block)
        if self._identity(os.stat(physical)) != ident:
            raise loadopts.Unstatable(f"{physical} changed while it was hashed")
        with self._lock:
            self._known[physical] = (ident, before.st_size, h.hexdigest())
            self.reads.append(physical)
        return before.st_size, h.hexdigest()


def tree_digest(root: Path, files: FileDigests | None = None) -> str:
    """The digest of a backend's installed directory: a backend engine's
    `image`, 09 §9.3's tree digest byte for byte (vega.md §4, §9).

        H( canonical JSON (09 §9.1) of { <path>: <entry>, ... } )

    over every entry below `root` (`root` itself excluded), walked without
    following symbolic links, except the top-level `metadata.json`
    (INSTALL_RECORD). <path> is relative to `root`, `/`-separated, UTF-8; a
    name that is not UTF-8 is refused. <entry> is:
    - a regular file: {"type": "file", "exec": <bool: any of the mode's three
      execute bits is set>, "size": <bytes>, "sha256": "<64 lowercase hex of
      its contents>"};
    - a symbolic link: {"type": "link", "target": "<readlink's string>"},
      never followed;
    - a directory: {"type": "dir"}, an empty one included;
    anything else (a socket, a FIFO, a device) is refused. Owners, times and
    the other mode bits are not in it. H is sha256, written `sha256:<hex>`, as
    the weights digest of a directory is (app/rage/runs.py) — the form 09
    §9.3's validator reads for an image. Unchanged files are not read again
    (`files`, a FileDigests)."""
    files = files if files is not None else FileDigests()
    root = Path(root)
    manifest: dict[str, dict[str, Any]] = {}

    def walk(directory: str, prefix: str) -> None:
        with os.scandir(directory) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if not prefix and e.name == INSTALL_RECORD:
                continue
            rel = prefix + e.name
            try:
                rel.encode("utf-8")
            except UnicodeEncodeError:
                raise loadopts.Unstatable(f"{e.path!r}: a name that is not UTF-8 has no "
                                          f"canonical form") from None
            st = e.stat(follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                manifest[rel] = {"type": "link", "target": os.readlink(e.path)}
            elif stat.S_ISDIR(st.st_mode):
                manifest[rel] = {"type": "dir"}
                walk(e.path, rel + "/")
            elif stat.S_ISREG(st.st_mode):
                size, sha = files.read(e.path)
                manifest[rel] = {"type": "file", "exec": bool(st.st_mode & 0o111),
                                 "size": size, "sha256": sha}
            else:
                raise loadopts.Unstatable(f"{e.path} is neither a file, a directory nor a "
                                          f"symbolic link: the backend's tree has no digest")

    walk(str(root), "")
    try:
        return digest(canonical_json(manifest))
    except UnicodeEncodeError:
        raise loadopts.Unstatable(f"{root}: a link target that is not UTF-8 has no "
                                  f"canonical form") from None


def short_build(tree: str) -> str:
    """The build an engine names when its install record declares no
    version: the tree digest's first 12 hex digits, no prefix (09 §9.3, a
    backend engine)."""
    return tree.removeprefix("sha256:")[:12]


def environment_of(pids: Iterable[int], proc_root: Path = Path("/proc")
                   ) -> list[dict[str, str]]:
    """The environment each process started with (`/proc/<pid>/environ`),
    run.sh's exports included for the program it `exec`s or starts; the
    processes whose environment cannot be read are left out."""
    out: list[dict[str, str]] = []
    for pid in pids:
        try:
            raw = (Path(proc_root) / str(pid) / "environ").read_bytes()
        except OSError:
            continue
        env: dict[str, str] = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                env[key.decode("utf-8", "surrogateescape")] = value.decode("utf-8",
                                                                           "surrogateescape")
        out.append(env)
    return out


def driver_libraries(directory: Path, pids: list[int], given: Mapping[str, str],
                     proc_root: Path = Path("/proc")) -> list[Path]:
    """The compute driver libraries the backend loads, as it was started:
    1. ALT_DRIVERS_VARIABLE in the environment of the backend's processes,
       read from /proc (`given`, the environment the wrapper passed, when none
       can be read) — what run.sh exported, or what the model's `env:` set;
    2. otherwise the driver its own `lib/` carries (BUNDLED_DRIVER_GLOB);
    3. otherwise none, and the run's driver is read as before (dpkg).
    Each named path must be absolute; two processes naming different ones,
    or a `lib/` carrying two different drivers, state no run."""
    observed = environment_of(pids, proc_root)
    values = {e[ALT_DRIVERS_VARIABLE] for e in observed if e.get(ALT_DRIVERS_VARIABLE)}
    if not observed and given.get(ALT_DRIVERS_VARIABLE):
        values = {given[ALT_DRIVERS_VARIABLE]}
    if len(values) > 1:
        raise loadopts.Unstatable(f"the backend's processes name different drivers in "
                                  f"{ALT_DRIVERS_VARIABLE}: {sorted(values)}")
    if values:
        (value,) = values
        paths = [p.strip() for p in value.split(",") if p.strip()]
        relative = [p for p in paths if not p.startswith("/")]
        if relative:
            raise loadopts.Unstatable(f"{ALT_DRIVERS_VARIABLE} names a driver by a relative "
                                      f"path ({relative[0]!r}): which file loads is unknown")
        return [Path(p) for p in paths]
    carried = {os.path.realpath(p) for p in sorted((Path(directory) / "lib")
                                                   .glob(BUNDLED_DRIVER_GLOB))}
    if len(carried) > 1:
        raise loadopts.Unstatable(f"{directory}/lib carries several drivers "
                                  f"({sorted(Path(p).name for p in carried)})")
    return [Path(p) for p in carried]


def driver_lines(libraries: Iterable[Path], files: FileDigests) -> list[str]:
    """`<file name> sha256:<hex>` of each driver library, the file a symbolic
    link names: its name carries its version (libze_intel_gpu.so.1.15.39122),
    its bytes are what loads."""
    lines = []
    for lib in libraries:
        physical = Path(os.path.realpath(lib))
        try:
            _size, sha = files.read(physical)
        except OSError as e:
            raise loadopts.Unstatable(f"the driver {lib} cannot be read: {e}") from None
        lines.append(f"{physical.name} sha256:{sha}")
    return lines


def runtime_lines(packages: Iterable[str], status: Path = Path("/var/lib/dpkg/status")) -> list[str]:
    """`name version` of each installed package, as `dpkg-query -W` prints it,
    read from dpkg's status file: no package manager runs."""
    wanted = list(packages)
    try:
        text = Path(status).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: dict[str, str] = {}
    for paragraph in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in paragraph.splitlines():
            if line[:1] in (" ", "\t"):
                continue
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        name = fields.get("Package")
        if name in wanted and "installed" in fields.get("Status", "").split():
            found[name] = fields.get("Version", "")
    return [f"{p} {found[p]}" for p in wanted if p in found]


def family(pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """The process and its descendants: a backend whose run.sh does not
    `exec` holds the card from a child."""
    out: list[int] = []
    todo = [pid]
    while todo:
        p = todo.pop()
        if p in out:
            continue
        out.append(p)
        try:
            tasks = list((Path(proc_root) / str(p) / "task").iterdir())
        except OSError:
            continue
        for t in tasks:
            try:
                todo.extend(int(k) for k in (t / "children").read_text().split() if k.isdigit())
            except OSError:
                continue
    return out


def device_of(pids: list[int], cards: Mapping[str, str], procfs: ProcFS) -> tuple[str, str | None]:
    """(the device as its driver names it, its PCI address) of the card the
    processes hold memory on; (cpu, None) when they hold none. Read, never
    declared: a backend that fell back to the CPU states a CPU run."""
    on: dict[str | None, int] = {}
    for i, pid in enumerate(pids):
        held = procfs.card_mib_by_device(pid)
        if held is None:
            if i == 0:
                raise loadopts.Unstatable("which card the backend is on cannot be read: its "
                                          "DRM clients are not visible in /proc")
            continue                                  # a descendant gone since it was listed
        for pdev, mib in held.items():
            if mib > 0:
                on[pdev] = on.get(pdev, 0) + mib
    if not on:
        return CPU, None
    if len(on) > 1:
        raise loadopts.Unstatable(f"the backend holds memory on several cards "
                                  f"({sorted(map(str, on))}); a run is on one device")
    (pdev,) = on
    if pdev is None:
        if len(cards) == 1:
            pci, name = next(iter(cards.items()))
            return name, pci
        raise loadopts.Unstatable("the card's driver names no address (drm-pdev) and "
                                  "vega.json names several cards")
    if pdev not in cards:
        raise loadopts.Unstatable(f"the backend holds memory on the card at {pdev}, which "
                                  f"vega.json's `cards` names no device for")
    return cards[pdev], pdev


def declares_pairs(options: Any) -> bool:
    """Whether a load's `Options` declare a pair model (app/rage/pairs.py)."""
    return isinstance(options, list) and any(
        isinstance(o, str) and o.replace(" ", "") == PAIRS_OPTION for o in options)


# ---- the load options and the run ---------------------------------------------------

def canon_from_protojson(text: str) -> loadopts.Canon:
    """ModelOptions as protojson with proto field names, unset fields omitted
    — the form P1 lists them in (deploy/localai/patches) — as 09 §9.3's
    options: RAGE's canonicalisation, byte for byte."""
    return loadopts.canonical(loadopts.parse(text))


def load_options(pb: Any, raw: bytes) -> loadopts.Canon:
    """The options of a LoadModel request exactly as received. Fields this
    backend.proto does not know are refused, never dropped: an option missing
    from the run is a declaration where 02 §2.4 asks for what ran."""
    from google.protobuf import json_format
    try:
        msg = pb.ModelOptions.FromString(raw)
    except Exception as e:                              # noqa: BLE001 — protobuf's DecodeError
        raise loadopts.Unstatable(f"the load call is not a ModelOptions: {e}") from None
    known = pb.ModelOptions()
    known.CopyFrom(msg)
    known.DiscardUnknownFields()
    if known.SerializeToString(deterministic=True) != msg.SerializeToString(deterministic=True):
        raise loadopts.Unstatable(
            "LocalAI sent load options this wrapper's backend.proto cannot name (a newer "
            "LocalAI?); they would be missing from the run. Rebuild the wrapper against "
            "the backend.proto of the LocalAI release it runs under")
    return canon_from_protojson(json_format.MessageToJson(msg, preserving_proto_field_name=True))


def state_run(*, backend: RealBackend, canon: loadopts.Canon, environ: Mapping[str, str],
              device: str, pci: str | None, weights: str, tree: str, runtime: list[str],
              sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc")
              ) -> tuple[str, dict[str, Any]]:
    """(run_id, run) of 09 §9.12, from what this wrapper launched: `tree` the
    backend's tree digest read at this load, `runtime` the driver lines
    (`driver_lines`, or dpkg's `runtime_lines` for a backend carrying none).
    The build is the install record's declared `version`, else the tree's
    short form (`short_build`) — never the uri's tag, which moves."""
    missing = sorted(k for k, v in canon.env.items() if environ.get(k) != v)
    if missing:
        raise loadopts.Unstatable(f"the backend does not carry the environment it was loaded "
                                  f"with: {', '.join(missing)}")
    driver = runs.driver_string(CPU if device == CPU else "card", pci=pci, sys_root=sys_root,
                                proc_root=proc_root, runtime_lines=runtime)
    engine = {"name": f"localai/{backend.name}",
              "build": backend.version or short_build(tree), "variant": None,
              "image": tree,
              "args": [f"{BACKEND_PLACEHOLDER}/{backend.run_sh.name}", "--addr",
                       f"127.0.0.1:{PORT_PLACEHOLDER}"],
              "env": dict(canon.env), "device": device, "options": canon.options}
    run = runs.run_object(driver=driver, engine=engine, weights=weights)
    return runs.run_id(run), run


# ---- the receipts service's client ---------------------------------------------------

class ReceiptClient:
    """One connection to VeGa's intake, kept open, requests one at a time.
    Each connection is told the current run before its first receipt, so a
    receipts service that restarted learns it again."""

    def __init__(self, path: Path, timeout_s: float = RECEIPT_TIMEOUT_S) -> None:
        self.path, self.timeout_s = Path(path), timeout_s
        self._sock: socket.socket | None = None
        self._reader: Any = None
        self._told: str | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        for thing in (self._reader, self._sock):
            try:
                if thing is not None:
                    thing.close()
            except OSError:
                pass
        self._sock = self._reader = self._told = None

    def _exchange(self, msg: Mapping[str, Any], deadline: float) -> dict[str, Any]:
        if self._sock is None:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                s.connect(str(self.path))
            except OSError:
                s.close()
                raise
            self._sock, self._reader = s, s.makefile("rb")
        self._sock.settimeout(max(0.001, deadline - time.monotonic()))
        self._sock.sendall(canonical_json(dict(msg)) + b"\n")
        line = self._reader.readline(1 << 16)
        if not line.endswith(b"\n"):
            raise OSError("the receipts service closed the connection")
        reply = json.loads(line)
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise ValueError("the receipts service's answer is not one")
        return reply

    def _send(self, msg: Mapping[str, Any], run_id: str, run: Mapping[str, Any]) -> None:
        deadline = time.monotonic() + self.timeout_s
        last: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                if self._told != run_id:
                    reply = self._exchange({"kind": "run", "run_id": run_id, "run": run},
                                           deadline)
                    if not reply["ok"]:
                        raise ReceiptRefused(f"the run was refused: {reply.get('error')}")
                    self._told = run_id
                if msg.get("kind") == "run":
                    return
                reply = self._exchange(msg, deadline)
                if reply["ok"]:
                    return
                if reply.get("code") == "unknown-run":
                    self._told = None                 # it forgot: tell it again
                    continue
                raise ReceiptRefused(str(reply.get("error")))
            except (OSError, ValueError) as e:        # socket timeouts are OSErrors
                last = e
                self.close()
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        why = f"{type(last).__name__}: {last}" if last else "no time left"
        raise Unavailable(f"no acknowledgement within {self.timeout_s:g} s ({why})")

    def publish(self, run_id: str, run: Mapping[str, Any]) -> None:
        with self._lock:
            self._send({"kind": "run"}, run_id, run)

    def deliver(self, receipt: Mapping[str, str], run_id: str, run: Mapping[str, Any]) -> None:
        with self._lock:
            self._send({"kind": "receipt", **receipt}, run_id, run)


# ---- the child: the real backend --------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Child:
    """The real backend, started as LocalAI starts one: its run.sh, from its
    own directory, with `--addr` on loopback, in this process's environment —
    the one LocalAI gave the wrapper, the model's `env:` included."""

    def __init__(self, run_sh: Path, *, environ: Mapping[str, str] | None = None) -> None:
        self.port = free_port()
        self.argv = [str(run_sh), "--addr", f"127.0.0.1:{self.port}"]
        self.proc = subprocess.Popen(self.argv, cwd=str(Path(run_sh).parent),
                                     env=None if environ is None else dict(environ))

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, grace: float = GRACE_S) -> None:
        if self.proc.poll() is not None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


# ---- the two intercepted methods -------------------------------------------------------

@dataclass(frozen=True)
class Loaded:
    run_id: str
    run: dict[str, Any]
    pairs: bool
    pid: int


class Gate:
    """LoadModel and Embedding, on raw request bytes; `forward` sends bytes
    to the real backend and returns its answer's bytes."""

    def __init__(self, cfg: Config, backend: RealBackend, child: Child, receipts: ReceiptClient,
                 pb: Any, *, environ: Mapping[str, str] | None = None,
                 hash_weights: Callable[[Path], str] = runs.weights_digest,
                 procfs: ProcFS | None = None, proc_root: Path = Path("/proc"),
                 sys_root: Path = Path("/sys"), dpkg_status: Path = Path("/var/lib/dpkg/status"),
                 files: FileDigests | None = None) -> None:
        self.cfg, self.backend, self.child, self.receipts, self.pb = cfg, backend, child, receipts, pb
        self.environ = os.environ if environ is None else environ
        self.hash_weights = hash_weights
        self.files = files if files is not None else FileDigests()
        self.procfs = procfs or ProcFS(proc_root)
        self.proc_root, self.sys_root, self.dpkg_status = proc_root, sys_root, dpkg_status
        self.loaded: Loaded | None = None
        self._load_lock = threading.Lock()

    def _refuse_load(self, why: BaseException) -> bytes:
        log(f"{self.backend.name}: no run can be stated: {why}")
        return self.pb.Result(success=False,
                              message=f"VeGa: no run can be stated: {why}").SerializeToString()

    def load_model(self, raw: bytes, forward: Callable[[bytes], bytes]) -> bytes:
        with self._load_lock:
            self.loaded = None                       # a new load has no run until it is stated
            try:
                canon = load_options(self.pb, raw)
            except loadopts.Unstatable as e:
                return self._refuse_load(e)
            hashed: dict[str, Any] = {}

            def hashing(key: str, compute: Callable[[], str]) -> threading.Thread:
                def run() -> None:
                    try:
                        hashed[key] = compute()
                    except Exception as e:           # noqa: BLE001 — said to LocalAI
                        hashed[f"{key} error"] = e
                t = threading.Thread(target=run, name=f"vega-{key}", daemon=True)
                t.start()
                return t

            # both while the backend loads: the weights it reads, the tree it runs
            hashers = {
                "weights": hashing("weights", lambda: self.hash_weights(Path(canon.weights_path))),
                "tree": hashing("tree", lambda: tree_digest(self.backend.directory, self.files)),
            }
            answer = forward(raw)
            if not self.pb.Result.FromString(answer).success:
                return answer                        # the backend's own refusal, as it said it
            deadline = time.monotonic() + WEIGHTS_TIMEOUT_S
            for t in hashers.values():
                t.join(max(0.0, deadline - time.monotonic()))
            try:
                for key, t in hashers.items():
                    if t.is_alive():
                        raise loadopts.Unstatable(f"the {key} digest was not computed within "
                                                  f"{WEIGHTS_TIMEOUT_S:g} s")
                    if f"{key} error" in hashed:
                        raise loadopts.Unstatable(f"the {key} digest cannot be computed: "
                                                  f"{hashed[f'{key} error']}")
                pids = family(self.child.pid, self.proc_root)
                device, pci = device_of(pids, self.cfg.cards, self.procfs)
                runtime: list[str] = []
                if device != CPU:
                    libraries = driver_libraries(self.backend.directory, pids, self.environ,
                                                 self.proc_root)
                    runtime = (driver_lines(libraries, self.files) if libraries else
                               runtime_lines(self.cfg.runtime_packages, self.dpkg_status))
                run_id, run = state_run(
                    backend=self.backend, canon=canon, environ=self.environ, device=device,
                    pci=pci, weights=hashed["weights"], tree=hashed["tree"], runtime=runtime,
                    sys_root=self.sys_root, proc_root=self.proc_root)
                if not self.child.alive():
                    raise loadopts.Unstatable("the backend exited while its run was computed")
            except (loadopts.Unstatable, runs.DriverUnknown, OSError) as e:
                return self._refuse_load(e)
            self.loaded = Loaded(run_id, run, declares_pairs(canon.options.get("Options")),
                                 self.child.pid)
            log(f"{self.backend.name}: {Path(canon.weights_path).name} loaded on {device}, "
                f"run {run_id[:23]}…, files {hashed['tree'][:19]}… (installed from "
                f"{self.backend.uri or 'nowhere recorded'}, the registry's digest "
                f"{(self.backend.registry_digest or 'none')[:19]}…: informational)")
            try:
                self.receipts.publish(run_id, run)
            except (Unavailable, ReceiptRefused) as e:
                # told again before the first receipt of every connection
                log(f"{self.backend.name}: the run is not published yet: {e}")
            return answer

    def embedding(self, raw: bytes, forward: Callable[[bytes], bytes]) -> bytes:
        state = self.loaded
        if state is None:
            raise Refusal("FAILED_PRECONDITION", "VeGa: no run is stated for this backend (not "
                                                 "loaded, or its load could not be stated); no "
                                                 "vector goes out without one")
        try:
            req = self.pb.PredictOptions.FromString(raw)
        except Exception:                            # noqa: BLE001 — protobuf's DecodeError
            raise Refusal("INVALID_ARGUMENT", "VeGa: the request is not a PredictOptions") from None
        if len(req.EmbeddingTokens):
            raise Refusal("INVALID_ARGUMENT", "VeGa: token ids carry no tag; send the tagged text")
        try:
            tid, form, content = vtag.parse(req.Embeddings)
        except vtag.TagError as e:
            raise Refusal("INVALID_ARGUMENT", f"VeGa: {e}") from None
        if form == vtag.PAIR:
            if not state.pairs:
                raise Refusal("INVALID_ARGUMENT",
                              f"VeGa: a text pair, to {self.backend.name} loaded without "
                              f"{PAIRS_OPTION}; a pair is never flattened into one string")
            req.Embeddings = vtag.backend_pair(*content)
        else:
            if vtag.PAIR_MARK in content:
                raise Refusal("INVALID_ARGUMENT", f"VeGa: a text carries the pair marker "
                                                  f"{vtag.PAIR_MARK!r}, which only VeGa writes")
            req.Embeddings = content
        answer = forward(req.SerializeToString())
        result = self.pb.EmbeddingResult.FromString(answer)
        if result.layout == self.pb.EMBEDDING_LAYOUT_PER_TOKEN:
            raise Refusal("FAILED_PRECONDITION",
                          "VeGa: the backend answered per-token vectors, which LocalAI would "
                          "pool itself — a vector no receipt can name; pool in the backend")
        if self.loaded is not state or self.child.pid != state.pid or not self.child.alive():
            raise Refusal("UNAVAILABLE", "VeGa unavailable: the backend was reloaded while it "
                                         "answered; ask again")
        receipt = {"id": tid, "run_id": state.run_id,
                   "input_digest": vtag.input_digest(form, content),
                   "vector_digest": vtag.vector_digest(list(result.embeddings))}
        try:
            self.receipts.deliver(receipt, state.run_id, state.run)
        except Unavailable as e:
            raise Refusal("UNAVAILABLE", f"VeGa unavailable: the receipt was not "
                                         f"acknowledged: {e}") from None
        except ReceiptRefused as e:
            raise Refusal("INTERNAL", f"VeGa: the receipts service refused the receipt: "
                                      f"{e}") from None
        return answer                                # only now: the receipt is held


# ---- gRPC: the two methods, and everything else as bytes --------------------------------

#: Above this, grpcio's `time_remaining()` means "no deadline": it reports
#: 9.2e18 s for a call that set none, and passed on as a timeout that overflows
#: into a deadline already past. LocalAI v4.11.0 sets none on LoadModel or
#: Embedding, so every load failed DEADLINE_EXCEEDED at once (the node,
#: 2026-10-08, docs/notes/vega.md §10).
NO_DEADLINE_S = 100 * 365 * 24 * 3600.0


def remaining(context: Any) -> float | None:
    """The caller's deadline as a timeout for the forwarded call; None = none."""
    t = context.time_remaining()
    return None if t is None or t > NO_DEADLINE_S else t


def _forwardable(metadata: Iterable[tuple[str, Any]] | None) -> list[tuple[str, Any]]:
    return [(k, v) for k, v in (metadata or ())
            if not (k.startswith(":") or k.startswith("grpc-") or k == "user-agent")]


class Proxy:
    def __init__(self, gate: Gate, channel: Any, grpc_mod: Any) -> None:
        self.gate, self.channel, self.grpc = gate, channel, grpc_mod

    def _abort_from(self, e: Any, context: Any) -> None:
        context.set_trailing_metadata(tuple(_forwardable(e.trailing_metadata())))
        context.abort(e.code(), e.details() or "")

    def _unary(self, method: str, intercept: Callable[[bytes, Callable[[bytes], bytes]], bytes]):
        call = self.channel.unary_unary(method)       # no serializers: bytes both ways
        grpc = self.grpc

        def behaviour(raw: bytes, context: Any) -> bytes:
            def forward(request: bytes) -> bytes:
                response, c = call.with_call(request, metadata=_forwardable(
                    context.invocation_metadata()), timeout=remaining(context))
                context.set_trailing_metadata(tuple(_forwardable(c.trailing_metadata())))
                return response
            try:
                return intercept(raw, forward)
            except grpc.RpcError as e:
                self._abort_from(e, context)
            except Refusal as e:
                context.abort(getattr(grpc.StatusCode, e.code), str(e))
            except Exception as e:                    # noqa: BLE001 — said to LocalAI
                log(f"{method}: {type(e).__name__}: {e}")
                context.abort(grpc.StatusCode.INTERNAL, f"VeGa: {type(e).__name__}: {e}")

        return grpc.unary_unary_rpc_method_handler(behaviour)

    def _stream(self, method: str):
        call = self.channel.stream_stream(method)
        grpc = self.grpc

        def behaviour(requests: Any, context: Any):
            upstream = call(requests, metadata=_forwardable(context.invocation_metadata()),
                            timeout=remaining(context))
            context.add_callback(upstream.cancel)
            try:
                head = _forwardable(upstream.initial_metadata())
                if head:
                    context.send_initial_metadata(tuple(head))
                for response in upstream:
                    yield response
                context.set_trailing_metadata(tuple(_forwardable(upstream.trailing_metadata())))
            except grpc.RpcError as e:
                self._abort_from(e, context)

        return grpc.stream_stream_rpc_method_handler(behaviour)

    def handler(self) -> Any:
        proxy, grpc = self, self.grpc
        intercepted = {LOAD: proxy._unary(LOAD, proxy.gate.load_model),
                       EMBED: proxy._unary(EMBED, proxy.gate.embedding)}

        class Handler(grpc.GenericRpcHandler):
            def service(self, details: Any) -> Any:
                return intercepted.get(details.method) or proxy._stream(details.method)

        return Handler()


@dataclass
class Running:
    server: Any
    child: Child
    channel: Any
    gate: Gate
    port: int

    def stop(self, grace: float = GRACE_S) -> None:
        self.server.stop(grace).wait()
        self.channel.close()
        self.gate.receipts.close()
        self.child.stop(grace)


def serve(cfg: Config, address: str, *, pb: Any, interceptors: Iterable[Any] = (),
          environ: Mapping[str, str] | None = None,
          receipt_timeout_s: float = RECEIPT_TIMEOUT_S, **gate_kw: Any) -> Running:
    """Start the real backend and the proxy in front of it."""
    import grpc
    backend = real_backend(cfg.backend)
    child = Child(backend.run_sh, environ=environ)
    sizes = [("grpc.max_send_message_length", MAX_MESSAGE),
             ("grpc.max_receive_message_length", MAX_MESSAGE)]
    # The child takes seconds to listen; the channel must not back off for
    # minutes meanwhile (gRPC's default goes to 120 s between attempts).
    channel = grpc.insecure_channel(
        f"127.0.0.1:{child.port}",
        options=sizes + [("grpc.initial_reconnect_backoff_ms", 100),
                         ("grpc.min_reconnect_backoff_ms", 100),
                         ("grpc.max_reconnect_backoff_ms", 1000)])
    gate = Gate(cfg, backend, child, ReceiptClient(cfg.socket, receipt_timeout_s), pb,
                environ=environ, **gate_kw)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=WORKERS), options=sizes,
                         interceptors=list(interceptors))
    server.add_generic_rpc_handlers((Proxy(gate, channel, grpc).handler(),))
    port = server.add_insecure_port(address)
    server.start()
    log(f"{backend.name} behind VeGa: listening on port {port}, the backend on "
        f"127.0.0.1:{child.port} (pid {child.pid})")
    return Running(server, child, channel, gate, port)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(allow_abbrev=False)
    p.add_argument("--addr", required=True, help="where LocalAI reaches it: host:port")
    addr = p.parse_args(sys.argv[1:] if argv is None else argv).addr
    # vega.json sits in the package, and the package in LocalAI's backends
    # path beside the real backend: a relative `backend` is read from there
    cfg_path = Path(os.environ.get(CONFIG_VARIABLE)
                    or Path(__file__).resolve().parent / "vega.json").resolve()
    try:
        cfg = load_config(cfg_path, backends_root=cfg_path.parent.parent)
        real_backend(cfg.backend)                    # refuse now, not at the first load
    except (ConfigError, loadopts.Unstatable) as e:
        log(f"refusing to start: {e}")
        return 2
    import backend_pb2
    from grpc_auth import TokenAuthInterceptor      # LocalAI's, the release's own
    from parent_watch import start_parent_death_watcher

    # LocalAI's backstop: a wrapper whose LocalAI died ends itself, and its
    # child — which watches the wrapper the same way — after it. NOT LocalAI's
    # model-identity interceptor: it reads decoded requests, and this proxy
    # passes bytes; the real backend enforces it on the same bytes.
    start_parent_death_watcher()
    token = os.environ.get("LOCALAI_GRPC_AUTH_TOKEN", "")
    try:
        running = serve(cfg, addr, pb=backend_pb2,
                        interceptors=[TokenAuthInterceptor(token)] if token else [])
    except (loadopts.Unstatable, OSError) as e:
        log(f"refusing to start: {e}")
        return 2
    stop = threading.Event()
    code = [0]

    def child_died() -> None:
        running.child.proc.wait()
        if not stop.is_set():
            log(f"the backend exited with {running.child.proc.returncode}; stopping")
            code[0] = 1
            stop.set()

    threading.Thread(target=child_died, name="vega-child", daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    running.stop(GRACE_S)
    return code[0]


if __name__ == "__main__":
    sys.exit(main())
