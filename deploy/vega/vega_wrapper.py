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
§3). Only the card is read, from the child's DRM clients, as RAGE reads it.

THE ENGINE'S `image` IS THE BACKEND'S IMAGE: the OCI digest LocalAI's gallery
wrote into the backend's `metadata.json` at install. PENDING THE AMENDMENT OF
09 §9.3 (vega.md §4): today `image` names the image the platform runs in. A
backend without a digest — installed by hand — states no run, and the wrapper
refuses to start.

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
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.hold.codex import canonical_json
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
CONFIG_VARIABLE = "VEGA_CONFIG"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
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
    directory: Path
    run_sh: Path
    name: str
    build: str
    image: str


def real_backend(directory: Path) -> RealBackend:
    """The real backend, from what LocalAI's gallery wrote at its install
    (`metadata.json`: name, version, uri, digest — core/gallery/backend_types.go).
    Without an image digest no run can be stated: loadopts.Unstatable."""
    directory = Path(directory).resolve()
    run_sh = directory / "run.sh"
    if not run_sh.is_file():
        raise loadopts.Unstatable(f"{directory} holds no run.sh: not a LocalAI backend")
    try:
        meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise loadopts.Unstatable(f"{directory}/metadata.json cannot be read "
                                  f"({type(e).__name__}): the backend's image is unknown")
    if not isinstance(meta, dict):
        raise loadopts.Unstatable(f"{directory}/metadata.json is not an object")
    image = meta.get("digest")
    if not isinstance(image, str) or not _DIGEST.fullmatch(image):
        raise loadopts.Unstatable(
            f"{directory}/metadata.json carries no image digest (digest={image!r}): a backend "
            f"states a run only when LocalAI's gallery installed it from an OCI image, by "
            f"digest (09 §9.3 as amended by docs/notes/vega.md §4)")
    build = meta.get("version") or meta.get("uri")
    if not isinstance(build, str) or not build:
        raise loadopts.Unstatable(f"{directory}/metadata.json names neither a version nor "
                                  f"a uri: the engine's build is unknown")
    name = meta.get("name") if isinstance(meta.get("name"), str) and meta["name"] else directory.name
    return RealBackend(directory, run_sh, name, build, image)


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
              device: str, pci: str | None, weights: str, runtime: list[str],
              sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc")
              ) -> tuple[str, dict[str, Any]]:
    """(run_id, run) of 09 §9.12, from what this wrapper launched."""
    missing = sorted(k for k, v in canon.env.items() if environ.get(k) != v)
    if missing:
        raise loadopts.Unstatable(f"the backend does not carry the environment it was loaded "
                                  f"with: {', '.join(missing)}")
    driver = runs.driver_string(CPU if device == CPU else "card", pci=pci, sys_root=sys_root,
                                proc_root=proc_root, runtime_lines=runtime)
    engine = {"name": f"localai/{backend.name}", "build": backend.build, "variant": None,
              "image": backend.image,
              "args": [str(backend.run_sh), "--addr", f"127.0.0.1:{PORT_PLACEHOLDER}"],
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
                 sys_root: Path = Path("/sys"), dpkg_status: Path = Path("/var/lib/dpkg/status")
                 ) -> None:
        self.cfg, self.backend, self.child, self.receipts, self.pb = cfg, backend, child, receipts, pb
        self.environ = os.environ if environ is None else environ
        self.hash_weights = hash_weights
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

            def hash_them() -> None:
                try:
                    hashed["digest"] = self.hash_weights(Path(canon.weights_path))
                except Exception as e:               # noqa: BLE001 — said to LocalAI
                    hashed["error"] = e

            hasher = threading.Thread(target=hash_them, name="vega-weights", daemon=True)
            hasher.start()                           # while the backend reads the same bytes
            answer = forward(raw)
            if not self.pb.Result.FromString(answer).success:
                return answer                        # the backend's own refusal, as it said it
            hasher.join(WEIGHTS_TIMEOUT_S)
            try:
                if hasher.is_alive():
                    raise loadopts.Unstatable(f"the weights were not hashed within "
                                              f"{WEIGHTS_TIMEOUT_S:g} s")
                if "error" in hashed:
                    raise loadopts.Unstatable(f"the weights cannot be hashed: {hashed['error']}")
                device, pci = device_of(family(self.child.pid, self.proc_root), self.cfg.cards,
                                        self.procfs)
                run_id, run = state_run(
                    backend=self.backend, canon=canon, environ=self.environ, device=device,
                    pci=pci, weights=hashed["digest"],
                    runtime=runtime_lines(self.cfg.runtime_packages, self.dpkg_status),
                    sys_root=self.sys_root, proc_root=self.proc_root)
                if not self.child.alive():
                    raise loadopts.Unstatable("the backend exited while its run was computed")
            except (loadopts.Unstatable, runs.DriverUnknown, OSError) as e:
                return self._refuse_load(e)
            self.loaded = Loaded(run_id, run, declares_pairs(canon.options.get("Options")),
                                 self.child.pid)
            log(f"{self.backend.name}: {Path(canon.weights_path).name} loaded on {device}, "
                f"run {run_id[:23]}…")
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
                    context.invocation_metadata()), timeout=context.time_remaining())
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
                            timeout=context.time_remaining())
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
