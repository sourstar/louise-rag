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
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .. import gpu_memory
from . import admission, model, runs, swapyaml
from .model import CPU, ModelEntry, RageConfig
from .procfs import ProcFS
from .store import RunStore
from .swap import SwapError

READY, UNSTATED, LOADING = "ready", "unstated", "loading"
ABSENT, UNLOADED = "absent", "unloaded"
EXITED = "exited"          # only ever REPORTED: the process that was checked is gone
#: a child's oom_score_adj: the killer takes the model that grew, never the
#: front, which stays at 0 (docs/notes/rage.md §9)
OOM_CHILD = 800
MIB = 1 << 20
#: a stranger holding less of a card than this is a desktop or a probe: nothing
#: to act on
FOREIGN_FLOOR_MIB = 256
#: a child with at least this much of its GPU buffers in host RAM, and more
#: there than on the card, has been pushed off the card
PUSHED_OUT_MIB = 256
#: refusals, unloads under pressure and restores, newest last; Louise reads
#: them at `/louise/gpu` — they are her notification
EVENTS_KEPT = 50
REFUSED, RELIEVED, RESTORED = "refused", "unloaded-under-pressure", "restored"

#: a card's PCI address -> (total bytes, used bytes, why a number is missing)
CardReader = Callable[[str], "tuple[int | None, int | None, str | None]"]


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
    in_flight: int = 0          # requests being answered now: never idle while > 0


def least_given_up(holding: "list[tuple[Placed, int]]", deficit: int) -> "list[Placed]":
    """The models that, together, cover `deficit` MiB with the LEAST memory given
    up; among equals the fewer models, then the longer unused. All of them when
    even all of them fall short — it still helps. Every subset is tried: a node
    holds a dozen models (2^12 sums of small numbers); past that, the plain
    least-recently-used order."""
    if deficit <= 0 or not holding:
        return []
    if sum(mib for _, mib in holding) <= deficit:
        return [p for p, _ in holding]
    if len(holding) > 12:
        out, left = [], deficit
        for placed, mib in sorted(holding, key=lambda h: h[0].last_used):
            if left <= 0:
                break
            out.append(placed)
            left -= mib
        return out
    best: tuple | None = None
    for mask in range(1, 1 << len(holding)):
        chosen = [h for i, h in enumerate(holding) if mask >> i & 1]
        given = sum(mib for _, mib in chosen)
        if given < deficit:
            continue
        key = (given, len(chosen), sum(p.last_used for p, _ in chosen))
        if best is None or key < best[0]:
            best = (key, chosen)
    return [p for p, _ in best[1]] if best else []


def _log(message: str) -> None:
    print(f"[rage] {message}", flush=True)


