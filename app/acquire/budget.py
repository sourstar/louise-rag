"""The acquire-layer facade over the transfer manager (app/transfer/).

CLAUDE.md invariant 5: no code path moves bytes without drawing from the
central token-bucket budget. Every streaming transport calls acquire_bytes()
once per chunk; rsync — a subprocess we cannot meter per byte — takes a rate
lease instead. Keeping this module's signatures unchanged is what let P0.7 be
a swap-in rather than a transport rewrite.

WHY acquire_bytes IS CALLED AFTER THE WRITE, not before: transports write,
flush and fsync the chunk, and only then charge for it. That ordering is what
makes the abort path truthful — at the moment TransferAborted is raised, every
counted byte is already durable in the .part file and part.stat().st_size is
exactly what the manifest will record. The cost is that accounting is
pay-after, so the budget guarantee is a long-run average rather than an
instantaneous ceiling (the first chunk of each in-flight file is unmetered, and
kernel socket buffers are invisible to any userspace shaper).
"""
from contextlib import contextmanager

from ..transfer.bucket import Priority, TransferAborted as _BucketAborted
from ..transfer.manager import Direction, current_handle, get_manager
from .spec import TransferAborted


def acquire_bytes(n: int) -> None:
    """Draw `n` bytes from the shared budget, blocking as required.

    Raises spec.TransferAborted if the bound stop event fires while waiting —
    on SIGTERM most threads are parked *here*, inside the bucket, rather than
    in a socket read, so this is where a shutdown actually becomes prompt.

    With no platform.toml on disk the budget is unlimited and this returns
    immediately after bumping a counter, so an existing deployment sees no
    behavior change.
    """
    if n <= 0:
        return
    manager = get_manager()
    manager.maybe_refresh()          # picks up a schedule window flip
    handle = current_handle()
    if handle is None:
        # Unbound caller (e.g. a synchronous CLI fetch outside the pool).
        # BACKFILL explicitly: Priority's zero value is INTERACTIVE, so a
        # falsy default would silently hand unbound callers the top class.
        bucket = manager.bucket(Direction.DOWN)
        priority, stop = Priority.BACKFILL, None
    else:
        bucket, priority, stop = handle.bucket, handle.priority, handle.stop
    try:
        bucket.acquire(n, priority, stop=stop)
    except _BucketAborted as e:
        # Translate the transfer-layer exception into the acquire layer's own
        # hierarchy, so engine's `except (TransportError, OSError)` catches it.
        # The transfer package stays independent of app/acquire.
        raise TransferAborted(str(e)) from e


@contextmanager
def rsync_reservation(endpoint_key: str | None = None):
    """Yield the --bwlimit value in KiB/s for one rsync invocation, or None
    when the budget is unlimited (then no flag is passed at all).

    rsync's --bwlimit is per-process, integer KiB/s and fixed at process start,
    and we cannot observe the subprocess's byte flow — so instead of metering
    it we withdraw a slice of the bucket's refill RATE for the process's
    lifetime and hand it that slice as a ceiling. Sized from measured
    throughput (see TransferManager.rsync_lease) so a slow mirror does not
    cause us to withhold and waste the rest of the budget.

    APPROXIMATION AND ITS BOUND (architecture-v2.md §5.2 calls this out as
    approximate; the task requires stating the bound rather than accepting
    silent drift):
      - Overshoot is limited to rsync's own protocol overhead: --bwlimit paces
        payload bytes, not wire bytes.
      - Integer KiB/s flooring and a stale concurrency estimate both cause
        UNDER-use, never over-use.
      - The one genuine over-budget window is a schedule step-down that lands
        mid-file, bounded by that file's remaining duration, because a running
        rsync's limit cannot be changed. The lease is recomputed per file, so
        it self-corrects within one file.

    --bwlimit=0 means UNLIMITED to rsync, so the floor here is 1 KiB/s: a
    rounding artifact must never silently uncap the one transport we cannot
    otherwise meter.
    """
    with get_manager().rsync_lease(endpoint_key) as bps:
        yield None if bps is None else max(1, int(bps // 1024))
