"""What giving a model up costs: how long it takes to come back to the card,
from the NVMe and from RAM, on a quiet node and beside an embedding in progress.

    python -m tools.load_profile --rage http://nas:30437 \
        --models-dir /state/localai-models --token-file /rage/secrets/admin_token \
        --models chat,madlad,bge-m3 --busy qwen3-embedding-4b \
        --reps 3 --out /data/load-profile.jsonl

`louise dev load-profile` (docs/07 §7.3); docs/notes/rage.md §14. It reads the
weights at the paths LocalAI's model YAML names (`parameters.model`, under its
models directory): run it in a container with RAGE's volumes
(deploy/truenas-rage-localai.yaml), the node paused.

WHY (operator, 2026-10-01). RAGE frees the card on demand (rage.md §7) and a
lookahead would load the next model before it is asked for (rage.md §20,
the KVFlow template). Both are worth it only when a reload is cheaper than
what it saves. The weights sit on an NVMe 5.0 drive; a model given up a
minute ago may still be in RAM (the page cache, or ZFS's ARC), and one given
up an hour ago may not. Between two queries a reload may be affordable;
while an embedding batch is using the card it may cost the batch more than
it saves. This measures both, per model, instead of guessing.

ONE TRIAL. The model is unloaded through RAGE's admin API; its file is made
cold (`--cold-by`) or warmed by reading it; with `--busy`, an embedding
loop runs on another model and is measured quietly for `--window` seconds;
then the load is timed from the request to RAGE's answer (admission, the
child's start, its health, the weights hashed — rage.md §8 hashes them at
EVERY launch), and the unload after it. The disk's read counter
(/proc/diskstats) and ZFS's ARC counters, when present, are read on each
side of the load.

COLD IS OBSERVED, NOT ASSUMED. Each trial is classified by what the disk
actually read during the load against the size of the weights: `cold` from
half of it, `warm` below a twentieth, `partial` between. `posix_fadvise`
evicts the page cache of ext4, xfs and btrfs, and NOT ZFS's ARC; on ZFS use
`--cold-by drop-caches`, or set `primarycache=metadata` on the models'
dataset for a cold series (and back to `all` after) with `--cold-by none`.
Whatever the method, the classification is what the disk saw.

WHAT IT CHANGES. It loads and unloads models; run it with the node paused
(`louise node pause`). A model that answered a request in the last minute
refuses the run (exit 2). The models that were loaded when it started are
loaded again when it ends, interrupted or not. `--cold-by drop-caches`
empties the whole host's page cache, Louise's manifests included.

THE KERNEL CACHE. A launch also compiles the GPU's kernels (rage.md §20);
Intel's driver keeps them on disk by default (NEO_CACHE_DIR, under the
container's $HOME), so that cost is paid once per container unless the
cache is lost. Two runs with `--label`, one after emptying the cache in
RAGE's container and one after, keep the two series apart in the summary.
Each trial also records the run RAGE stated: a declared env change is a new
run, and the summary counts them.

THE RAW PHASE (once per model, unless `--skip-raw`): a plain read of the
weights cold and warm, and their SHA-256 as RAGE computes it — the I/O and
the hashing parts of a load, apart from the runtime's own start.

Records go to `--out` as JSON lines, one per trial, written as each ends.
No GPU import (invariant 7); the token is read from a file (invariant 8)
and never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import httpx

from app.rage import runs

MIB = 1 << 20
SECTOR = 512                     # /proc/diskstats counts 512-byte sectors, whatever the drive
READ_BLOCK = 8 * MIB

COLD_SHARE = 0.5                 # the disk read at least half the weights
WARM_SHARE = 0.05                # the disk read less than a twentieth of them
IN_USE_S = 60                    # a model asked this recently is someone's

#: RAGE's state for a model LocalAI holds: `loaded` (app/rage/front_localai.py),
#: whether or not a run is stated for it.
HOLDING = frozenset({"loaded"})
EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_DEFERRED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130

_WHOLE_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
_ARC_KEYS = ("demand_data_hits", "demand_data_misses", "prefetch_data_hits",
             "prefetch_data_misses", "size")


class ModelsError(ValueError):
    """LocalAI's models directory cannot say which weights a model reads."""


