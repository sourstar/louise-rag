"""The Ask page's queries as JOBS that outlive the page's connection
(operator, 2026-10-28: "Prepare a web proxy so that the web gui session is
not reset by the browser dropping a query. Also if the query is stuck or
dropped I should see a notification in the gui").

Before this, a search lived exactly as long as the request that asked for
it: `cancel.while_connected` stopped it the moment the browser's connection
went (12:05Z that day: "/ask/query: the caller left after 43.8 s; the search
is cancelled"), so a dropped connection, a phone that slept, or a reload
lost the query and the page started over.

Here the page STARTS a job and LISTENS to it. The job runs in the web
process whatever happens to the connection; what it says is kept as
numbered events, and a listener that comes back names the last one it saw
and gets the rest. Server-Sent Events (WHATWG HTML §9.2), with ONE
departure: the standard's reconnection sends `Last-Event-ID` by itself, and
the events route still reads it, but the Ask page reconnects itself and
names the last event as `?after=` — Chromium 154 sends no Last-Event-ID
that the node can be shown to receive (München, 2026-11-02), and a header
the page cannot rely on would lose events silently.
Each event names its kind; the page renders `result` and `delta`, and shows
a notice for `stuck`, `moving` and an `end` that is not `done`.

The rule of 2026-09-21 stays — no search runs for nobody (a PubMed search
had run 75 minutes for a caller long gone): a job nobody has listened to
for GRACE_S is cancelled, through the same cancellation token the searches
already honour (app/cancel.py). A reconnect within the grace keeps it.

STUCK is no checkpoint for STUCK_S: every `cancel.checkpoint()` a search
passes is progress (the token keeps when and where), and so is every word
of an answer. A stuck job is SAID — an event the page shows, a warn line in
the web log — never cancelled: a cold model load can take a minute, and
the reader decides.

One web process (the image runs one uvicorn worker), so the jobs are held
in memory; a restart loses them, and a page coming back is told so (404).
Bounded: MAX_JOBS, and a finished job is kept KEEP_S for a page that
reloads, as the page's search is kept for its answer (web.SEARCH_TTL_S).
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cancel

#: a job nobody has listened to for this long is cancelled (the 2026-09-21
#: rule, with room for a reconnect, a phone that wakes, a reload)
GRACE_S = 120.0
#: no progress for this long: the page is told, and the web log says so
STUCK_S = 60.0
#: a finished job is kept this long for a page that comes back
KEEP_S = 600.0
MAX_JOBS = 64
#: how often the reaper looks at the jobs
TICK_S = 1.0

RUNNING, DONE, FAILED, DROPPED = "running", "done", "failed", "dropped"


@dataclass
class Event:
    seq: int
    kind: str
    data: dict


@dataclass
class Job:
    id: str
    kind: str
    owner: str | None
    token: cancel.Token = field(default_factory=cancel.Token)
    created: float = field(default_factory=time.monotonic)
    state: str = RUNNING
    reason: str = ""
    ended: float | None = None
    watchers: int = 0
    unwatched_since: float | None = field(default_factory=time.monotonic)
    stuck: bool = False
    events: list[Event] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, kind: str, **data: Any) -> Event:
        with self.lock:
            ev = Event(len(self.events) + 1, kind, data)
            self.events.append(ev)
            return ev

    def since(self, seq: int) -> list[Event]:
        with self.lock:
            return self.events[max(0, seq):]

    @property
    def last_seq(self) -> int:
        with self.lock:
            return len(self.events)

    def touch(self, where: str) -> None:
        """Progress the job's own code makes outside a checkpoint (a word
        of an answer)."""
        self.token.progress(where)

    def attach(self) -> None:
        with self.lock:
            self.watchers += 1
            self.unwatched_since = None

    def detach(self) -> None:
        with self.lock:
            self.watchers = max(0, self.watchers - 1)
            if self.watchers == 0:
                self.unwatched_since = time.monotonic()

    def status(self) -> dict:
        now = time.monotonic()
        return {"id": self.id, "kind": self.kind, "state": self.state, "reason": self.reason,
                "elapsed_s": round(now - self.created, 1), "where": self.token.where,
                "quiet_s": round(now - self.token.last_progress, 1), "stuck": self.stuck,
                "events": self.last_seq}


_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_reaper: threading.Thread | None = None


def _say(level: str, msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [web {level}:ask] {msg}", flush=True)


def start(kind: str, owner: str | None, fn: Callable[[Job], None], *,
          label: str = "", replaces: list[str] | None = None) -> Job:
    """`fn(job)` in a thread of its own, under the job's cancellation token.
    `fn` emits what the page shows; the end is emitted here, whatever
    happened: `end` with the state (done, failed, dropped) and why.
    `replaces`: this owner's earlier jobs, cancelled first — one question,
    one search, and the model is one queue."""
    for old in replaces or []:
        cancel_job(old, owner, "replaced by a new question")
    job = Job(secrets.token_urlsafe(16), kind, owner)
    with _lock:
        _jobs[job.id] = job
        _evict()
    _ensure_reaper()
    # the question itself is not printed: the query log keeps it, where it belongs
    _say("info", f"relay {job.id[:8]} {kind} started")

    def run() -> None:
        try:
            cancel.run(job.token, fn, job)
            job.state = DONE
        except cancel.Cancelled:
            job.state = DROPPED
            job.reason = job.reason or "cancelled"
        except Exception as e:   # noqa: BLE001 — said to the page and the log
            job.state = FAILED
            job.reason = f"{type(e).__name__}: {e}"[:500]
        job.ended = time.monotonic()
        job.emit("end", state=job.state, reason=job.reason,
                 elapsed_s=round(job.ended - job.created, 1))
        _say("info" if job.state == DONE else "warn",
             f"relay {job.id[:8]} {kind} {job.state} after {job.ended - job.created:.1f} s"
             + (f": {job.reason}" if job.reason else ""))

    threading.Thread(target=run, name=f"relay-{kind}", daemon=True).start()
    return job


def get(job_id: str, owner: str | None) -> Job | None:
    """The job, to its owner only: an id read off another reader's screen
    opens nothing."""
    with _lock:
        job = _jobs.get(job_id)
    return job if job is not None and job.owner == owner else None


def cancel_job(job_id: str, owner: str | None, reason: str) -> bool:
    job = get(job_id, owner)
    if job is None or job.state != RUNNING:
        return False
    job.reason = reason
    job.token.cancel()
    return True


def _evict() -> None:
    """Over MAX_JOBS: the oldest FINISHED jobs go first; a running one is
    never dropped for room. Caller holds _lock."""
    if len(_jobs) <= MAX_JOBS:
        return
    for jid in sorted((j for j in _jobs.values() if j.state != RUNNING), key=lambda j: j.ended or 0):
        if len(_jobs) <= MAX_JOBS:
            break
        _jobs.pop(jid.id, None)


def tick(now: float | None = None) -> None:
    """One look at every job: the grace (a job nobody listens to is
    cancelled), STUCK (said once, and `moving` when it moves again), and the
    finished jobs past KEEP_S forgotten."""
    now = time.monotonic() if now is None else now
    with _lock:
        jobs = list(_jobs.values())
    for job in jobs:
        if job.state != RUNNING:
            if job.ended is not None and now - job.ended >= KEEP_S:
                with _lock:
                    _jobs.pop(job.id, None)
            continue
        with job.lock:
            unwatched = job.unwatched_since
        if unwatched is not None and now - unwatched >= GRACE_S:
            job.reason = f"nobody listened to it for {GRACE_S:.0f} s"
            job.token.cancel()
            continue
        quiet = now - job.token.last_progress
        if not job.stuck and quiet >= STUCK_S:
            job.stuck = True
            job.emit("stuck", where=job.token.where, quiet_s=round(quiet, 1),
                     elapsed_s=round(now - job.created, 1))
            _say("warn", f"relay {job.id[:8]} {job.kind}: no progress for {quiet:.0f} s "
                         f"(last step: {job.token.where})")
        elif job.stuck and quiet < STUCK_S:
            job.stuck = False
            job.emit("moving", where=job.token.where, elapsed_s=round(now - job.created, 1))
            _say("info", f"relay {job.id[:8]} {job.kind}: moving again ({job.token.where})")


def _reap() -> None:
    while True:
        time.sleep(TICK_S)
        try:
            tick()
        except Exception:   # noqa: BLE001 — the reaper never dies of one job
            pass


def _ensure_reaper() -> None:
    global _reaper
    with _lock:
        if _reaper is None or not _reaper.is_alive():
            _reaper = threading.Thread(target=_reap, name="relay-reaper", daemon=True)
            _reaper.start()


def reset() -> None:
    """Every job cancelled and forgotten (tests)."""
    with _lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    for job in jobs:
        job.token.cancel()
