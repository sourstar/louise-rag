"""Which model is where, and which run it is — the front's one state machine.

docs/notes/rage.md §4, §7, §8. Everything that decides is here, and none of
it is HTTP: front.py turns requests into these calls, the tests call them
with a fake process runner.

ONE LOAD AT A TIME PER MODEL. `ensure` holds the model's lock for the whole
load, so ten first requests for a dormant model admit once, launch once,
and all answer from the same placement (single-flight).

A PLACE IS HELD FROM THE MOMENT IT IS ADMITTED. Between `fits()` saying yes
and the placement being recorded there is no `await`, so two models admitted
in the same instant each see the other; a model still loading counts in
full, because it will hold what it declares.

A RUN IS STATED ONLY FOR THE PROCESS THAT WAS CHECKED. After a load the
process is matched against the entry (runs.match) and its argument vector
remembered. Before an answer is given a run, the same pid must still show
the same argument vector (`verified`); llama-swap restarts a child that
died on its next request, and that is another launch — the weights are
hashed again and the process matched again before any run is named.
`still_the_run` asks once more after the answer's headers arrive: a child
replaced mid-request gets no `Louise-Run`, and Louise writes nothing from it.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from . import admission, model, runs, swapyaml
from .model import CPU, ModelEntry, RageConfig
from .procfs import ProcFS
from .store import RunStore
from .swap import SwapError

READY, UNSTATED, LOADING = "ready", "unstated", "loading"
ABSENT, UNLOADED = "absent", "unloaded"
#: a child's oom_score_adj: the killer takes the model that grew, never the
#: front, which stays at 0 (docs/notes/rage.md §9)
OOM_CHILD = 800


class Runner(Protocol):
    async def load(self, variant: str, health: str) -> None: ...
    async def unload(self, variant: str) -> None: ...


class Refused(Exception):
    """The front will not do it. `status` and `body` are the HTTP answer;
    503 is a deferral to Louise's worker, never an error on a document."""

    def __init__(self, status: int, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.status = status
        self.body = {"error": reason, **extra}


@dataclass
class Placed:
    model: str
    device: str
    state: str
    since: float
    last_used: float
    run: str | None = None
    pid: int | None = None
    argv: tuple[str, ...] = ()
    reason: str | None = None
    observed: dict[str, int | None] = field(default_factory=dict)


def _log(message: str) -> None:
    print(f"[rage] {message}", flush=True)


class Placement:
    def __init__(self, *, config_path: Path, yaml_path: Path, image: str, runner: Runner,
                 store: RunStore, procfs: ProcFS | None = None,
                 capacity: admission.CapacityReader = admission.system_capacity,
                 sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc"),
                 runtime_versions: Path = Path("/etc/rage/runtime-versions"),
                 log: Callable[[str], None] = _log) -> None:
        self.config_path, self.yaml_path = Path(config_path), Path(yaml_path)
        self.image, self.runner, self.store = image, runner, store
        self.procfs = procfs or ProcFS()
        self.capacity, self.log = capacity, log
        self._sys_root, self._proc_root = sys_root, proc_root
        self._runtime_versions = runtime_versions
        self.placed: dict[str, Placed] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._drivers: dict[str, str] = {}
        self._yaml_text: str | None = None
        self.cfg: RageConfig = model.load(self.config_path)
        self._write_yaml()

    # ---- the configuration ---------------------------------------------------

    def signature(self) -> tuple:
        """What `rage.toml` and `rage.d/` are now — a change re-applies them."""
        files = [self.config_path, *sorted(model.dropin_dir(self.config_path).glob("*.toml"))]
        out = []
        for f in files:
            try:
                st = f.stat()
                out.append((str(f), st.st_mtime_ns, st.st_size))
            except OSError:
                out.append((str(f), None, None))
        return tuple(out)

    def _write_yaml(self) -> None:
        text = swapyaml.render(self.cfg)
        if text == self._yaml_text:
            return
        self.yaml_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.yaml_path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, self.yaml_path)         # llama-swap watches the path: never half a file
        self._yaml_text = text

    def reload(self) -> bool:
        """Apply the files. An edit that does not parse changes nothing: the
        models that are up stay up, and the log names the rule broken."""
        try:
            new = model.load(self.config_path)
        except model.RageConfigError as e:
            self.log(f"rage.toml not applied, the previous configuration stands: {e}")
            return False
        self.cfg = new
        self._drivers.clear()        # a card's declared address may have changed
        self._write_yaml()
        return True

    async def apply(self) -> None:
        """What the files say, made true: a model no longer configured is
        unloaded, and the preload list is loaded — through admission, like
        any load. Runtime loads and unloads last until this runs again
        (CLAUDE.md invariant 10)."""
        for model_id in [m for m in self.placed if m not in self.cfg.models]:
            try:
                await self.unload(model_id)
            except Refused as e:
                self.log(f"{model_id}: removed from rage.toml, not unloaded: {e}")
        for model_id in self.cfg.preload:
            try:
                await self.ensure(model_id)
            except Refused as e:
                self.log(f"{model_id}: not preloaded: {e}")

    # ---- loading ---------------------------------------------------------------

    def _lock(self, model_id: str) -> asyncio.Lock:
        return self._locks.setdefault(model_id, asyncio.Lock())

    def _holding(self) -> dict[str, str]:
        return {m: p.device for m, p in self.placed.items()}

    async def ensure(self, model_id: str, device: str | None = None) -> Placed:
        """The model, loaded — on `device`, or wherever it is or defaults to."""
        entry = self.cfg.models.get(model_id)
        if entry is None:
            raise Refused(404, f"{model_id!r} is not a model of this container")
        if device is not None and device not in entry.cmd:
            raise Refused(400, f"{model_id} declares no command for device {device!r}; "
                               f"it has {list(entry.devices)}")
        async with self._lock(model_id):
            placed = self.placed.get(model_id)
            if placed is not None:
                if device not in (None, placed.device):
                    raise Refused(409, f"{model_id} is loaded on {placed.device}; unload it "
                                       f"before loading it on {device}", device=placed.device)
                return await self._verified(entry, placed)
            return await self._admit(entry, device or entry.device)

    async def _admit(self, entry: ModelEntry, device: str) -> Placed:
        if not Path(entry.weights).exists():
            raise Refused(503, f"the weights of {entry.id} are absent: {entry.weights}. RAGE "
                               f"fetches nothing; place them and ask again", state=ABSENT)
        verdict = admission.fits(self.cfg, self._holding(), entry.id, device, self.capacity)
        if not verdict.ok:
            raise Refused(503, verdict.reason or "does not fit", admission=verdict.to_json())
        now = time.time()
        placed = Placed(model=entry.id, device=device, state=LOADING, since=now, last_used=now)
        self.placed[entry.id] = placed          # no await since fits(): the place is held
        try:
            await self._start(entry, placed)
        except BaseException:                   # a refusal, a bug, a cancelled request:
            self.placed.pop(entry.id, None)     # a load that did not finish holds no place
            raise
        return placed

    def _digest(self, entry: ModelEntry) -> tuple[str | None, str | None]:
        try:
            return runs.weights_digest(Path(entry.weights)), None
        except (runs.WeightsError, OSError) as e:
            return None, f"the weights cannot be hashed: {e}"

    async def _start(self, entry: ModelEntry, placed: Placed) -> None:
        """One launch: the weights hashed while the child comes up, then the
        process checked, then — only then — a run."""
        placed.state, placed.run, placed.pid, placed.argv = LOADING, None, None, ()
        hashing = asyncio.create_task(asyncio.to_thread(self._digest, entry))
        t0 = time.monotonic()
        try:
            await self.runner.load(entry.variant_id(placed.device), entry.health)
        except SwapError as e:
            await asyncio.gather(hashing, return_exceptions=True)
            self.log(f"{entry.id}@{placed.device}: not loaded: {e}")
            raise Refused(e.status, str(e)) from None
        weights, why = await hashing
        self._state(entry, placed, weights, why)
        self.log(f"{entry.id}@{placed.device}: {placed.state} in {time.monotonic() - t0:.1f} s"
                 + (f", run {placed.run}" if placed.run else f" — no run stated: {placed.reason}"))

    def _state(self, entry: ModelEntry, placed: Placed, weights: str | None,
               why: str | None) -> None:
        found = runs.match(self.procfs, entry, placed.device)
        placed.pid = found.pid
        placed.argv = tuple(self.procfs.cmdline(found.pid)) if found.pid is not None else ()
        if found.pid is not None:
            placed.observed = {"rss_mib": self.procfs.rss_mib(found.pid),
                               "vram_mib": self.procfs.vram_mib(found.pid)}
            self.procfs.set_oom_score_adj(found.pid, OOM_CHILD)
        if weights is None or not found.ok:
            placed.state, placed.run = UNSTATED, None
            placed.reason = why if weights is None else found.reason
            return
        engine = runs.engine_object(entry, placed.device, image=self.image,
                                    device_name=self.device_name(placed.device))
        try:
            driver = self._driver(placed.device)
        except runs.DriverUnknown as e:
            # the driver is part of what a run IS: one that cannot be read is
            # not guessed, and the model serves with no run stated
            placed.state, placed.run, placed.reason = UNSTATED, None, str(e)
            return
        run = runs.run_object(driver=driver, engine=engine, weights=weights)
        placed.run = runs.run_id(run)
        self.store.put(placed.run, run)
        placed.state, placed.reason = READY, None
        self.log(f"{entry.id}@{placed.device}: pid {found.pid}, program "
                 f"{self.procfs.exe_digest(found.pid)}, engine {runs.engine_id(engine)}")

    def still_the_run(self, placed: Placed) -> bool:
        """Is the process that was checked still the one answering."""
        return (placed.state == READY and placed.pid is not None
                and tuple(self.procfs.cmdline(placed.pid)) == placed.argv)

    async def _verified(self, entry: ModelEntry, placed: Placed) -> Placed:
        if placed.pid is None or tuple(self.procfs.cmdline(placed.pid)) == placed.argv:
            return placed
        self.log(f"{entry.id}@{placed.device}: pid {placed.pid} is gone; its next start is "
                 f"another launch, hashed and checked again")
        try:
            await self._start(entry, placed)
        except BaseException:
            self.placed.pop(entry.id, None)
            raise
        return placed

    # ---- unloading -------------------------------------------------------------

    async def unload(self, model_id: str) -> None:
        async with self._lock(model_id):
            placed = self.placed.get(model_id)
            if placed is None:
                raise Refused(404, f"{model_id} is not loaded")
            try:
                await self.runner.unload(f"{model_id}@{placed.device}")
            except SwapError as e:
                raise Refused(e.status, str(e)) from None
            self.placed.pop(model_id, None)
            self.log(f"{model_id}@{placed.device}: unloaded")

    async def restart(self, model_id: str) -> Placed:
        """One child stopped and started, and nothing else (the `xe` fan that
        stays high until a GPU process restarts; operator, 2026-09-20: never
        restart other apps). Never called by the front itself."""
        placed = self.placed.get(model_id)
        if placed is None:
            raise Refused(404, f"{model_id} is not loaded")
        device = placed.device
        await self.unload(model_id)
        return await self.ensure(model_id, device)

    # ---- describing ------------------------------------------------------------

    def device_name(self, device: str) -> str:
        """09 §9.3's `device`, "as its driver names it": the operator's words
        in `[rage] device_names`, which model.load requires for every card."""
        return CPU if device == CPU else self.cfg.device_names[device]

    def _driver(self, device: str) -> str:
        if device not in self._drivers:
            self._drivers[device] = runs.driver_string(
                device, pci=self.cfg.pci.get(device), sys_root=self._sys_root,
                proc_root=self._proc_root, runtime_versions=self._runtime_versions)
        return self._drivers[device]

    def describe(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for model_id in sorted(self.cfg.models):
            entry = self.cfg.models[model_id]
            placed = self.placed.get(model_id)
            row: dict[str, Any] = {
                "id": model_id, "devices": list(entry.devices), "default_device": entry.device,
                "engine": {"name": entry.engine.name, "build": entry.engine.build,
                           "variant": entry.engine.variant},
                "memory": {"declared": dict(entry.memory), "host": dict(entry.host),
                           "observed": None},
                "source": entry.source}
            if placed is None:
                row |= {"state": UNLOADED if Path(entry.weights).exists() else ABSENT,
                        "device": None, "run": None, "pid": None, "reason": None}
            else:
                row |= {"state": placed.state, "device": placed.device, "run": placed.run,
                        "pid": placed.pid, "reason": placed.reason,
                        "since": int(placed.since), "idle_s": int(now - placed.last_used)}
                row["memory"]["observed"] = placed.observed or None
            out.append(row)
        return out