@dataclass(frozen=True)
class Weights:
    weights: str


@dataclass(frozen=True)
class ModelsDir:
    models: dict[str, Weights]


def models_dir(path: Path) -> ModelsDir:
    """Each model's weights, from LocalAI's YAML: `parameters.model` under the
    models directory. The YAMLs are JSON, which is YAML, after their comment
    lines (deploy/rage/localai-models/, and what RAGE's PUT writes); one that
    is not is refused rather than guessed at."""
    if not path.is_dir():
        raise ModelsError(f"{path} is not LocalAI's models directory")
    out: dict[str, Weights] = {}
    for f in sorted(path.glob("*.yaml")):
        body = "\n".join(l for l in f.read_text(encoding="utf-8").splitlines()
                         if not l.lstrip().startswith("#"))
        try:
            doc = json.loads(body)
            out[str(doc["name"])] = Weights(str(path / doc["parameters"]["model"]))
        except (ValueError, KeyError, TypeError) as e:
            raise ModelsError(f"{f.name}: no name and parameters.model to read "
                              f"({type(e).__name__}: {e})") from None
    if not out:
        raise ModelsError(f"{path} holds no model YAML")
    return ModelsDir(out)


class Refusal(Exception):
    """A rule stood in the way before anything was done (exit 2)."""


# ---- what the host reads -----------------------------------------------------

def disk_read_bytes(diskstats: str, devices: Sequence[str] = ()) -> dict[str, int]:
    """Bytes read so far, per device. Whole disks only unless `devices` names
    them: a partition's reads are its disk's too, and counting both would
    count them twice."""
    out: dict[str, int] = {}
    for line in diskstats.splitlines():
        f = line.split()
        if len(f) < 6:
            continue
        name = f[2]
        if devices and name not in devices:
            continue
        if not devices and not _WHOLE_DISK.match(name):
            continue
        out[name] = int(f[5]) * SECTOR
    return out


def arc_counters(arcstats: str) -> dict[str, int]:
    """ZFS's ARC counters (/proc/spl/kstat/zfs/arcstats), the ones a load moves."""
    out: dict[str, int] = {}
    for line in arcstats.splitlines():
        f = line.split()
        if len(f) == 3 and f[0] in _ARC_KEYS and f[2].lstrip("-").isdigit():
            out[f[0]] = int(f[2])
    return out


def fstype_of(path: Path, mountinfo: str) -> str | None:
    """The filesystem `path` is on: the longest mount point that contains it."""
    target = str(path)
    best, kind = "", None
    for line in mountinfo.splitlines():
        f = line.split()
        if "-" not in f:
            continue
        point = f[4].replace("\\040", " ")
        sep = f.index("-")
        if len(f) <= sep + 1:
            continue
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) > len(best):
            best, kind = point, f[sep + 1]
    return kind


def meminfo_mib(meminfo: str) -> dict[str, int]:
    keys = {"MemTotal", "MemAvailable", "Cached"}
    out: dict[str, int] = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        if name in keys and rest.split():
            out[name] = int(rest.split()[0]) // 1024
    return out


def weight_files(weights: Path) -> list[Path]:
    """The files a load reads: the file, or every regular file under the directory."""
    if weights.is_file():
        return [weights]
    return sorted(p for p in weights.rglob("*") if p.is_file() and not p.is_symlink())


def cache_seen(read_bytes: int, weights_bytes: int) -> str:
    if weights_bytes <= 0:
        return "unknown"
    share = read_bytes / weights_bytes
    if share >= COLD_SHARE:
        return "cold"
    if share < WARM_SHARE:
        return "warm"
    return "partial"


