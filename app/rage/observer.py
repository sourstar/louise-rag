"""What LocalAI runs, and the run each of its answers states (migration step
15d; docs/notes/rage-over-localai.md §4, §7, §10; 09 §9.12 as amended).

RAGE NO LONGER LAUNCHES ANYTHING. LocalAI starts a backend process per model
and sends it the model's options over gRPC; this module reads what came of
it, and the run identity is computed from that reading and never from a
configuration:
- from LocalAI's `GET /system`, which process serves each model and the
  options its weights were loaded with (`load_options`, P1);
- from the kernel, that the process is there and when it started (pid and
  start ticks name one process for good), its argument vector, its
  environment, and which card its DRM clients hold memory on;
- from the disk, the weights, hashed at every new process.

ONE PROCESS PER MODEL, AND A RELOAD IS A NEW PROCESS (LocalAI 58830f7,
`spawnGRPCModel`: every load starts a backend). So a (pid, start) seen before
and after an answer is the process that gave it, with the options it was
loaded with; a model whose process changed in between states no run for that
answer. A model served from no local process (a remote address) states none.

THE EVENTS ARE WHAT RAGE SEES, not what it decided: a model loaded or
unloaded between two readings, a new run with what changed since the
model's previous one (§10: "bge-m3: new run — NBatch 512 → 2048"), a run
that cannot be stated and why.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import gpu_memory
from . import loadopts, runs
from .localai import LocalAI, LocalAIError
from .localai_config import LocalAIConfig
from .model import CPU
from .procfs import ProcFS
from .store import RunStore

MIB = 1 << 20
PORT_PLACEHOLDER = "${PORT}"
#: LocalAI starts every backend with `--addr 127.0.0.1:<port>` (`process.go`)
_ADDR = re.compile(r"(--addr=)?((?:127\.0\.0\.1|localhost):)(\d+)")
LOADED, UNLOADED, NEW_RUN, UNSTATED = "loaded", "unloaded", "new-run", "unstated"


@dataclass(eq=False)
class Instance:
    """One backend process LocalAI serves a model from."""
    model: str
    pid: int
    ticks: int                       # start, in clock ticks since boot
    started: float | None            # the same, as Unix seconds
    backend: str
    size_vram_mib: int | None = None
    device: str | None = None
    run: str | None = None
    reason: str | None = None        # why no run is stated, once that is known
    engine: dict[str, Any] | None = None
    stating: asyncio.Task | None = None


class Observer:
    def __init__(self, cfg: LocalAIConfig, client: LocalAI, store: RunStore, *, image: str,
                 procfs: ProcFS | None = None, sys_root: Path = Path("/sys"),
                 proc_root: Path = Path("/proc"),
                 runtime_versions: Path = Path("/etc/rage/runtime-versions"),
                 card: Callable[[str], tuple[int | None, int | None, str | None]] | None = None,
                 hash_weights: Callable[[Path], str] = runs.weights_digest,
                 log: Callable[[str], None] = print) -> None:
        self.cfg, self.client, self.store, self.image = cfg, client, store, image
        self.procfs = procfs or ProcFS()
        self.sys_root, self.proc_root, self.runtime_versions = sys_root, proc_root, runtime_versions
        self.card = card or (lambda pci: gpu_memory.read(pci, sys_root=sys_root))
        self.hash_weights = hash_weights
        self.log = log
        self.instances: dict[str, Instance] = {}
        #: model -> (run id, run object) of the last run it stated, for the
        #: event that names what a new one changed
        self.stated: dict[str, tuple[str, dict[str, Any]]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.version: str | None = None
        self._lock = asyncio.Lock()

    # ---- reading ------------------------------------------------------------------

    def event(self, model: str, kind: str, detail: str) -> None:
        self.events.append({"time": int(time.time()), "model": model, "kind": kind,
                            "detail": detail})
        self.log(f"[rage] {model}: {kind}: {detail}")

    def alive(self, inst: Instance) -> bool:
        return self.procfs.start_ticks(inst.pid) == inst.ticks

    def current(self, model: str) -> Instance | None:
        """The model's process, if it is still the one last read."""
        inst = self.instances.get(model)
        return inst if inst is not None and self.alive(inst) else None

    async def refresh(self) -> None:
        """Read `/system` and bring the instances up to it. A new process is
        stated in its own task — hashing its weights takes seconds and must
        hold up no other model."""
        async with self._lock:
            rows = await self.client.system()
            if self.version is None:
                self.version = await self.client.version()
            seen: set[str] = set()
            for row in rows:
                model, proc = row.get("id"), row.get("process")
                if not isinstance(model, str) or not isinstance(proc, dict):
                    continue                    # no local process: nothing to read
                pid = proc.get("pid")
                ticks = self.procfs.start_ticks(pid) if isinstance(pid, int) and pid > 0 else None
                if ticks is None:
                    continue                    # gone since LocalAI looked, or not ours
                seen.add(model)
                vram = row.get("size_vram")
                size = int(vram) // MIB if isinstance(vram, int) else None
                inst = self.instances.get(model)
                if inst is not None and inst.pid == pid and inst.ticks == ticks:
                    inst.size_vram_mib = size
                    continue
                if inst is not None:
                    self._drop(inst, f"pid {inst.pid} replaced by pid {pid}")
                inst = Instance(model, pid, ticks, self.procfs.start_time(pid),
                                str(row.get("backend") or ""), size)
                self.instances[model] = inst
                inst.stating = asyncio.create_task(self._state(inst, row))
            for model in sorted(set(self.instances) - seen):
                gone = self.instances.pop(model)
                self._drop(gone, f"pid {gone.pid} is no longer loaded")

    def _drop(self, inst: Instance, why: str) -> None:
        if inst.stating is not None and not inst.stating.done():
            inst.stating.cancel()
        self.event(inst.model, UNLOADED, why)

    # ---- the run ----------------------------------------------------------------------

    def _args(self, pid: int) -> list[str]:
        argv = self.procfs.cmdline(pid)
        if not argv:
            raise loadopts.Unstatable("its process shows no argument vector")
        out, found = [], 0
        for arg in argv:
            m = _ADDR.fullmatch(arg)
            if m:
                found += 1
                out.append(f"{m.group(1) or ''}{m.group(2)}{PORT_PLACEHOLDER}")
            else:
                out.append(arg)
        if found != 1:
            raise loadopts.Unstatable(
                f"its argument vector carries {found} listening addresses where LocalAI "
                f"passes one (`--addr`): {argv!r}")
        return out

    def _device(self, pid: int) -> str:
        """The card the process holds memory on, by the address its DRM
        clients report; the CPU when it holds none. Read, never declared: a
        model that fell back to the CPU while its YAML asked for the card is
        a CPU run, and says so."""
        held = self.procfs.card_mib_by_device(pid)
        if held is None:
            raise loadopts.Unstatable("which card it is on cannot be read: its DRM clients "
                                      "are not visible in /proc")
        on = {pdev: mib for pdev, mib in held.items() if mib > 0}
        if not on:
            return CPU
        if len(on) > 1:
            raise loadopts.Unstatable(f"it holds memory on several cards ({sorted(map(str, on))}); "
                                      f"a run is on one device")
        (pdev,) = on
        if pdev is None:
            if len(self.cfg.pci) == 1:
                return next(iter(self.cfg.pci))
            raise loadopts.Unstatable("its driver names no card address (drm-pdev) and "
                                      "rage.toml declares several cards")
        card = self.cfg.card_at(pdev)
        if card is None:
            raise loadopts.Unstatable(f"it holds memory on the card at {pdev}, which rage.toml "
                                      f"declares no card for ([rage] pci)")
        return card

    async def _state(self, inst: Instance, row: dict[str, Any]) -> None:
        try:
            canon = loadopts.canonical(row.get("load_options"))
            if not inst.backend:
                raise loadopts.Unstatable("LocalAI names no backend for it")
            args = self._args(inst.pid)
            env = self.procfs.environ(inst.pid)
            missing = sorted(k for k, v in canon.env.items() if env.get(k) != v)
            if missing:
                raise loadopts.Unstatable(f"its process does not carry the environment it was "
                                          f"loaded with: {', '.join(missing)}")
            inst.device = self._device(inst.pid)
            card = inst.device != CPU
            driver = runs.driver_string(inst.device, pci=self.cfg.pci.get(inst.device),
                                        sys_root=self.sys_root, proc_root=self.proc_root,
                                        runtime_versions=self.runtime_versions)
            weights = await asyncio.to_thread(self.hash_weights, Path(canon.weights_path))
            engine = {"name": f"localai/{inst.backend}", "build": self.version,
                      "variant": None, "image": self.image, "args": args, "env": canon.env,
                      "device": self.cfg.device_names[inst.device] if card else CPU,
                      "options": canon.options}
            run = runs.run_object(driver=driver, engine=engine, weights=weights)
            rid = runs.run_id(run)
            if not self.alive(inst):
                raise loadopts.Unstatable("its process went away while its run was computed")
        except asyncio.CancelledError:
            raise
        except (loadopts.Unstatable, runs.DriverUnknown, runs.WeightsError, OSError) as e:
            inst.reason = str(e)
            self.event(inst.model, UNSTATED, f"pid {inst.pid}: {e}")
            return
        self.store.put(rid, run)
        inst.engine, inst.run = engine, rid
        before = self.stated.get(inst.model)
        if before is None or before[0] == rid:
            same = "" if before is None else "the same "
            self.event(inst.model, LOADED, f"pid {inst.pid} on {inst.device}, {same}run "
                                           f"{rid[:23]}…")
        else:
            self.event(inst.model, NEW_RUN, f"run {rid[:23]}…: "
                                             + "; ".join(changes(before[1], run)))
        self.stated[inst.model] = (rid, run)

    async def settle(self, model: str) -> None:
        """Wait until the model's process, if any, has its run stated or its
        reason — what an operator's load waits for before it answers."""
        inst = self.instances.get(model)
        if inst is not None and inst.stating is not None:
            await asyncio.shield(inst.stating)

    async def answered_by(self, model: str, before: Instance | None,
                          headers_at: float) -> str | None:
        """The run of an answer whose headers arrived at `headers_at`, or None.

        `before` is the model's process when the request was passed on. Seen
        again after the answer, alive, it gave the answer. With none before,
        the request loaded the model: the process now serving it gave the
        answer only if it had started by the time the headers arrived — one
        started later replaced the one that answered."""
        inst = self.current(model)
        if inst is None or (before is not None and inst is not before):
            try:
                await self.refresh()
            except LocalAIError as e:
                self.log(f"[rage] {model}: no run stated for an answer: {e}")
                return None
            inst = self.current(model)
        if inst is None:
            return None
        if before is not None:
            if inst is not before:
                return None
        elif inst.started is None or inst.started > headers_at:
            return None
        if inst.stating is not None:
            # shielded: a caller who hangs up must not cancel the hash every
            # other caller of this process is waiting for
            await asyncio.shield(inst.stating)
        return inst.run if inst.run is not None and self.alive(inst) else None

    # ---- what the panel shows ---------------------------------------------------------

    def row(self, model: str) -> dict[str, Any]:
        inst = self.instances.get(model)
        if inst is None:
            return {"id": model, "state": "unloaded"}
        return {"id": model, "state": "loaded", "pid": inst.pid, "backend": inst.backend,
                "device": inst.device, "run": inst.run, "reason": inst.reason,
                "size_vram_mib": inst.size_vram_mib}

    def processes(self) -> list[dict[str, Any]]:
        return [{"pid": i.pid, "model": m, "device": i.device}
                for m, i in sorted(self.instances.items())]

    def card_memory(self, card: str) -> dict[str, Any]:
        """The card's real memory as its driver counts it, split by model
        from LocalAI's `size_vram`; what is left over is a stranger's."""
        by_model = {m: i.size_vram_mib or 0 for m, i in sorted(self.instances.items())
                    if i.device == card}
        own = sum(by_model.values())
        out: dict[str, Any] = {"by_model_mib": by_model, "own_mib": own, "read": False,
                               "why_not": None, "total_mib": None, "used_mib": None,
                               "foreign_mib": None, "free_mib": None}
        try:
            total, used, why = self.card(self.cfg.pci[card])
        except Exception as e:                          # noqa: BLE001 — a reading, never a crash
            total, used, why = None, None, f"{type(e).__name__}: {e}"
        out["why_not"] = why
        if total is not None:
            out["total_mib"] = total // MIB
        if total is not None and used is not None:
            out |= {"read": True, "used_mib": used // MIB,
                    "foreign_mib": max(0, used // MIB - own),
                    "free_mib": max(0, (total - used) // MIB)}
        return out


def changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What differs between two run objects, the options one by one."""
    out = []
    for key in ("weights", "driver"):
        if old.get(key) != new.get(key):
            out.append(f"{key} {old.get(key)} → {new.get(key)}")
    oe, ne = old.get("engine", {}), new.get("engine", {})
    for key in sorted((set(oe) | set(ne)) - {"options"}):
        if oe.get(key) != ne.get(key):
            out.append(f"{key} {json.dumps(oe.get(key))} → {json.dumps(ne.get(key))}")
    return out + loadopts.changed(oe.get("options"), ne.get("options"))
