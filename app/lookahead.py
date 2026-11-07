"""The NEXT batch of a stage, computed while the pipeline thread writes the
current one: double buffering, with the standard library's
`concurrent.futures.Future` as the hand-over.

WHY (München, 2026-11-07, the text-index stage of wikipedia at ~503
documents a second). py-spy on the pipeline thread put 52.4 % of its time
waiting on the parse pool and 37.6 % + 7.0 % writing the batch's marks and
tags, ONE AFTER THE OTHER: while the thread wrote, the eleven children had
nothing to do, and while they parsed, the thread had nothing to write. Here
the next batch's parse is handed to the pool before the current batch's
index commit and marks, and the thread collects it on its next turn.

WHAT IT DOES NOT CHANGE. The computation is `parallel.compute`, unchanged:
its admission, its node-profile cap, its PARSE_TIMEOUT_S watchdog and its
fall-back to computing in-process all stand, because they run inside it. A
daemon thread only WAITS on it (the pool's children do the work), and it
writes nothing: every writer stays on the pipeline thread
(docs/notes/scheduling.md §3 rule 3). Whoever takes the batch waits for it
with a bound, and a batch not answered in that bound, or answered with an
exception, is abandoned: the caller computes it itself, as it always did.

A DAEMON THREAD, not a `ThreadPoolExecutor`: the interpreter's exit joins
an executor's threads with no bound (as it joins a process pool's manager,
scheduling.md §5 pitfall 19), and a batch nobody will write is not worth
the wait. Abandoned, its result is dropped when it arrives.

THE STANDARD ALTERNATIVE: a bounded queue between a producer thread that
selects and parses and the writer (`queue.Queue(maxsize=1)`), the textbook
pipeline. It would move the SELECTION off the pipeline thread too, and so
onto a second manifest connection that reads rows the writer is about to
mark; one batch of look-ahead keeps the selection on the writer's thread,
where the rows it excludes are known. It pays if more than one batch of
look-ahead is ever wanted.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future


class Ahead:
    """`rows` in their order, each with its result: `known[i]` where it is
    already in hand (carried from the batch before), else computed by
    `compute(the rows still to do)` on a daemon thread, which returns one
    result per row, in order."""

    def __init__(self, rows: Sequence, known: Sequence, compute: Callable[[list], list], *,
                 name: str = "lookahead") -> None:
        if len(rows) != len(known):
            raise ValueError("one known result (or None) per row")
        self.rows = list(rows)
        self._known = list(known)
        self._future: Future = Future()
        todo = [r for r, k in zip(self.rows, self._known) if k is None]
        self.computing = len(todo)
        if not todo:
            self._future.set_result([])
            return
        threading.Thread(target=self._run, args=(compute, todo), name=name, daemon=True).start()

    def _run(self, compute: Callable[[list], list], todo: list) -> None:
        if not self._future.set_running_or_notify_cancel():
            return                                  # abandoned before it began
        try:
            got = compute(todo)
            if len(got) != len(todo):
                raise RuntimeError(f"{len(got)} results for {len(todo)} rows")
            self._future.set_result(got)
        except BaseException as e:                  # noqa: BLE001 — handed to the taker
            self._future.set_exception(e)

    def done(self) -> bool:
        return self._future.done()

    def results(self, timeout: float) -> list | None:
        """One result per row, in order — or None, the batch abandoned, when
        the computation did not answer within `timeout` seconds or raised."""
        try:
            got = iter(self._future.result(timeout=max(0.0, timeout)))
        except Exception:                           # noqa: BLE001 — TimeoutError, or the compute's
            self.abandon()
            return None
        return [k if k is not None else next(got) for k in self._known]

    def abandon(self) -> None:
        """Nobody will take it: not started, it never starts; running, its
        result is dropped when it comes."""
        self._future.cancel()


class Chain:
    """The rows a stage will take next, in their order, as a chain of
    `Ahead`s: each link holds the rows that were ready at one moment — the
    carry of the batch before, the rows a commit upstream just made ready,
    the next rows after the cursor — and starts computing the moment it is
    added. The stage takes whole links from the head.

    WHY (2026-11-07, after #346: München's pool held 5.8-6.5 of its 11
    slots in flight). One `Ahead` per batch, started once the batch before
    was in hand, fed the pool in two ways it could not use:
    - batch k+1 went to the pool only after ALL of batch k's results were
      back, so nothing was queued behind batch k's last parts and the
      children that finished early idled through its tail;
    - while rdf and fts interleave (wikipedia: 11 M documents still to
      extract), the rows the next batch needs do not exist yet when the
      batch before ends — rdf makes them ready later — so the look-ahead
      held only the carry past the chunk cap: the next round wrote that
      carry as a small batch of its own, and the full batch after it was
      parsed while the pipeline thread waited, as before #346.
    A chain feeds both: the next link is added (and computing) while the
    current one is, so its parts queue behind the current one's and fill
    its tail; and a commit upstream adds its rows at once.

    The links are `Ahead`s, unchanged: their computation, its bounds and its
    fall-back are `parallel.compute`'s. Only the stage's own thread adds,
    takes and re-heads a chain (one per manifest, `Held`); the daemon threads
    only compute, and a chain is written from by nobody.

    THE STANDARD ALTERNATIVE: a work queue with a bounded number of tasks in
    flight across batches (`parallel.compute_pages`, an `imap` in the
    standard library's sense), fed by a page source. Its source is pulled
    by the stream itself, so it must have the next page ready the moment it
    is asked: it suits a records build, whose pages exist from the start,
    and not a stage whose next rows appear only as the stage upstream
    commits them. It would also move the selection off the writer's thread
    (the docstring above)."""

    def __init__(self, cursor: int) -> None:
        #: the highest key (a rowid) ever held: the next rows are after it
        self.cursor = cursor
        #: set when a link was abandoned out of the middle: the rows after it
        #: are not all held, so the chain must not be extended from its cursor
        self.broken = False
        self._links: list[Ahead] = []

    def add(self, ahead: Ahead, cursor: int) -> None:
        """A link after the others, its rows the ones after `self.cursor`."""
        self._links.append(ahead)
        self.cursor = max(self.cursor, cursor)

    def prepend(self, ahead: Ahead) -> None:
        """A link before the others: the rows of the batch just cut that it
        did not write, which come first in the order."""
        self._links.insert(0, ahead)

    def head(self):
        """The first row held, or None."""
        for link in self._links:
            if link.rows:
                return link.rows[0]
        return None

    def held(self) -> int:
        """Rows held, computed or computing."""
        return sum(len(link.rows) for link in self._links)

    def computing(self) -> int:
        """Links whose computation has not answered yet: the batches queued
        in the pool at once (a count, for the stage's tests and its log)."""
        return sum(1 for link in self._links if link.computing and not link.done())

    def take(self, want: int, enough: Callable[[list], bool], timeout: float) -> list | None:
        """Whole links from the head, each waited for within ONE bound of
        `timeout` seconds for them all, until `want` rows are in hand or
        `enough(pairs)` says so: [(row, result)], in order. None when the
        first link did not answer within the bound (or raised): the chain is
        then abandoned whole. A later link that did not answer ends the take
        with what came; the rest of the chain is abandoned and it is
        `broken`."""
        deadline = time.monotonic() + max(0.0, timeout)
        out: list = []
        while self._links and len(out) < want and not enough(out):
            link = self._links[0]
            got = link.results(timeout=deadline - time.monotonic())
            if got is None:
                if not out:
                    self.abandon()
                    return None
                self.abandon()
                self.broken = True
                break
            self._links.pop(0)
            out.extend(zip(link.rows, got))
        return out

    def abandon(self) -> None:
        links, self._links = self._links, []
        for link in links:
            link.abandon()


class Held:
    """At most one `Ahead` (or `Chain`) per key — a PHYSICAL identity, the
    manifest's path (scheduling.md §5 pitfall 12), never a collection's
    name."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, Ahead] = {}

    def put(self, key: str, ahead: Ahead) -> None:
        with self._lock:
            old = self._held.pop(key, None)
            self._held[key] = ahead
        if old is not None:
            old.abandon()

    def take(self, key: str) -> Ahead | None:
        with self._lock:
            return self._held.pop(key, None)

    def get(self, key: str):
        """What is held for `key`, left held."""
        with self._lock:
            return self._held.get(key)

    def drop(self, key: str) -> None:
        got = self.take(key)
        if got is not None:
            got.abandon()

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._held)

    def reset(self) -> None:
        with self._lock:
            held, self._held = list(self._held.values()), {}
        for a in held:
            a.abandon()
