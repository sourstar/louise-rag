"""The plan run in the background (03 §3.2: "the same report is what the
worker produces in the background, once per cadence for every collection it
holds, with a fresh seed each time, and keeps in the collection's state
directory"). Until 2026-10-08 nothing ran `roots`, `chunks` or
`records-vs-origin` by itself, so a copy in quarantine cleared only when
someone ran `hold check` by hand.

WHAT RUNS WHEN, measured on the node first (docs/notes/scheduling.md §6;
docs/notes/scheduling-log.md, 2026-10-08): the whole plan of a three-book
collection takes 1-2 s and 0.15 GB; pubmed's 13.6 min and 2.0 GB at peak
(18.7 GB before the snapshot and origin-check fixes of the same day);
gutenberg's 25.5 min and 2.6 GB. So:

  - a CHILD PROCESS per run (`louise hold check <name> --origin <corpus>
    --json`), at nice 10: its memory is returned when it exits and it takes
    no GIL from the pipeline threads — a 25-minute check inside the worker
    would have been the long CPU job §1 forbids every shared thread;
  - ONE AT A TIME on the node, so the peak is one check's;
  - only in a mode that PARSES (`node_state.runs`): a check reads the SHARD
    and the origin; a paused or download-only node runs none;
  - DUE when the collection has a snapshot and either the last scheduled
    check was of another snapshot or one cadence has passed since it;
  - BOUNDED: a stop ends the child (terminate, then kill), and a check
    writes its attestations only at its end, so a stopped one leaves nothing
    half-written; a run past `TIMEOUT_S` is ended and recorded as such;
  - ADMITTED BY MEMORY (2026-11-07): a check starts only when the worker's
    cgroup has room for its last measured peak (`admission`); until it has,
    the check is deferred, never failed;
  - BOUNDED IN RETRIES (2026-11-07): a run the worker died under is found
    by its started marker at the next turn and recorded as interrupted;
    after `CHECK_MAX_INTERRUPTS` of one snapshot it waits a cadence (`due`).
    Before, `turn` recorded nothing for it, so check → OOM kill → restart →
    the same check from zero, for ever.

What it keeps: `<root>/<name>/state/scheduled-check.json` — when, which
snapshot, the exit code, how long, the peak memory of the check's session,
the interruptions of its snapshot, and the report (09 §9.8) — which `hold
collection show` displays. Beside it `scheduled-check.started.json` while a
run is in hand, and the last run's whole stderr, `scheduled-check.stderr`.
The attestations are the check's own (`hold check` writes them), published
by `share` like any other.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

STATE_FILE = "scheduled-check.json"
#: written before a run starts and removed at its end or at a stop: one that
#: a turn finds is a run the worker died under
STARTED_FILE = "scheduled-check.started.json"
#: the last run's stderr, whole; the record keeps its last lines
STDERR_FILE = "scheduled-check.stderr"
BEAT_S = 600.0                    # how often the runner looks for a due collection
TIMEOUT_S = 3 * 3600.0            # gutenberg's plan took 25.5 min; three hours is a stuck one
STOP_GRACE_S = 30.0
#: the check's pool is the parse pool's size divided by this (`check_processes`)
CHECK_SHARE = 4
PERIOD_S = {"monthly": 30 * 86400.0, "weekly": 7 * 86400.0}
INTERRUPTED = "interrupted"
#: THE CRASH-LOOP BOUND, the standard one — systemd's StartLimitBurst within
#: StartLimitIntervalSec, Kubernetes' CrashLoopBackOff — with the collection's
#: cadence as the interval: after this many runs of ONE snapshot that the
#: worker died under, the check waits a cadence after the last, or a new
#: snapshot. 2026-11-07: wikidata's check (~6.4 GB) beside the parse pool
#: passed the worker's 24 GiB cap, the kernel killed the worker at 06:17, and
#: at 06:18:01 the restarted worker began the same check from zero.
CHECK_MAX_INTERRUPTS = 2
#: ADMISSION: the room a check must find in the worker's cgroup, as a multiple
#: of its last measured peak. The peak is sampled once a second, so the true
#: one lies between samples (pubmed, 2026-10-08: 1.68 GB sampled, 2.0 GB by
#: hand), and the parse pool beside the check moves while it runs.
ADMIT_MARGIN = 1.2
#: ...and when no peak is measured yet: the largest scheduled check measured to
#: its end, gutenberg's 2.6 GB (2026-10-08; pubmed 2.0 GB), × ADMIT_MARGIN =
#: 3.1 GB, rounded up to 4 GiB. A bigger check is admitted once on this and
#: keeps its own peak from then on — in its started marker while it grows, so
#: a run the kernel kills leaves its peak too, and the next start asks for it.
ADMIT_DEFAULT_BYTES = 4 * 2 ** 30
#: a deferred check says so at most this often, per collection
ADMIT_LOG_S = 600.0
CGROUP_DIR = "/sys/fs/cgroup"
PROC_DIR = "/proc"
#: the started marker is rewritten when the peak grows by this much, or 5 %
PEAK_WRITE_STEP = 64 * 2 ** 20
#: how much of the end of stderr is read back for its last lines
TAIL_BYTES = 64 * 1024


def state_path(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / STATE_FILE


def started_path(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / STARTED_FILE


def _read_json(p: Path) -> dict[str, Any] | None:
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def _write_json(p: Path, obj: dict[str, Any]) -> None:
    """Atomically: a temporary beside it, then a rename."""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def last(root: Path, name: str) -> dict[str, Any] | None:
    return _read_json(state_path(root, name))


def started(root: Path, name: str) -> dict[str, Any] | None:
    return _read_json(started_path(root, name))


def period_of(codex) -> float:
    return PERIOD_S.get(getattr(getattr(codex, "update", None), "cadence", "monthly"), PERIOD_S["monthly"])


def held_back(prev: dict[str, Any] | None, *, now: float, period: float) -> bool:
    """CHECK_MAX_INTERRUPTS interrupted runs of the record's snapshot, the
    last started within one cadence: the crash-loop bound holds."""
    return (prev is not None and prev.get("outcome") == INTERRUPTED
            and int(prev.get("interrupts") or 0) >= CHECK_MAX_INTERRUPTS
            and now - float(prev.get("time", 0)) < period)


def due(root: Path, name: str, codex, snapshot_id: str | None, *, now: float,
        snapshot_hash: str | None = None) -> bool:
    """Has this collection a snapshot, and either no scheduled check of it
    yet or none within one cadence? A snapshot is ITS HASH, not its id: one
    withdrawn before it was shared and created again under the same id
    (wikipedia-v8's 2026-09, 2026-10-28) is a snapshot never checked. A
    record from before the hash was kept compares by id, as it did. A run
    the worker died under is no check: due again at once, until
    CHECK_MAX_INTERRUPTS of them, then a cadence after the last."""
    if snapshot_id is None:
        return False
    prev = last(root, name)
    if prev is None or prev.get("snapshot_id") != snapshot_id:
        return True
    if snapshot_hash is not None and prev.get("snapshot_hash") not in (None, snapshot_hash):
        return True
    period = period_of(codex)
    if prev.get("outcome") == INTERRUPTED:
        return not held_back(prev, now=now, period=period)
    return now - float(prev.get("time", 0)) >= period


def mark_started(root: Path, name: str, snapshot_id: str, *, now: float,
                 snapshot_hash: str | None = None) -> dict[str, Any]:
    """The started marker, before the run, written atomically as `record`
    writes: which snapshot, when, the worker's pid, and an id of this run.
    Never re-creates a collection by name."""
    if not (Path(root) / name).is_dir():
        raise FileNotFoundError(f"{Path(root) / name}: no such collection here; no check is started")
    p = started_path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    marker = {"time": now, "snapshot_id": snapshot_id, "pid": os.getpid(), "run": uuid.uuid4().hex,
              **({"snapshot_hash": snapshot_hash} if snapshot_hash else {})}
    _write_json(p, marker)
    return marker


def clear_started(root: Path, name: str, run: str | None = None) -> bool:
    """The marker removed — only the run's own when `run` is given. True
    when one was."""
    if run is not None and (started(root, name) or {}).get("run") != run:
        return False
    try:
        started_path(root, name).unlink()
        return True
    except OSError:
        return False


def _peak_writer(root: Path, name: str, marker: dict[str, Any]) -> Callable[[int], None]:
    """The marker rewritten with the peak so far: a run the kernel kills
    leaves the memory it had reached. Only this run's marker, by its id."""
    def write(peak: int) -> None:
        if (started(root, name) or {}).get("run") == marker["run"]:
            _write_json(started_path(root, name), {**marker, "peak_rss": int(peak)})
    return write


def settle_interrupted(root: Path, name: str, *, period: float) -> dict[str, Any] | None:
    """A started marker with no end: the worker died under that run — it
    removes its own at every end and at a stop. Recorded INTERRUPTED and
    counted per snapshot: one more than the record's when the record is the
    same snapshot's interruption started within a cadence, one otherwise.
    Its peak is the larger of the marker's and the record's. Returns the
    record written, or None: no marker, or one whose run was recorded (the
    worker died between the record and the marker's removal)."""
    m = started(root, name)
    if m is None:
        return None
    prev = last(root, name)
    t = float(m.get("time", 0))
    if prev is not None and prev.get("outcome") != INTERRUPTED and float(prev.get("time", 0)) >= t:
        clear_started(root, name)
        return None
    same = (prev is not None and prev.get("outcome") == INTERRUPTED
            and prev.get("snapshot_id") == m.get("snapshot_id")
            and prev.get("snapshot_hash") == m.get("snapshot_hash")
            and t - float(prev.get("time", 0)) < period)
    peaks = [int(x) for x in (m.get("peak_rss"), (prev or {}).get("peak_rss")) if x]
    got = {"exit": None, "outcome": INTERRUPTED, "secs": None,
           "interrupts": int(prev.get("interrupts") or 0) + 1 if same else 1,
           "pid": m.get("pid"), **({"peak_rss": max(peaks)} if peaks else {})}
    record(root, name, m.get("snapshot_id"), got, now=t, snapshot_hash=m.get("snapshot_hash"))
    clear_started(root, name)
    return last(root, name)


def retry_at(prev: dict[str, Any] | None, codex) -> float | None:
    """When a check the crash-loop bound holds back is due again (a new
    snapshot makes it due sooner); None when it is not held back."""
    if prev is None or prev.get("outcome") != INTERRUPTED \
            or int(prev.get("interrupts") or 0) < CHECK_MAX_INTERRUPTS:
        return None
    return float(prev.get("time", 0)) + period_of(codex)


def _utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(t))


