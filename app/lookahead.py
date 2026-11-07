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


class Held:
    """At most one `Ahead` per key — a PHYSICAL identity, the manifest's path
    (scheduling.md §5 pitfall 12), never a collection's name."""

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
