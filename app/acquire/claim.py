"""Advisory single-writer claim for acquisition.

`louise hold records fetch` and the background worker can both call
run_acquisition on the same collection with nothing preventing overlap. Two
processes would then race on the same `.part` files and on os.replace, and
both would burn origin bandwidth fetching identical bytes — the opposite of
the etiquette the transfer manager exists to enforce.

The race pre-dates P0.7, but P0.7 widens it: the delta gate is now evaluated
once up front rather than immediately before each file, so the overlap window
grows from "the same file happened to be next in both processes" to "the whole
pending list".

ADVISORY, not a lock. It lives in the existing `control` table (Tier 3:
runtime state, ephemeral, never desired state — CLAUDE.md invariant 10), holds
pid + timestamp, and goes stale on its own so a hard-killed process cannot
wedge acquisition forever. A stale claim is simply taken over. This is the same
posture as the coordinator leases in architecture-v2.md §7: collisions are made
harmless rather than impossible.
"""
import json
import os
import time
from contextlib import contextmanager

from .. import db

CLAIM_KEY = "acquire_claim"
# Generous relative to a fetch loop iteration, short enough that a killed
# process never blocks the next run for long.
STALE_AFTER_SECONDS = 300
_REFRESH_EVERY = 60


class AcquisitionBusy(RuntimeError):
    """Another live process is already acquiring for this collection."""


def read_claim(con) -> dict | None:
    raw = db.get_control(con, CLAIM_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _is_live(claim: dict | None, now: float) -> bool:
    if not claim:
        return False
    if now - float(claim.get("ts", 0)) > STALE_AFTER_SECONDS:
        return False          # stale: the holder died or hung; take it over
    return claim.get("pid") != os.getpid()


@contextmanager
def acquisition_claim(con, source_id: str, force: bool = False):
    """Claim acquisition for `source_id`, or raise AcquisitionBusy.

    Released on the way out even if the body raises, so a crash inside
    run_acquisition does not leave the claim held for the full stale window.
    """
    now = time.time()
    existing = read_claim(con)
    if not force and _is_live(existing, now):
        held_for = int(now - float(existing.get("ts", now)))
        raise AcquisitionBusy(
            f"{source_id}: acquisition already running in pid {existing.get('pid')} "
            f"(claimed {held_for}s ago). Wait for it to finish, or re-run once the "
            f"claim goes stale after {STALE_AFTER_SECONDS}s.")
    db.set_control(con, CLAIM_KEY,
                    json.dumps({"pid": os.getpid(), "ts": now, "source_id": source_id}))
    try:
        yield
    finally:
        current = read_claim(con)
        if current and current.get("pid") == os.getpid():
            db.set_control(con, CLAIM_KEY, "")


def refresh_claim(con, source_id: str, last_refresh: float) -> float:
    """Keep a long-running acquisition's claim from going stale under it.
    Returns the (possibly unchanged) last-refresh timestamp."""
    now = time.time()
    if now - last_refresh < _REFRESH_EVERY:
        return last_refresh
    current = read_claim(con)
    if current and current.get("pid") == os.getpid():
        db.set_control(con, CLAIM_KEY,
                        json.dumps({"pid": os.getpid(), "ts": now, "source_id": source_id}))
    return now