def summary(root: Path, name: str, codex=None) -> dict[str, Any] | None:
    """What `hold collection show` displays of the last scheduled check;
    for an interrupted one, the same in plain words (`note`)."""
    got = last(root, name)
    if got is None:
        return None
    out = {k: got.get(k) for k in ("time", "snapshot_id", "outcome", "exit", "secs")}
    if got.get("peak_rss"):
        out["peak_rss"] = got["peak_rss"]
    if got.get("outcome") == INTERRUPTED:
        n = int(got.get("interrupts") or 0)
        times = f"{n} time{'s' if n != 1 else ''}"
        out["interrupts"] = n
        when = retry_at(got, codex)
        if when is None:
            out["note"] = (f"interrupted {times}: the worker died under the check of snapshot "
                           f"{got.get('snapshot_id')} (a crash, or the kernel's out-of-memory kill); "
                           f"retried as soon as the worker has room for it")
        else:
            out["retry_at"] = when
            out["note"] = (f"interrupted {times}: the worker died under each check of snapshot "
                           f"{got.get('snapshot_id')}; retried at {_utc(when)}, or sooner for a new snapshot")
    return out


# ---- admission by memory -----------------------------------------------------

def cgroup_memory(cgroup: str | None = None) -> tuple[int, int] | None:
    """(limit, in use) of this process's cgroup (v2), read in the container
    as `config.available_cpus` reads its CPU quota; None where no limit
    reads — no cgroup v2, `memory.max` "max", a file that does not parse.
    In use is the WORKING SET, the figure Kubernetes' kubelet and cAdvisor
    evict on: `memory.current` less `memory.stat`'s `inactive_file`, the
    page cache the kernel reclaims before it kills anything. With
    `memory.current` alone, a worker that reads its archives fills its
    cgroup with cache and no check would ever be admitted."""
    cgroup = cgroup or CGROUP_DIR
    try:
        raw = Path(cgroup, "memory.max").read_text(encoding="ascii").strip()
        if raw == "max":
            return None
        limit = int(raw)
        current = int(Path(cgroup, "memory.current").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    inactive = 0
    try:
        for line in Path(cgroup, "memory.stat").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(" ")
            if key == "inactive_file":
                inactive = int(value)
                break
    except (OSError, ValueError):
        inactive = 0
    return limit, max(0, current - inactive)


def last_peak(root: Path, name: str) -> int | None:
    got = last(root, name) or {}
    try:
        return int(got["peak_rss"]) if got.get("peak_rss") else None
    except (TypeError, ValueError):
        return None


def _gib(n: float) -> str:
    return f"{n / 2 ** 30:.1f}"


def admission(root: Path, name: str, *, cgroup: str | None = None) -> tuple[bool, str]:
    """May this collection's check start now? When the worker's cgroup has
    room for it: (limit − in use) ≥ its last peak × ADMIT_MARGIN, or ≥
    ADMIT_DEFAULT_BYTES when no peak is known. No limit that reads:
    admitted, as before. The verdict, and its numbers in words."""
    mem = cgroup_memory(cgroup)
    if mem is None:
        return True, "no cgroup memory limit reads here"
    limit, used = mem
    room = limit - used
    peak = last_peak(root, name)
    need = int(peak * ADMIT_MARGIN) if peak else ADMIT_DEFAULT_BYTES
    why = (f"{_gib(room)} GiB free of the worker's {_gib(limit)} GiB ({_gib(used)} GiB in use, page cache "
           f"not counted); the check needs {_gib(need)} GiB ("
           + (f"its last peak, {_gib(peak)} GiB, × {ADMIT_MARGIN}" if peak
              else "no peak of it measured yet: the default") + ")")
    return room >= need, why


#: when each collection's deferral was last said, keyed on its folder (a path)
_deferred_said: dict[str, float] = {}


def _say_deferred(log: Callable[[str], None], root: Path, name: str, why: str, *, now: float) -> None:
    key = str(Path(root) / name)
    if now - _deferred_said.get(key, float("-inf")) < ADMIT_LOG_S:
        return
    _deferred_said[key] = now
    log(f"{name}: scheduled check deferred — {why}; looked at again each beat")


def session_rss(sid: int, proc: str = PROC_DIR) -> int | None:
    """The resident memory of session `sid` — the check (its leader:
    start_new_session) and its pool's children, which inherit the session
    — as the sum of each process's /proc/<pid>/status VmRSS. A page shared
    copy-on-write counts once per process that maps it, so the sum errs
    high: the admission it feeds errs on the side of waiting. (The
    proportional figure, Pss in /proc/<pid>/smaps_rollup, walks the page
    tables of a 5 GB process each second; not worth it for a margin.)
    None when no process of the session reads."""
    try:
        pids = [e for e in os.listdir(proc) if e.isdigit()]
    except OSError:
        return None
    total, seen = 0, False
    for pid in pids:
        try:
            with open(f"{proc}/{pid}/stat", "rb") as f:
                stat = f.read()
            # after the command's ")": state, ppid, pgrp, session
            if int(stat[stat.rindex(b")") + 2:].split()[3]) != sid:
                continue
            with open(f"{proc}/{pid}/status", "rb") as f:
                for line in f:
                    if line.startswith(b"VmRSS:"):
                        total += int(line.split()[1]) * 1024
                        seen = True
                        break
        except (OSError, ValueError, IndexError):
            continue                          # gone meanwhile, or a zombie
    return total if seen else None


# ---- the run -------------------------------------------------------------------

def argv_for(collection) -> list[str]:
    """`hold check` with the collection's origin when it has one here: a
    fetched copy (`source = "shard"`) has none, and its `records-vs-origin`
    says so (inconclusive, never a pass)."""
    argv = [sys.executable, "-m", "app.louise", "hold", "check", collection.name, "--json"]
    corpus = Path(collection.corpus_dir)
    if getattr(collection, "source", "origin") != "shard" and corpus.is_dir():
        argv += ["--origin", str(corpus)]
    return argv


def check_processes() -> int:
    """The background check's pool: a quarter of the parse pool's. The check
    runs inside the worker's memory cap BESIDE the parse pool, and with the
    whole quota it doubled the worker's processes: wikidata's check at twelve
    children (5.3 GB) beside the parse pool's eleven (12 GB) and the worker
    (4.5 GB) passed the 24 GiB cap, and the kernel killed the worker twice
    in seven minutes (2026-11-07, 00:35 and 00:42). A check is a background
    verification at nice 10, never a round's work: slower is right."""
    from app import parallel
    return max(1, parallel.processes() // CHECK_SHARE)


def child_env() -> dict[str, str]:
    return {**os.environ, "PARSE_PROCESSES": str(check_processes())}


def _signal(child: subprocess.Popen, sig: int) -> None:
    """`sig` to the check and every process of its session: its pool's
    children are in it (start_new_session)."""
    try:
        if os.name == "posix":
            os.killpg(child.pid, sig)
        else:
            child.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass                          # already gone


def _nice() -> None:                  # in the child, before exec
    try:
        os.nice(10)
    except OSError:
        pass


def _tail(f, lines: int = 3) -> list[str]:
    """The last lines of a file the child wrote, reading only its end."""
    f.seek(0, os.SEEK_END)
    f.seek(max(0, f.tell() - TAIL_BYTES))
    return f.read().decode("utf-8", "replace").strip().splitlines()[-lines:]


def run_one(collection, *, stop: Callable[[], bool], timeout_s: float = TIMEOUT_S,
            argv: list[str] | None = None, log_dir: Path | None = None,
            on_peak: Callable[[int], None] | None = None) -> dict[str, Any]:
    """One plan run in a child; returns what `record` keeps. Never raises.

    Its stdout and stderr go to FILES, not pipes. Read with `communicate`
    only at the end, a check that wrote more than a pipe buffer (64 KiB)
    blocked for ever on its write; a file never fills. The subprocess docs'
    other answer, `communicate(timeout=)` in the poll loop (or a reader
    thread per pipe), holds the whole output in the worker's memory — the
    memory a check must not take. stdout is the report, read whole at the
    end from an unnamed file (`tempfile.TemporaryFile`) in `log_dir`; stderr
    is `log_dir`/STDERR_FILE, kept whole for the operator, its last lines in
    the record. `log_dir` is the collection's state directory (/data, a
    disk — a tmpfs would be charged to the worker's cgroup); None: the
    system's temporary directory.

    The session's resident memory is sampled each second (`session_rss`):
    its peak is in the result, and `on_peak` hears it as it grows."""
    t0 = time.time()
    posix = os.name == "posix"
    with contextlib.ExitStack() as files:
        try:
            out_f = files.enter_context(tempfile.TemporaryFile(dir=log_dir))
            err_f = files.enter_context(open(Path(log_dir) / STDERR_FILE, "w+b") if log_dir is not None
                                        else tempfile.TemporaryFile())
            # its OWN SESSION, so a stop reaches the check's pool too (below),
            # and a pool of CHECK_PROCESSES children: the worker's cgroup holds
            # this check beside the parse pool
            child = subprocess.Popen(argv or argv_for(collection), stdout=out_f, stderr=err_f,
                                     preexec_fn=_nice if posix else None,
                                     start_new_session=posix, env=child_env())
        except OSError as e:
            return {"exit": None, "outcome": f"could not start: {type(e).__name__}: {e}", "secs": 0.0}
        ended = None
        peak = said = 0
        while child.poll() is None:
            if stop() or time.time() - t0 > timeout_s:
                ended = "stopped" if stop() else "timed out"
                _signal(child, signal.SIGTERM)
                try:
                    child.wait(STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    _signal(child, signal.SIGKILL)
                    child.wait()
                break
            rss = session_rss(child.pid) if posix else None
            if rss is not None and rss > peak:
                peak = rss
                if on_peak is not None and peak - said >= max(PEAK_WRITE_STEP, said // 20):
                    said = peak
                    try:
                        on_peak(peak)
                    except Exception:   # noqa: BLE001 — a bookkeeping write never ends the run
                        pass
            time.sleep(1.0)
        # the pool's children outlive a check the kernel or a stop ended: they
        # wait on a queue whose parent is gone, holding their memory (live
        # 2026-11-07: twelve of wikidata's check, 5.3 GB, after its parent went)
        _signal(child, signal.SIGKILL)
        child.wait()
        out_f.seek(0)
        out = out_f.read()
        tail = _tail(err_f)
    report: Any = None
    try:
        report = json.loads(out.decode("utf-8")) if out.strip() else None
    except ValueError:
        report = None
    outcome = ended or {0: "pass", 1: "fail", 2: "refused", 3: "inconclusive"}.get(child.returncode,
                                                                               f"exit {child.returncode}")
    return {"exit": child.returncode, "outcome": outcome, "secs": round(time.time() - t0, 1),
            "report": report, "stderr_tail": tail, **({"peak_rss": peak} if peak else {})}


def record(root: Path, name: str, snapshot_id: str, got: dict[str, Any], *, now: float,
           snapshot_hash: str | None = None) -> Path:
    """The report, under the collection's state directory — which must
    exist: a record never re-creates a collection by name (`turn` resolves
    the name by identity first)."""
    p = state_path(root, name)
    if not (Path(root) / name).is_dir():
        raise FileNotFoundError(f"{Path(root) / name}: no such collection here; the report is not kept")
    p.parent.mkdir(parents=True, exist_ok=True)
    _write_json(p, {"time": now, "snapshot_id": snapshot_id,
                    **({"snapshot_hash": snapshot_hash} if snapshot_hash else {}), **got})
    return p


def _end_started(root: Path, name: str, collection_id: str, run: str) -> None:
    """The run's marker removed, under the name the collection holds now."""
    if clear_started(root, name, run) or (Path(root) / name).is_dir():
        return
    from app.hold import collection as col
    where = col.where_now(root, name, collection_id)
    if where is not None:
        clear_started(root, where, run)


def turn(*, stop: Callable[[], bool], log: Callable[[str], None], now: float | None = None,
         run: Callable[..., dict[str, Any]] | None = None, cgroup: str | None = None) -> str | None:
    """One look: the first due collection, by name, that the worker has
    room for, run and recorded. Returns its name, or None when nothing was
    due or admitted, or the mode runs none. `run` replaces `run_one`."""
    from app import collections as collections_mod, node_state
    from app.share import seeder
    if not node_state.runs(node_state.peek_worker_state(), node_state.PARSE):
        return None
    root = collections_mod.louise_root() / "collections"
    try:
        collections = collections_mod.load_all()
    except Exception:   # noqa: BLE001 — the next beat
        return None
    for c in sorted(collections, key=lambda c: c.name):
        if stop():
            return None
        codex = getattr(c, "codex", None)
        if codex is None:
            continue
        try:
            snap = seeder.current_snapshot(root / c.name / "shard")
        except Exception:   # noqa: BLE001 — a SHARD that does not read is not checked this beat
            continue
        sid = snap.snapshot_id if snap else None
        shash = getattr(snap, "hash", None) if snap else None
        t = time.time() if now is None else now
        try:
            died = settle_interrupted(root, c.name, period=period_of(codex))
        except OSError:
            died = None
        if died is not None:
            when = retry_at(died, codex)
            log(f"{c.name}: the scheduled check of snapshot {died.get('snapshot_id')} started "
                f"{_utc(float(died['time']))} never ended — the worker (pid {died.get('pid')}) died under it; "
                f"interrupted {died['interrupts']} time(s), "
                + (f"not retried before {_utc(when)} or a new snapshot (crash-loop bound, "
                   f"{CHECK_MAX_INTERRUPTS})" if when else "retried"))
        if not due(root, c.name, codex, sid, now=t, snapshot_hash=shash):
            continue
        admitted, why = admission(root, c.name, cgroup=cgroup)
        if not admitted:
            _say_deferred(log, root, c.name, why, now=t)
            continue
        log(f"{c.name}: the plan, scheduled (03 §3.2), snapshot {sid}")
        marker = mark_started(root, c.name, sid, now=t, snapshot_hash=shash)

        def stopping(name: str = c.name, run_id: str = marker["run"]) -> bool:
            # a stop is no interruption: the marker goes the moment the run
            # sees it, before the child is ended — the worker may exit first
            if stop():
                clear_started(root, name, run_id)
                return True
            return False
        runner = run or functools.partial(run_one, log_dir=started_path(root, c.name).parent,
                                          on_peak=_peak_writer(root, c.name, marker))
        try:
            got = runner(c, stop=stopping)
            if got.get("outcome") == "stopped":
                return c.name                      # not recorded: it runs again at the next start
            # by identity, not by the name the run started under: a collection
            # renamed during a 24-minute check (the gutenberg re-pin) is recorded
            # under its new name, one forgotten meanwhile not at all
            from app.hold import collection as col
            where = col.where_now(root, c.name, codex.collection_id)
            if where is None:
                log(f"{c.name}: scheduled check {got['outcome']} in {got['secs']:.0f}s — the collection is held "
                    f"under no name now (forgotten while it was checked); not recorded")
                return c.name
            record(root, where, sid, got, now=t, snapshot_hash=shash)
            log(f"{where}: scheduled check {got['outcome']} in {got['secs']:.0f}s"
                + (f", peak {_gib(got['peak_rss'])} GiB" if got.get("peak_rss") else "")
                + (f" (started as {c.name}, renamed meanwhile)" if where != c.name else ""))
            return c.name
        finally:
            _end_started(root, c.name, codex.collection_id, marker["run"])
    return None


def loop(stop, log: Callable[[str], None] = print, beat_s: float = BEAT_S) -> None:
    """The worker's thread: a turn, then wait a beat; a turn that ran one
    looks again at once, so a node with several due runs them back to back,
    one at a time."""
    while not stop.is_set():
        try:
            ran = turn(stop=stop.is_set, log=log)
        except Exception as e:   # noqa: BLE001 — the thread outlives one bad turn
            log(f"scheduled checks: {type(e).__name__}: {e}"[:300])
            ran = None
        if ran is None and stop.wait(beat_s):
            return