class Placement:
    def __init__(self, *, config_path: Path, yaml_path: Path, image: str, runner: Runner,
                 store: RunStore, procfs: ProcFS | None = None,
                 capacity: admission.CapacityReader = admission.system_capacity,
                 sys_root: Path = Path("/sys"), proc_root: Path = Path("/proc"),
                 runtime_versions: Path = Path("/etc/rage/runtime-versions"),
                 card: CardReader | None = None,
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
        self._card: CardReader = card or (lambda pci: gpu_memory.read(pci, sys_root=sys_root))
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        #: model -> what was given up under pressure, and why
        self._dropped: dict[str, dict[str, Any]] = {}
        #: card -> since when nothing has squeezed it (None: squeezed now)
        self._quiet_since: dict[str, float | None] = {}
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
        foreign = {} if device == CPU else {
            device: self.card_memory(device).get("foreign_mib") or 0}
        verdict = admission.fits(self.cfg, self._holding(), entry.id, device, self.capacity,
                                 foreign)
        if not verdict.ok:
            # THE NOTIFICATION (operator, 2026-09-20): Louise needed a model and it
            # does not fit. She gets the reason on the answer, and RAGE keeps it
            # where her panel reads it — a refusal is never silent.
            self._event(REFUSED, entry.id, device, verdict.reason or "does not fit")
            raise Refused(503, verdict.reason or "does not fit", admission=verdict.to_json())
        self._dropped.pop(entry.id, None)       # asked for again: it comes back on demand
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
            placed.observed = self._observed(found.pid)
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

    def _observed(self, pid: int) -> dict[str, int | None]:
        """What the process holds, read now. `ram_mib` is what it OWNS and
        `mapped_mib` the files it has merely mapped — the model file right after
        a load, page cache the kernel takes back at will — kept apart because
        their sum (`rss_mib`) made a freshly loaded model look as large as its
        file (operator, 2026-09-20)."""
        ram, mapped = self.procfs.ram_and_mapped_mib(pid)
        return {"rss_mib": self.procfs.rss_mib(pid), "ram_mib": ram, "mapped_mib": mapped,
                "vram_mib": self.procfs.vram_mib(pid)}

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

    async def unload(self, model_id: str, *, only_if_idle: bool = False) -> bool:
        """Unloaded, or — with `only_if_idle` — left alone because a request
        reached it while we waited for its lock: a model is never taken from
        under an answer it is giving."""
        async with self._lock(model_id):
            placed = self.placed.get(model_id)
            if placed is None:
                raise Refused(404, f"{model_id} is not loaded")
            if only_if_idle and placed.in_flight:
                return False
            try:
                await self.runner.unload(f"{model_id}@{placed.device}")
            except SwapError as e:
                raise Refused(e.status, str(e)) from None
            self.placed.pop(model_id, None)
            self.log(f"{model_id}@{placed.device}: unloaded")
            return True

    # ---- the card's real memory, and a stranger on it ---------------------------------
    #
    # Operator, 2026-09-20, the day a container outside RAGE took 21.4 GiB of
    # the card and three models died: read the card's real memory before a
    # load; show what is RAGE's and what is not; when a process outside RAGE
    # squeezes the card, "wisely unload the unused models to avoid an oom
    # crash of the other model"; and when Louise needs a model that does not
    # fit, tell her.

    def _event(self, kind: str, model_id: str, device: str, detail: str) -> None:
        """Kept for Louise's panel. A refusal repeated every round by a worker
        that keeps asking is ONE event with a count, not fifty."""
        now = int(time.time())
        last = self.events[-1] if self.events else None
        if last and (last["kind"], last["model"], last["detail"]) == (kind, model_id, detail) \
                and now - last["time"] < 600:
            last["time"], last["count"] = now, last["count"] + 1
            return
        self.events.append({"time": now, "kind": kind, "model": model_id, "device": device,
                            "detail": detail, "count": 1})

    def card_memory(self, device: str) -> dict[str, Any]:
        """What the card holds NOW and whose it is. `own` is everything in this
        container's own PID namespace that holds GPU memory — a model still
        loading included, which has no pid on its placement yet and would
        otherwise be counted as a stranger AND as its own declaration. `foreign`
        is the driver's count of what is used, less `own`. Where the driver
        cannot be asked, `read` is false, `why_not` says why, and nothing here
        decides anything."""
        own_card = own_host = 0
        for pid in self.procfs.pids():
            on_card, in_host = self.procfs.gpu_mib(pid)
            own_card += on_card or 0
            own_host += in_host or 0
        pushed = []
        for model_id, placed in self.placed.items():
            if placed.device != device or placed.pid is None:
                continue
            on_card, in_host = self.procfs.gpu_mib(placed.pid)
            if in_host is not None and in_host >= PUSHED_OUT_MIB and in_host > (on_card or 0):
                pushed.append(model_id)
        declared = sum(self.cfg.models[m].memory[p.device] for m, p in self.placed.items()
                       if p.device == device and m in self.cfg.models)
        out: dict[str, Any] = {
            "declared_capacity_mib": self.cfg.capacity_mib.get(device),
            "reserve_mib": self.cfg.reserve_mib.get(device, 0),
            "declared_held_mib": declared, "own_mib": own_card,
            "own_in_host_ram_mib": own_host, "pushed_out": sorted(pushed),
            "read": False, "why_not": None, "total_mib": None, "used_mib": None,
            "foreign_mib": None, "free_mib": None,
            "squeezed": False, "unloaded_under_pressure": sorted(
                m for m, d in self._dropped.items() if d["device"] == device)}
        pci = self.cfg.pci.get(device)
        if pci is None:
            out["why_not"] = (f"declare [rage] pci.{device}: the card's memory is read where "
                              f"the card is on the bus")
            return out
        try:
            total, used, why = self._card(pci)
        except Exception as e:                          # noqa: BLE001 — a reading, never a crash
            total, used, why = None, None, f"{type(e).__name__}: {e}"
        out["why_not"] = why
        if total is not None:
            out["total_mib"] = total // MIB
        if total is not None and used is not None:
            out |= {"read": True, "used_mib": used // MIB,
                    "foreign_mib": max(0, used // MIB - own_card),
                    "free_mib": max(0, (total - used) // MIB)}
            out["squeezed"] = (out["foreign_mib"] >= FOREIGN_FLOOR_MIB
                               and out["free_mib"] < out["reserve_mib"])
        return out

    def _idle(self, device: str, now: float) -> list[Placed]:
        """Nobody is using them: no request in flight, none for `idle_s`.
        Least recently used first."""
        idle_s = self.cfg.pressure["idle_s"]
        idle = [p for p in self.placed.values()
                if p.device == device and p.state in (READY, UNSTATED) and not p.in_flight
                and now - p.last_used >= idle_s]
        return sorted(idle, key=lambda p: p.last_used)

    async def relieve(self, now: float | None = None) -> list[str]:
        """One look at every card, and what follows from it. Returns the models
        given up.

        GIVEN UP, in this order, and only IDLE models (`_idle`):
          1. a model PUSHED OFF THE CARD — it holds host RAM, answers slowly, and
             its reload puts it back on the card once there is room;
          2. while a stranger SQUEEZES the card (real free memory below the
             declared reserve, with a stranger holding part of it): the idle
             models that TOGETHER cover the deficit with the LEAST given up
             (`least_given_up`). Least-recently-used-first was the first rule,
             and the first live squeeze (the node, 2026-09-20: 1,435 MiB short)
             gave up 9,937 MiB for it — two small models and then the 8.6 GiB
             one that happened to be next in line.
        A model somebody is using is never touched: if the stranger squeezes
        past what the idle ones free, the driver pushes it to host RAM and it
        goes on answering.

        RESTORED: a PRELOADED model given up comes back by itself once the card
        has been quiet for `restore_s` and REALLY has room for it — read, not
        assumed — one per look. Any other comes back on its next request,
        through admission like any load."""
        now = time.time() if now is None else now
        given_up: list[str] = []
        cards = sorted({p.device for p in self.placed.values()}
                       | {d["device"] for d in self._dropped.values()} - {CPU})
        for device in cards:
            if device == CPU:
                continue
            mem = self.card_memory(device)
            squeezed = bool(mem["squeezed"])
            if squeezed or mem["pushed_out"]:
                self._quiet_since[device] = None
            elif self._quiet_since.get(device) is None:
                self._quiet_since[device] = now
            if not self.cfg.pressure["unload"]:
                continue
            idle = self._idle(device, now)
            victims: list[tuple[Placed, str]] = [
                (p, "its buffers had been pushed off the card into host RAM")
                for p in idle if p.model in mem["pushed_out"]]
            if squeezed:
                deficit = mem["reserve_mib"] - mem["free_mib"] - sum(
                    (self.procfs.gpu_mib(p.pid)[0] or 0) for p, _ in victims if p.pid)
                why = (f"processes outside RAGE hold {mem['foreign_mib']} MiB of the card and "
                       f"{mem['free_mib']} MiB are free, under the {mem['reserve_mib']} reserved")
                holding = [(p, self.procfs.gpu_mib(p.pid)[0] or 0) for p in idle
                           if p.pid is not None and not any(p is v for v, _ in victims)]
                victims += [(p, why) for p in least_given_up(
                    [(p, mib) for p, mib in holding if mib], deficit)]
            for placed, why in victims:
                idle_for = int(now - placed.last_used)
                try:
                    if not await self.unload(placed.model, only_if_idle=True):
                        continue
                except Refused as e:
                    self.log(f"{placed.model}: not unloaded under pressure: {e}")
                    continue
                self._dropped[placed.model] = {"device": device, "time": int(now), "why": why}
                self._event(RELIEVED, placed.model, device,
                            f"idle for {idle_for} s and unloaded: {why}")
                self.log(f"{placed.model}@{device}: UNLOADED UNDER PRESSURE, idle for "
                         f"{idle_for} s — {why}")
                given_up.append(placed.model)
        await self._restore(now)
        return given_up

    async def _restore(self, now: float) -> None:
        restore_s = self.cfg.pressure["restore_s"]
        for model_id, dropped in list(self._dropped.items()):
            if model_id in self.placed or model_id not in self.cfg.models:
                self._dropped.pop(model_id, None)
                continue
            if model_id not in self.cfg.preload:
                continue                                # back on its next request
            device = dropped["device"]
            quiet = self._quiet_since.get(device)
            if quiet is None or now - quiet < restore_s:
                continue
            mem = self.card_memory(device)
            need = self.cfg.models[model_id].memory.get(device, 0)
            if not mem["read"] or mem["free_mib"] < need + mem["reserve_mib"]:
                continue
            try:
                await self.ensure(model_id, device)
            except Refused:
                continue
            self._event(RESTORED, model_id, device,
                        f"the card has been quiet for {int(now - quiet)} s and has room again")
            self.log(f"{model_id}@{device}: restored after the pressure")
            return                                      # one per look: each load is measured

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
                dropped = self._dropped.get(model_id)
                row |= {"state": UNLOADED if Path(entry.weights).exists() else ABSENT,
                        "device": None, "run": None, "pid": None,
                        "reason": None if dropped is None else (
                            f"unloaded under memory pressure: {dropped['why']}. It comes "
                            f"back on its next request, or by itself if it is preloaded "
                            f"and the card has room again")}
            else:
                row |= {"state": placed.state, "device": placed.device, "run": placed.run,
                        "pid": placed.pid, "reason": placed.reason,
                        "since": int(placed.since), "idle_s": int(now - placed.last_used)}
                row["memory"]["observed"] = placed.observed or None
                gone = (placed.pid is not None and placed.state != LOADING
                        and tuple(self.procfs.cmdline(placed.pid)) != placed.argv)
                if gone:
                    # SAID AS IT IS. The node, 2026-09-20: the kernel's out-of-memory
                    # killer took three models and this list read "ready" for each,
                    # with the pid of a process that no longer existed. Nothing is
                    # changed here — the next request finds it gone and starts
                    # another launch, hashed and checked again (`_verified`) — but
                    # a list that says ready must be true when it is read.
                    row |= {"state": EXITED, "run": None,
                            "reason": f"pid {placed.pid} is gone — killed or crashed; the "
                                      f"next request starts it again, as another launch"}
                    row["memory"]["observed"] = None
            out.append(row)
        return out
