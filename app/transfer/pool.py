"""Bounded worker pool for acquisition (architecture-v2.md §5.2's three
parallelism axes).

  across files    — a sliding window of workers over a prioritised queue
  across mirrors  — per-endpoint slots, EWMA-steered (see TransferManager)
  across stages   — download and checksum overlap ACROSS files, because one
                    work item owns a whole file's chain (fetch -> verify ->
                    digest). There is deliberately no second pool fed by a
                    bounded queue: producers blocking on put() while holding
                    every slot, or shutdown(wait=True) joining a worker blocked
                    on an undrained queue, both hang with no pytest-timeout in
                    this repo to catch it.

SLIDING WINDOW, NOT SUBMIT-ALL. If every item were queued up front, "stop
starting new files" would be a no-op on SIGTERM — every file would already be
in flight or queued. Submission is throttled to a small multiple of the worker
count and halts the moment the stop event is set.

RESULTS COME BACK TO THE CALLER'S THREAD. `on_result` is invoked from the
thread that called run_pool, never from a worker, so callers can touch a
SQLite connection (which is thread-affine) while still committing each row as
early as possible rather than batching to the end.
"""
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable, Iterable, Iterator, TypeVar

T = TypeVar("T")
R = TypeVar("R")

_WINDOW_MULTIPLE = 2


def run_pool(items: Iterable[T],
             worker: Callable[[T], R],
             on_result: Callable[[R], None],
             max_workers: int = 4,
             stop: threading.Event | None = None) -> int:
    """Run `worker` over `items`, calling `on_result` on THIS thread for each.

    `worker` must not raise — catch inside it and return a failure record, the
    idiom app/retrieval.py::federated_search already uses. A raising worker
    would make the executor's iterator throw partway through, and every item
    after it would silently lose its result (files on disk with no manifest
    row — a CLAUDE.md invariant-2 break).

    Returns the number of items processed.
    """
    if max_workers <= 1:
        # Serial fallback with no executor at all, matching
        # embeddings._embed_texts()'s convention for a single worker.
        processed = 0
        for item in items:
            if stop is not None and stop.is_set():
                break
            on_result(worker(item))
            processed += 1
        return processed

    pending: Iterator[T] = iter(items)
    processed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures: set = set()

        def _submit_next() -> bool:
            if stop is not None and stop.is_set():
                return False
            try:
                item = next(pending)
            except StopIteration:
                return False
            futures.add(ex.submit(worker, item))
            return True

        for _ in range(max_workers * _WINDOW_MULTIPLE):
            if not _submit_next():
                break

        while futures:
            done, still_running = wait(futures, return_when=FIRST_COMPLETED)
            futures = set(still_running)
            for future in done:
                on_result(future.result())
                processed += 1
                _submit_next()
    return processed


def acquire_endpoint_slot(manager, ranked, spec, stop: threading.Event | None = None):
    """Take a per-endpoint concurrency slot, preferring the fastest mirror that
    has one free right now.

    Returns (key, endpoint, semaphore) or None if the stop event fired. The
    caller MUST release the semaphore in a finally.

    Politeness is never overridden by parallelism: slots come from
    min(global per_endpoint, Endpoint.max_conns, RateLimit.max_conns).
    """
    if not ranked:
        return None
    # First pass: a free slot on the fastest-measured mirror, without blocking.
    for key, endpoint in ranked:
        sem = manager.endpoint_semaphore(key, manager.endpoint_slots(endpoint, spec))
        if sem.acquire(blocking=False):
            return key, endpoint, sem
    # Everything is busy: queue on the preferred mirror, waking periodically so
    # a stop event is noticed rather than blocking indefinitely.
    key, endpoint = ranked[0]
    sem = manager.endpoint_semaphore(key, manager.endpoint_slots(endpoint, spec))
    while True:
        if stop is not None and stop.is_set():
            return None
        if sem.acquire(timeout=0.1):
            return key, endpoint, sem
