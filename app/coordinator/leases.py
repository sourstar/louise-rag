"""Advisory work leases (architecture-v2.md §7).

    "Leases can be advisory precisely because the work is idempotent: an
     expired-and-reissued lease wastes one GPU-hour and breaks nothing. That
     tolerance is what lets you skip BOINC's hardest machinery."

So this module is deliberately NOT a distributed lock manager. Two producers
handed the same uid range both do the work, both upload, and the second upload
is a no-op on identical content — the only cost is one wasted GPU-hour. Every
guarantee here is best-effort, and the tests assert CONVERGENCE rather than
mutual exclusion, because mutual exclusion is not what is being promised.

Assignment prefers what the network actually wants: §7's demand feed orders
uids so contributors compute the scarce things first, which is also the
mechanism behind "contributors' demand-sets get scheduling priority".
"""
import json
import secrets
import time

from .store import LEASE_TTL_SECONDS


def expire_stale(con) -> int:
    """Mark elapsed open leases expired so their work can be reissued."""
    now = int(time.time())
    cursor = con.execute(
        "UPDATE leases SET state='expired' WHERE state='open' AND expires_at < ?",
        (now,))
    return cursor.rowcount or 0


def _leased_uids(con, space_id: str) -> set[str]:
    now = int(time.time())
    out: set[str] = set()
    for row in con.execute(
            "SELECT uids FROM leases WHERE space_id=? AND state='open' AND expires_at >= ?",
            (space_id, now)):
        try:
            out.update(json.loads(row["uids"] or "[]"))
        except json.JSONDecodeError:
            continue
    return out


def issue(con, *, space_id: str, collection: str, producer: str, count: int,
          candidate_uids: list[str], ttl: int = LEASE_TTL_SECONDS) -> dict:
    """Hand out up to `count` uids that are neither covered nor currently leased.

    Falls back to already-leased uids when nothing else remains, rather than
    returning an empty lease: near the end of a work set that duplication is
    exactly the harmless collision §7 designs for, and it keeps contributors
    busy instead of idling.
    """
    from .store import covered_uids

    expire_stale(con)
    covered = set(covered_uids(con, space_id))
    leased = _leased_uids(con, space_id)

    fresh = [u for u in candidate_uids if u not in covered and u not in leased]
    assigned = fresh[:count]
    duplicated = False
    if not assigned:
        # Everything is spoken for; re-issue rather than idle.
        remainder = [u for u in candidate_uids if u not in covered]
        assigned = remainder[:count]
        duplicated = bool(assigned)

    if not assigned:
        return {"lease_id": None, "uids": [], "reason": "no work available"}

    now = int(time.time())
    lease_id = secrets.token_hex(16)
    con.execute(
        "INSERT INTO leases(lease_id,space_id,collection,uids,producer,issued_at,"
        "expires_at,state) VALUES(?,?,?,?,?,?,?,'open')",
        (lease_id, space_id, collection, json.dumps(assigned), producer, now,
         now + ttl))
    return {"lease_id": lease_id, "space_id": space_id, "collection": collection,
            "uids": assigned, "expires_at": now + ttl, "ttl": ttl,
            "duplicate_assignment": duplicated}


def complete(con, lease_id: str, producer: str) -> bool:
    """Close a lease. Idempotent, and tolerant of a lease that already expired.

    A producer completing an EXPIRED lease is not an error: the work is done and
    valid, the lease merely elapsed. Refusing it would throw away a real GPU-hour
    to enforce bookkeeping nobody depends on.
    """
    row = con.execute("SELECT * FROM leases WHERE lease_id=?", (lease_id,)).fetchone()
    if row is None or row["producer"] != producer:
        return False
    con.execute("UPDATE leases SET state='completed' WHERE lease_id=?", (lease_id,))
    return True


def complete_covered(con, *, space_id: str, producer: str,
                     covered: list[str]) -> int:
    """Close every open lease of `producer` whose uids the accepted bundle
    covers.

    F1-D3: nothing else ever completes a lease. The wire format (correctly)
    carries no lease id — a bundle is content, not workflow — and the old
    /bundles handler keyed dead completion code on that nonexistent field, so
    every fulfilled lease sat "open" until TTL expiry: 24 hours of lease
    listings lying about work that finished in minutes. Matching by coverage
    is server-side, needs no wire change, and is naturally idempotent.
    """
    covered_set = set(covered)
    completed = 0
    for row in con.execute(
            "SELECT lease_id, uids FROM leases WHERE state='open' "
            "AND space_id=? AND producer=?", (space_id, producer)):
        try:
            leased = set(json.loads(row["uids"] or "[]"))
        except json.JSONDecodeError:
            continue
        if leased and leased <= covered_set:
            con.execute("UPDATE leases SET state='completed' WHERE lease_id=?",
                        (row["lease_id"],))
            completed += 1
    return completed


def open_leases(con, space_id: str | None = None) -> list[dict]:
    expire_stale(con)
    if space_id:
        rows = con.execute(
            "SELECT * FROM leases WHERE state='open' AND space_id=? ORDER BY issued_at",
            (space_id,))
    else:
        rows = con.execute("SELECT * FROM leases WHERE state='open' ORDER BY issued_at")
    return [dict(r) for r in rows]
