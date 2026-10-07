"""The bytes the web process sends in answer to peers' pulls (09 §9.9), under
the node's one UP budget (CLAUDE.md invariant 5; 04 §4.3). Migration step 9,
owed since 9c: "the bytes the WEB answers to a pull leave through a process
with no bucket of the worker's and are unmetered until pulls grow with the
fetch" — the fetch is live since 2026-10-07.

Each process builds its own transfer manager, so the web cannot draw from
the worker's buckets. Two halves, then:

  - `pace(n)`, in the web, before an answer leaves: `n` drawn from the web
    process's own UP bucket — the same ceilings, so one answer never goes
    faster than the node may send — and recorded as OWED in the node's state
    database (`state/node.sqlite`, the control key `web_up_owed`);
  - `drain()`, in the worker, every gossip beat: what is owed taken from the
    worker's UP bucket (`take`, which may drive it into debt that refill
    repays), so the worker's own uploads yield to what the web sent and the
    node's long-run rate stays under one ceiling.

Recording never fails an answer: a peer is not refused because a counter
could not be written.
"""
from __future__ import annotations

from contextlib import closing

KEY = "web_up_owed"


def _priority():
    from app.share import gossip
    return gossip.PRIORITY                 # DELTA: the class of the digests and events


def record(n: int) -> None:
    if n <= 0:
        return
    from app import db, node_state
    try:
        with closing(node_state.connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                owed = int(db.get_control(con, KEY) or 0)
                db.set_control(con, KEY, str(owed + n))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
    except Exception:   # noqa: BLE001 — accounting never fails an answer
        pass


def pace(n: int) -> None:
    """The web's half: draw `n` from this process's UP bucket, then record it."""
    if n <= 0:
        return
    from app.transfer.manager import Direction, get_manager
    get_manager().bucket(Direction.UP).acquire(n, _priority())
    record(n)


def drain() -> int:
    """The worker's half: what the web sent since the last beat, charged to
    the worker's UP bucket. Returns the bytes charged."""
    from app import db, node_state
    from app.transfer.manager import Direction, get_manager
    with closing(node_state.connect()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            owed = int(db.get_control(con, KEY) or 0)
            if owed:
                db.set_control(con, KEY, "0")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    if owed > 0:
        get_manager().bucket(Direction.UP).charge(owed, _priority())
    return owed
