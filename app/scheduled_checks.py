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
  - only in a mode that PARSES (`node_state.runs`): a check reads the drag
    and the origin; a paused or download-only node runs none;
  - DUE when the collection has a snapshot and either the last scheduled
    check was of another snapshot or one cadence has passed since it;
  - BOUNDED: a stop ends the child (terminate, then kill), and a check
    writes its attestations only at its end, so a stopped one leaves nothing
    half-written; a run past `TIMEOUT_S` is ended and recorded as such.

What it keeps: `<root>/<name>/state/scheduled-check.json` — when, which
snapshot, the exit code, how long, and the report (09 §9.8) — which `hold
collection show` displays. The attestations are the check's own (`hold
check` writes them), published by `share` like any other.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

STATE_FILE = "scheduled-check.json"
BEAT_S = 600.0                    # how often the runner looks for a due collection
TIMEOUT_S = 3 * 3600.0            # gutenberg's plan took 25.5 min; three hours is a stuck one
STOP_GRACE_S = 30.0
PERIOD_S = {"monthly": 30 * 86400.0, "weekly": 7 * 86400.0}


def state_path(root: Path, name: str) -> Path:
    return Path(root) / name / "state" / STATE_FILE


def last(root: Path, name: str) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(root, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def due(root: Path, name: str, codex, snapshot_id: str | None, *, now: float) -> bool:
    """Has this collection a snapshot, and either no scheduled check of it
    yet or none within one cadence?"""
    if snapshot_id is None:
        return False
    prev = last(root, name)
    if prev is None or prev.get("snapshot_id") != snapshot_id:
        return True
    period = PERIOD_S.get(getattr(getattr(codex, "update", None), "cadence", "monthly"), PERIOD_S["monthly"])
    return now - float(prev.get("time", 0)) >= period


def argv_for(collection) -> list[str]:
    """`hold check` with the collection's origin when it has one here: a
    fetched copy (`source = "drag"`) has none, and its `records-vs-origin`
    says so (inconclusive, never a pass)."""
    argv = [sys.executable, "-m", "app.louise", "hold", "check", collection.name, "--json"]
    corpus = Path(collection.corpus_dir)
    if getattr(collection, "source", "origin") != "drag" and corpus.is_dir():
        argv += ["--origin", str(corpus)]
    return argv


def _nice() -> None:                  # in the child, before exec
    try:
        os.nice(10)
    except OSError:
        pass


def run_one(collection, *, stop: Callable[[], bool], timeout_s: float = TIMEOUT_S,
            argv: list[str] | None = None) -> dict[str, Any]:
    """One plan run in a child; returns what `record` keeps. Never raises."""
    t0 = time.time()
    try:
        child = subprocess.Popen(argv or argv_for(collection), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, preexec_fn=_nice if os.name == "posix" else None)
    except OSError as e:
        return {"exit": None, "outcome": f"could not start: {type(e).__name__}: {e}", "secs": 0.0}
    ended = None
    while child.poll() is None:
        if stop() or time.time() - t0 > timeout_s:
            ended = "stopped" if stop() else "timed out"
            child.terminate()
            try:
                child.wait(STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            break
        time.sleep(1.0)
    out, err = child.communicate()
    report: Any = None
    try:
        report = json.loads(out.decode("utf-8")) if out.strip() else None
    except ValueError:
        report = None
    tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
    outcome = ended or {0: "pass", 1: "fail", 2: "refused", 3: "inconclusive"}.get(child.returncode,
                                                                               f"exit {child.returncode}")
    return {"exit": child.returncode, "outcome": outcome, "secs": round(time.time() - t0, 1),
            "report": report, "stderr_tail": tail}


def record(root: Path, name: str, snapshot_id: str, got: dict[str, Any], *, now: float) -> Path:
    p = state_path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"time": now, "snapshot_id": snapshot_id, **got}, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)
    return p


def turn(*, stop: Callable[[], bool], log: Callable[[str], None], now: float | None = None,
         run: Callable[..., dict[str, Any]] = run_one) -> str | None:
    """One look: the first due collection, by name, run and recorded.
    Returns its name, or None when nothing was due or the mode runs none."""
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
            snap = seeder.current_snapshot(root / c.name / "drag")
        except Exception:   # noqa: BLE001 — a drag that does not read is not checked this beat
            continue
        sid = snap.snapshot_id if snap else None
        t = time.time() if now is None else now
        if not due(root, c.name, codex, sid, now=t):
            continue
        log(f"{c.name}: the plan, scheduled (03 §3.2), snapshot {sid}")
        got = run(c, stop=stop)
        if got.get("outcome") == "stopped":
            return c.name                      # not recorded: it runs again at the next start
        record(root, c.name, sid, got, now=t)
        log(f"{c.name}: scheduled check {got['outcome']} in {got['secs']:.0f}s")
        return c.name
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
