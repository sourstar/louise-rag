"""`hold records fetch <collection> [--profile P] [--dry-run]` (07 §7.3):
the acquisition spec run for a profile (06 §6.2), drawing from the peer's
one budget (04 §4.3). Was `corpus add` / `corpus grow` (07 §7.6), carried
over from app/cli.py whole: the same engine calls, the same stop, the same
output.

The node's worker fetches on its own; this is the by-hand door, and the
Library's grow dialog shows its dry run as its command line
(tests/acquire/test_web_shared_cli_path.py: the same `plan_acquisition`).
Concurrent with the worker it is safe: files are claimed
(app/acquire/claim.py).
"""
from __future__ import annotations

import signal
import sys
import threading

from app.acquire import engine
from app.acquire.spec import DiskPreflightError, LicenseAckRequiredError
from app.acquire.specs import ACQUIRE_REGISTRY
from app.transfer.manager import get_manager

#: Set by a stop signal, threaded into run_acquisition and observed by the
#: workers parked in the bandwidth bucket (they are woken to see it).
_stop = threading.Event()


def _handle_sigterm(signum, frame) -> None:
    _stop.set()
    try:
        get_manager().wake_all()
    except Exception:                                   # noqa: BLE001
        pass


#: What ends the command the graceful way. SIGHUP too: rsync leads its own
#: session (app/acquire/transport_rsync.py), so a terminal that goes away —
#: a dropped ssh, a `docker exec -it` — no longer takes rsync with it, and
#: a hangup left at its default killed the command and left rsync writing
#: its .part with no lease and nobody to stop it.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def route_stop_signals(handler=_handle_sigterm) -> None:
    for sig in STOP_SIGNALS:
        signal.signal(sig, handler)


def hangup_is_ctrl_c() -> None:
    """Before any stop is bound (a dry run lists without one): a hangup
    arrives as the KeyboardInterrupt the transport cleans up after — it
    kills rsync's group before letting it past — not as the default that
    kills this process and strands rsync."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGHUP, signal.default_int_handler)


def print_plan(plan, profile: str, name: str) -> None:
    print(f"profile: {profile}")
    print(f"files:   {plan.files_pending} pending / {plan.files_total} total")
    print(f"bytes:   {plan.bytes_pending} pending / {plan.bytes_total} total")
    print(f"eta:     ~{plan.eta_seconds:.0f}s (placeholder rate; no live bandwidth "
          f"measurement until the transfer manager lands)")
    blocked = (f" — BLOCKS acquisition: louise hold collection ack {name}"
               if plan.license_state == "missing" else "")
    print(f"license: {plan.license_state}{blocked}")


def fetch(collection, con, profile: str, *, dry_run: bool = False) -> int:
    """07 §7.7's exit: 0 done, 1 a file failed, 2 refused (the licence, the
    disk), 130 stopped by a signal after the unit in flight committed."""
    spec = ACQUIRE_REGISTRY.get(collection.plugin)
    if spec is None:
        print(f"louise: {collection.name}: no acquisition spec is registered for "
              f"plugin {collection.plugin!r}", file=sys.stderr)
        return 2
    if dry_run:
        hangup_is_ctrl_c()
        print_plan(engine.plan_acquisition(con, collection, spec, profile), profile,
                   collection.name)
        return 0

    route_stop_signals()
    try:
        result = engine.run_acquisition(con, collection, spec, profile, stop=_stop)
    except (LicenseAckRequiredError, DiskPreflightError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    aborted = result.get("aborted", 0)
    adopted = result.get("adopted", 0)
    print(f"fetched={result['fetched']} skipped={result['skipped']} "
          f"failed={result['failed']} aborted={aborted} total={result['total']}")
    if adopted:
        # "fetched" counts these, and an operator watching a 200 GB profile
        # finish in seconds deserves to know why
        print(f"  ({adopted} of them already on disk: verified and recorded, "
              f"not downloaded)")
    if result.get("stopped"):
        # stopped before any transfer — most often inside the listing, which
        # is ~12 minutes on gutenberg. Said outright: "total=0" alone reads
        # as an origin with no files.
        print(f"interrupted before any transfer ({result['stopped']}) — nothing "
              f"was recorded; re-run to resume")
    elif aborted:
        print("interrupted — partial downloads were checkpointed; re-run to resume")
    # 07 §7.7 and invariant 2: a signal finishes the unit, commits, and
    # exits 130. Only genuine failures are 1.
    if _stop.is_set():
        return 130
    return 1 if result["failed"] else 0