@dataclass(frozen=True)
class Snapshot:
    disk: Mapping[str, int]
    arc: Mapping[str, int]

    def read_since(self, before: "Snapshot") -> int:
        return sum(max(0, v - before.disk.get(k, v)) for k, v in self.disk.items())

    def arc_since(self, before: "Snapshot") -> dict[str, int]:
        return {k: v - before.arc[k] for k, v in self.arc.items() if k in before.arc}


class Host:
    """The host's counters and its caches, read through `proc` (a root other
    than /proc in tests)."""

    def __init__(self, proc: Path = Path("/proc"), devices: Sequence[str] = ()) -> None:
        self.proc = proc
        self.devices = tuple(devices)

    def _read(self, rel: str) -> str:
        try:
            return (self.proc / rel).read_text(encoding="utf-8")
        except OSError:
            return ""

    def snapshot(self) -> Snapshot:
        return Snapshot(disk=disk_read_bytes(self._read("diskstats"), self.devices),
                        arc=arc_counters(self._read("spl/kstat/zfs/arcstats")))

    def fstype(self, path: Path) -> str | None:
        return fstype_of(path.resolve(), self._read("self/mountinfo"))

    def memory(self) -> dict[str, int]:
        return meminfo_mib(self._read("meminfo"))

    def can_drop_caches(self) -> bool:
        return os.access(self.proc / "sys/vm/drop_caches", os.W_OK)

    def evict(self, files: Sequence[Path], how: str) -> None:
        if how == "fadvise":
            for p in files:
                fd = os.open(p, os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                finally:
                    os.close(fd)
        elif how == "drop-caches":
            os.sync()
            (self.proc / "sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        # "none": the operator made it cold (zfs primarycache=metadata)

    def read_all(self, files: Sequence[Path]) -> float:
        """Read every byte, as a load does; seconds taken."""
        buf = bytearray(READ_BLOCK)
        t0 = time.monotonic()
        for p in files:
            with p.open("rb", buffering=0) as f:
                while f.readinto(buf):
                    pass
        return time.monotonic() - t0


# ---- RAGE --------------------------------------------------------------------

class RageUnreachable(Exception):
    """Deferred: nothing is wrong and nothing was done (exit 3)."""


class Rage:
    """RAGE's admin API (rage.md §6). The token goes in a header and nowhere else."""

    def __init__(self, url: str, token: str, client: httpx.Client) -> None:
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self.client = client

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return self.client.request(method, f"{self.url}{path}", headers=self._headers,
                                       timeout=900, **kw)
        except httpx.TransportError as e:
            raise RageUnreachable(f"{self.url}{path}: {type(e).__name__}") from None

    def models(self) -> list[dict[str, Any]]:
        r = self._call("GET", "/louise/models")
        r.raise_for_status()
        return r.json()

    def gpu(self) -> dict[str, Any] | None:
        r = self._call("GET", "/louise/gpu")
        return r.json() if r.status_code == 200 else None

    @staticmethod
    def _authorised(r: httpx.Response) -> httpx.Response:
        if r.status_code in (401, 403):
            raise Refusal(f"RAGE refused the admin token ({r.status_code}); check the "
                          f"token file against RAGE's ADMIN_TOKEN_FILE")
        return r

    def load(self, model_id: str, device: str | None = None) -> tuple[int, str]:
        r = self._authorised(self._call("POST", f"/louise/models/{model_id}/load",
                                        json={"device": device} if device else None))
        return r.status_code, r.text

    def unload(self, model_id: str) -> int:
        """200 unloaded, 404 it was not loaded; both leave it unloaded."""
        r = self._authorised(self._call("POST", f"/louise/models/{model_id}/unload"))
        if r.status_code not in (200, 404):
            raise RuntimeError(f"unloading {model_id}: {r.status_code} {r.text[:200]}")
        return r.status_code


# ---- an embedding in progress ------------------------------------------------

class Background(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def completions(self) -> list[float]: ...


_SENTENCES = (
    "The committee voted to postpone the irrigation project until spring.",
    "Photosynthesis converts carbon dioxide and water into glucose using light.",
    "The ledger showed a deficit that no honest arithmetic could explain.",
    "A murmuration of starlings turned above the harbour like a single body.",
)


def chunk_text(nonce: str, i: int, chars: int) -> str:
    """A chunk-sized text no cache has seen."""
    parts = [f"load-profile-{nonce}-{i}."]
    j = i
    while sum(len(p) + 1 for p in parts) < chars:
        parts.append(_SENTENCES[j % len(_SENTENCES)])
        j += 1
    return " ".join(parts)


class EmbeddingLoop:
    """`concurrency` requests always in flight on `model`, as the worker's
    embed producer keeps them (`Louise-Level: corpus`); the end time of every
    answer is kept."""

    def __init__(self, rage_url: str, model: str, *, concurrency: int, chars: int,
                 client: httpx.Client, clock: Callable[[], float] = time.monotonic) -> None:
        self.url = f"{rage_url.rstrip('/')}/upstream/{model}/v1/embeddings"
        self.model, self.concurrency, self.chars = model, concurrency, chars
        self.client, self.clock = client, clock
        self.nonce = secrets.token_hex(4)
        self._done: list[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._counter = 0
        self.errors = 0

    def _next(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def _worker(self) -> None:
        while not self._stop.is_set():
            text = chunk_text(self.nonce, self._next(), self.chars)
            try:
                r = self.client.post(self.url, json={"model": self.model, "input": [text]},
                                     headers={"Louise-Level": "corpus"}, timeout=120)
                ok = r.status_code == 200
            except httpx.HTTPError:
                ok = False
            with self._lock:
                if ok:
                    self._done.append(self.clock())
                else:
                    self.errors += 1
            if not ok:
                self._stop.wait(0.5)

    def start(self) -> None:
        self._stop.clear()
        self._threads = [threading.Thread(target=self._worker, daemon=True,
                                          name=f"load-profile-busy-{i}")
                         for i in range(self.concurrency)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=150)

    def completions(self) -> list[float]:
        with self._lock:
            return list(self._done)


def disturbance(done: Iterable[float], quiet: tuple[float, float],
                load: tuple[float, float]) -> dict[str, float | int | None]:
    """What the load cost the embedding beside it: answers per second while
    it was alone and while the load ran, and the seconds of embedding lost."""
    done = list(done)
    q0, q1 = quiet
    l0, l1 = load
    n_quiet = sum(1 for t in done if q0 <= t < q1)
    n_load = sum(1 for t in done if l0 <= t < l1)
    rate_quiet = n_quiet / (q1 - q0) if q1 > q0 else None
    rate_load = n_load / (l1 - l0) if l1 > l0 else None
    lost_s = None
    if rate_quiet and l1 > l0:
        lost = max(0.0, rate_quiet * (l1 - l0) - n_load)
        lost_s = round(lost / rate_quiet, 2)
    return {"answers_quiet": n_quiet, "answers_during_load": n_load,
            "per_s_quiet": None if rate_quiet is None else round(rate_quiet, 3),
            "per_s_during_load": None if rate_load is None else round(rate_load, 3),
            "embedding_lost_s": lost_s}


# ---- the trials --------------------------------------------------------------

@dataclass
class Trial:
    model: str
    background: str                  # "idle" | "busy:<model>"
    cache_wanted: str                # "cold" | "warm"
    rep: int
    weights_mib: float
    kind: str = "trial"
    label: str = ""                  # the series, e.g. kernel cache empty / full
    run: str | None = None           # the run RAGE stated: an env change is a new run
    status: int = 0
    refused: str | None = None
    load_s: float | None = None
    unload_s: float | None = None
    read_mib: float = 0.0            # what the disk read during the load
    cache_seen: str = "unknown"
    arc: dict[str, int] = field(default_factory=dict)
    busy: dict[str, Any] | None = None


@dataclass(frozen=True)
class Plan:
    models: tuple[str, ...]
    caches: tuple[str, ...]
    backgrounds: tuple[str, ...]
    reps: int
    raw: bool

    def trials(self) -> list[tuple[str, str, int, str]]:
        out = []
        for bg in self.backgrounds:
            for m in self.models:
                if bg == f"busy:{m}":
                    continue                     # a model cannot be busy beside itself
                for rep in range(1, self.reps + 1):
                    for cache in self.caches:
                        out.append((bg, m, rep, cache))
        return out


class Profiler:
    def __init__(self, rage: Rage, host: Host, cfg: "ModelsDir", *,
                 cold_by: str, window_s: float, emit: Callable[[dict[str, Any]], None],
                 background: Background | None = None, busy_model: str | None = None,
                 label: str = "",
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 say: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> None:
        self.rage, self.host, self.cfg, self.label = rage, host, cfg, label
        self.cold_by, self.window_s, self.emit = cold_by, window_s, emit
        self.background, self.busy_model = background, busy_model
        self.clock, self.sleep, self.say = clock, sleep, say

    def files(self, model_id: str) -> list[Path]:
        return weight_files(Path(self.cfg.models[model_id].weights))

    def weights_bytes(self, model_id: str) -> int:
        return sum(p.stat().st_size for p in self.files(model_id))

    def raw(self, model_id: str) -> dict[str, Any]:
        """The weights read cold, read warm, and hashed as RAGE hashes them."""
        files = self.files(model_id)
        size = self.weights_bytes(model_id)
        self.host.evict(files, self.cold_by)
        before = self.host.snapshot()
        cold_s = self.host.read_all(files)
        cold_read = self.host.snapshot().read_since(before)
        warm_s = self.host.read_all(files)
        t0 = self.clock()
        runs.weights_digest(Path(self.cfg.models[model_id].weights))
        hash_s = self.clock() - t0
        rec = {"kind": "raw", "model": model_id, "weights_mib": round(size / MIB, 1),
               "first_read_s": round(cold_s, 2), "first_read_seen": cache_seen(cold_read, size),
               "first_read_mib": round(cold_read / MIB, 1),
               "warm_read_s": round(warm_s, 2), "sha256_s": round(hash_s, 2)}
        self.emit(rec)
        return rec

    def trial(self, model_id: str, background: str, rep: int, cache: str) -> Trial:
        files = self.files(model_id)
        size = self.weights_bytes(model_id)
        t = Trial(model=model_id, background=background, cache_wanted=cache, rep=rep,
                  weights_mib=round(size / MIB, 1), label=self.label)
        self.rage.unload(model_id)               # before evicting: a mapped page stays
        if cache == "cold":
            self.host.evict(files, self.cold_by)
        else:
            self.host.read_all(files)
        beside = background != "idle" and self.background is not None
        quiet = None
        if beside:
            q0 = self.clock()
            self.sleep(self.window_s)
            quiet = (q0, self.clock())
        before = self.host.snapshot()
        l0 = self.clock()
        t.status, body = self.rage.load(model_id)
        l1 = self.clock()
        after = self.host.snapshot()
        t.read_mib = round(after.read_since(before) / MIB, 1)
        t.cache_seen = cache_seen(after.read_since(before), size)
        t.arc = after.arc_since(before)
        if t.status != 200:
            t.refused = body[:500]
        else:
            t.load_s = round(l1 - l0, 2)
            try:
                t.run = json.loads(body).get("run")
            except (ValueError, AttributeError):
                t.run = None
            u0 = self.clock()
            self.rage.unload(model_id)
            t.unload_s = round(self.clock() - u0, 2)
        if beside and quiet is not None:
            t.busy = {"model": self.busy_model,
                      **disturbance(self.background.completions(), quiet, (l0, l1))}
        self.emit({"kind": "trial", **asdict(t)})
        return t

    def run(self, plan: Plan) -> list[Trial]:
        results: list[Trial] = []
        if plan.raw:
            for m in plan.models:
                self.say(f"{m}: the raw read, cold and warm, and the hash")
                self.rage.unload(m)
                self.raw(m)
        started = False
        try:
            for bg, m, rep, cache in plan.trials():
                if bg != "idle" and not started:
                    if self.background is None or self.busy_model is None:
                        raise Refusal("a busy series needs --busy")
                    status, body = self.rage.load(self.busy_model)
                    if status != 200:
                        raise Refusal(f"the busy model {self.busy_model} did not load: "
                                      f"{status} {body}")
                    self.background.start()
                    started = True
                self.say(f"{m}: {bg}, {cache}, rep {rep}")
                results.append(self.trial(m, bg, rep, cache))
        finally:
            if started and self.background is not None:
                self.background.stop()
        return results


# ---- leave RAGE as it was found ----------------------------------------------

def holding(rows: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    return {r["id"]: r.get("device") for r in rows if r.get("state") in HOLDING}


def in_use(rows: Iterable[Mapping[str, Any]], models: Iterable[str]) -> list[str]:
    wanted = set(models)
    return [f"{r['id']} (asked {r['idle_s']} s ago)" for r in rows
            if r["id"] in wanted and r.get("state") in HOLDING
            and isinstance(r.get("idle_s"), int) and r["idle_s"] < IN_USE_S]


def restore(rage: Rage, before: Mapping[str, str | None], touched: Iterable[str],
            say: Callable[[str], None]) -> list[str]:
    """Load again what was loaded, unload what the run loaded. Returns what failed."""
    now = holding(rage.models())
    failed = []
    for m in sorted(set(touched)):
        if m in now and m not in before:
            rage.unload(m)
    for m, device in sorted(before.items()):
        if m not in now:
            status, body = rage.load(m, device)
            if status != 200:
                failed.append(f"{m}: {status} {body[:120]}")
                say(f"could not load {m} again: {status}")
    return failed


# ---- the summary -------------------------------------------------------------

def summarise(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per model, background and cache AS SEEN: the trials a wrong label would
    have mixed are kept apart."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for r in records:
        if r.get("kind") != "trial":
            continue
        key = (r.get("label", ""), r["model"], r["background"], r["cache_seen"])
        groups.setdefault(key, []).append(r)
    out = []
    for (label, m, bg, seen), rs in sorted(groups.items()):
        loads = [r["load_s"] for r in rs if r.get("load_s") is not None]
        unloads = [r["unload_s"] for r in rs if r.get("unload_s") is not None]
        lost = [r["busy"]["embedding_lost_s"] for r in rs
                if r.get("busy") and r["busy"].get("embedding_lost_s") is not None]
        out.append({
            "label": label or None, "model": m, "background": bg, "cache": seen,
            "n": len(rs), "runs": len({r.get("run") for r in rs if r.get("run")}),
            "refused": sum(1 for r in rs if r.get("refused")),
            "load_s_median": round(statistics.median(loads), 2) if loads else None,
            "load_s_max": max(loads) if loads else None,
            "unload_s_median": round(statistics.median(unloads), 2) if unloads else None,
            "read_mib_median": round(statistics.median(r["read_mib"] for r in rs), 1),
            "weights_mib": rs[0]["weights_mib"],
            "embedding_lost_s_median": round(statistics.median(lost), 2) if lost else None,
        })
    return out


def render(rows: Sequence[Mapping[str, Any]]) -> str:
    cols = ("label", "model", "background", "cache", "n", "refused", "load_s_median", "load_s_max",
            "unload_s_median", "read_mib_median", "weights_mib", "embedding_lost_s_median")
    head = ("label", "model", "background", "cache", "n", "refused", "load s", "max",
            "unload s", "read MiB", "weights MiB", "embedding lost s")
    table = [head] + [tuple("—" if r[c] is None else str(r[c]) for c in cols) for r in rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    return "\n".join("  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in table)


# ---- the command -------------------------------------------------------------

def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="louise dev load-profile",
        description="how long each model takes to come back to the card, from disk and "
                    "from RAM, idle and beside an embedding; loads and unloads models")
    p.add_argument("--rage", required=True, help="RAGE's URL (RAGE_URL)")
    p.add_argument("--models-dir", required=True, type=Path,
                   help="LocalAI's models directory (LOCALAI_MODELS_DIR): each model's "
                        "YAML names its weights")
    p.add_argument("--token-file", type=Path,
                   default=Path(os.environ["ADMIN_TOKEN_FILE"])
                   if os.environ.get("ADMIN_TOKEN_FILE") else None,
                   help="the admin token's file (default: $ADMIN_TOKEN_FILE)")
    p.add_argument("--models", type=_csv, default=(), help="comma-separated; default: all")
    p.add_argument("--cache", type=_csv, default=("cold", "warm"))
    p.add_argument("--busy", default=None,
                   help="an embedding model to keep busy: every trial runs idle and beside it")
    p.add_argument("--busy-concurrency", type=int, default=2)
    p.add_argument("--busy-chars", type=int, default=1500)
    p.add_argument("--window", type=float, default=20.0,
                   help="seconds the embedding is measured alone before each load")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--cold-by", choices=("fadvise", "drop-caches", "none"), default="fadvise")
    p.add_argument("--device", action="append", default=[],
                   help="a block device to count reads on (default: every whole disk)")
    p.add_argument("--skip-raw", action="store_true")
    p.add_argument("--out", type=Path, default=None, help="JSON lines, one per trial")
    p.add_argument("--label", default="",
                   help="names this series in every record and in the summary, so two "
                        "runs (a kernel cache empty, then full) are kept apart")
    p.add_argument("--json", action="store_true", help="the summary as JSON")
    p.add_argument("--dry-run", action="store_true", help="the plan; nothing is touched")
    a = p.parse_args(argv)
    bad = set(a.cache) - {"cold", "warm"}
    if bad:
        p.error(f"--cache takes cold and warm, not {sorted(bad)}")
    if a.reps < 1:
        p.error("--reps must be at least 1")
    return a


def _read_token(path: Path | None) -> str:
    if path is None:
        raise Refusal("no admin token: give --token-file or set ADMIN_TOKEN_FILE")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise Refusal(f"the token file {path} cannot be read: {type(e).__name__}") from None
    if not token:
        raise Refusal(f"the token file {path} is empty")
    return token


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None,
         host: Host | None = None, background: Background | None = None,
         clock: Callable[[], float] = time.monotonic,
         sleep: Callable[[float], None] = time.sleep) -> int:
    a = parse_args(argv)

    def say(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        cfg = models_dir(a.models_dir)
    except ModelsError as e:
        say(f"refused: {e}")
        return EXIT_REFUSED
    models = a.models or tuple(sorted(cfg.models))
    unknown = [m for m in (*models, *([a.busy] if a.busy else [])) if m not in cfg.models]
    if unknown:
        say(f"refused: not models of {a.models_dir}: {unknown}")
        return EXIT_REFUSED
    missing = [m for m in models if not Path(cfg.models[m].weights).exists()]
    if missing:
        say(f"refused: weights not readable here for {missing}; mount the models "
            f"directory where LocalAI reads it")
        return EXIT_REFUSED
    host = host or Host(devices=a.device)
    backgrounds = ("idle",) + ((f"busy:{a.busy}",) if a.busy else ())
    plan = Plan(models=models, caches=a.cache, backgrounds=backgrounds, reps=a.reps,
                raw=not a.skip_raw)
    kinds = {m: host.fstype(Path(cfg.models[m].weights)) for m in models}
    if a.cold_by == "fadvise" and "cold" in a.cache and "zfs" in kinds.values():
        say("note: fadvise does not evict ZFS's ARC; the cold trials on ZFS will be "
            "classified warm. Use --cold-by drop-caches, or set primarycache=metadata "
            "on the dataset and --cold-by none")
    if a.cold_by == "drop-caches" and not host.can_drop_caches():
        say("refused: --cold-by drop-caches needs /proc/sys/vm/drop_caches writable "
            "(a privileged container)")
        return EXIT_REFUSED
    trials = plan.trials()
    if a.dry_run:
        print(f"{len(trials)} trials over {len(models)} models "
              f"({', '.join(backgrounds)}; {', '.join(a.cache)}; {a.reps} reps)"
              + ("; and a raw read per model" if plan.raw else ""))
        for bg, m, rep, cache in trials:
            print(f"  {m}  {bg}  {cache}  rep {rep}")
        return EXIT_OK
    try:
        token = _read_token(a.token_file)
    except Refusal as e:
        say(f"refused: {e}")
        return EXIT_REFUSED

    own_client = client is None
    client = client or httpx.Client()
    rage = Rage(a.rage, token, client)
    records: list[dict[str, Any]] = []
    out = a.out.open("a", encoding="utf-8") if a.out else None

    def emit(rec: dict[str, Any]) -> None:
        records.append(rec)
        if out is not None:
            out.write(json.dumps(rec, sort_keys=True) + "\n")
            out.flush()

    previous = signal.signal(signal.SIGTERM, _on_sigterm) \
        if threading.current_thread() is threading.main_thread() else None
    code = EXIT_OK
    before: dict[str, str | None] | None = None
    try:
        rows = rage.models()
        busy_rows = in_use(rows, (*models, *([a.busy] if a.busy else [])))
        if busy_rows:
            say(f"refused: in use — {', '.join(busy_rows)}. Pause the node "
                f"(louise node pause) and run again")
            return EXIT_REFUSED
        before = holding(rows)
        emit({"kind": "scenario", "at": int(time.time()), "rage": a.rage,
              "models": list(models), "busy": a.busy, "cold_by": a.cold_by,
              "devices": sorted(host.snapshot().disk), "filesystems": kinds,
              "host_mib": host.memory(), "loaded_at_start": before, "gpu": rage.gpu(),
              "window_s": a.window, "busy_concurrency": a.busy_concurrency,
              "label": a.label})
        if a.busy and background is None:
            background = EmbeddingLoop(a.rage, a.busy, concurrency=a.busy_concurrency,
                                       chars=a.busy_chars, client=client, clock=clock)
        profiler = Profiler(rage, host, cfg, cold_by=a.cold_by, window_s=a.window,
                            emit=emit, background=background, busy_model=a.busy,
                            label=a.label,
                            clock=clock, sleep=sleep, say=say)
        profiler.run(plan)
    except KeyboardInterrupt:
        say("interrupted: putting RAGE back as it was")
        code = EXIT_INTERRUPTED
    except RageUnreachable as e:
        say(f"deferred: RAGE is unreachable: {e}")
        code = EXIT_DEFERRED
    except Refusal as e:
        say(f"refused: {e}")
        code = EXIT_REFUSED
    except (RuntimeError, httpx.HTTPError, OSError) as e:
        say(f"failed: {e}")
        code = EXIT_FAILED
    finally:
        if before is not None:
            try:
                failed = restore(rage, before, (*models, *([a.busy] if a.busy else [])), say)
                if failed and code == EXIT_OK:
                    code = EXIT_FAILED
            except (RageUnreachable, Refusal, RuntimeError, httpx.HTTPError) as e:
                say(f"could not put RAGE back: {e}; loaded at the start: {sorted(before)}")
                code = code if code != EXIT_OK else EXIT_FAILED
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        if out is not None:
            out.close()
        if own_client:
            client.close()
    rows_out = summarise(records)
    raws = [r for r in records if r.get("kind") == "raw"]
    if a.json:
        print(json.dumps({"summary": rows_out, "raw": raws}, indent=2, sort_keys=True))
    else:
        for r in raws:
            print(f"{r['model']}: {r['weights_mib']} MiB — first read {r['first_read_s']} s "
                  f"({r['first_read_seen']}, the disk read {r['first_read_mib']} MiB), "
                  f"warm read {r['warm_read_s']} s, SHA-256 {r['sha256_s']} s")
        if rows_out:
            print(render(rows_out))
    return code


if __name__ == "__main__":
    sys.exit(main())
