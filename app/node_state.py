"""The node's runtime overrides, in `<LOUISE_ROOT>/state/node.sqlite`.

docs/07 §7.8 gives every value exactly one home, and gives the runtime
overrides theirs: "state/  node.sqlite: the catalogue, coverage maps, peers
seen, runtime overrides". They used to live in the control table of
whichever collection sorted first — an accident of sort order, which the
code resolved FOUR ways (docs/notes/STATUS.md, "the control split"):

- the web wrote the operator's pause to `load_all()[0]`;
- the main loop read its FILTERED `all_collections[0]`, boot refusals
  removed, so with the first-sorting collection refused the Library said
  seed-only while the node downloaded;
- the stats beat's seeding gate and the index maintainer read
  `load_all()[0]`, obeying the UI the main loop ignored.

And every change of first collection had to carry the state to the new
owner (`adopt_node_control`, removed), which failed live twice: a wiped
or removed first collection paused the whole node with no log line.

A node-level file has none of that — there is no collection to choose.
The API, the Library and System pages, the main loop, the stats beat,
the seeder and the index maintainer all read the one row here: invariant
10's "the effective value shown anywhere comes from the same resolution
function the worker uses", in the only form that cannot drift.

The keys are `db.NODE_CONTROL_KEYS`, stored in a table with the
manifest control table's shape, so `db.worker_state` and
`db.get_bandwidth_override` decode them unchanged. hold's admission ledger
keeps its own tables in the same file (app/hold/admission.py).

NOT YET §7.8's "expire at restart": worker_state persists across
restarts, as it always has. Changing that changes what a restart does to
a running node, so it is recorded (STATUS.md) rather than done here.
"""
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config, db

_SCHEMA = "CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT)"


def path() -> Path:
    """Beside `collections/` under the one root — the same file
    `admission.connect(<root>/collections)` opens."""
    return config.louise_root() / "state" / "node.sqlite"


def connect() -> sqlite3.Connection:
    """Autocommit like a manifest connection, so `db.set_control` lands
    without a commit; thread-affine like every SQLite handle."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute(_SCHEMA)
    return con


def worker_state(con: sqlite3.Connection | None = None) -> str:
    """THE node state. `con` is a `connect()` a long-lived thread holds;
    without one, a connection is opened for the read and closed."""
    if con is not None:
        return db.worker_state(con)
    with closing(connect()) as c:
        return db.worker_state(c)


def set_worker_state(state: str) -> None:
    with closing(connect()) as c:
        db.set_worker_state(c, state)


def bandwidth_override(con: sqlite3.Connection | None = None) -> float | None:
    if con is not None:
        return db.get_bandwidth_override(con)
    with closing(connect()) as c:
        return db.get_bandwidth_override(c)


def set_bandwidth_override(rate_bps: float | None, ttl_seconds: int) -> None:
    with closing(connect()) as c:
        db.set_bandwidth_override(c, rate_bps, ttl_seconds=ttl_seconds)


@dataclass(frozen=True)
class Move:
    """What `adopt_legacy` did: the keys it moved, and the keys it would not."""
    adopted: dict[str, str]
    #: keys NOT moved because a manifest ahead of every copy could not be
    #: read. This file stays without them — the node reads its 'paused'
    #: default — and every copy is left where it is for the next boot.
    blocked: tuple[str, ...] = ()


def adopt_legacy(node_con: sqlite3.Connection, legacy_cons: list) -> Move:
    """Move node control out of the collection manifests, once per key.

    A key this file already holds is kept. Otherwise the first manifest in
    `legacy_cons` that has it gives its value — the caller passes them in
    the order the WEB wrote to (the unfiltered first collection first), so
    what moves is the operator's last click, not a sibling's stale copy.
    Then the keys are DELETED from every manifest given: one home (§7.8),
    and nothing can ever read a stale copy back.

    FAIL CLOSED. `None` in `legacy_cons` stands for a manifest that could
    not be opened; one that raises on read counts the same. If such a
    manifest comes AHEAD of every copy of a key, it may hold the operator's
    last click, and taking a later sibling's copy instead would resume a
    node the operator paused — reproduced by the review of this change: a
    manifest locked once at boot, a stale sibling adopted, and on the next
    boot the operator's value deleted. So that key is not moved, NOTHING is
    deleted anywhere, and the node reads 'paused' — stopping is the safe
    direction — until a boot that reads every manifest, or a click in the
    Library, which writes here and wins.

    Copy-if-absent runs under BEGIN IMMEDIATE (a read-then-write; the web
    may set the state concurrently, and its value must win). The deletes
    follow the commit, so an interruption leaves copies the next call
    deletes — never a key held nowhere.

    Known edge, accepted: roll back to an image that predates this file,
    change the state there, roll forward — the value this file kept wins.
    """
    adopted: dict[str, str] = {}
    blocked: list[str] = []
    node_con.execute("BEGIN IMMEDIATE")
    try:
        for key in db.NODE_CONTROL_KEYS:
            if db.get_control(node_con, key) is not None:
                continue
            for con in legacy_cons:
                try:
                    if con is None:
                        raise sqlite3.OperationalError("manifest could not be opened")
                    value = db.get_control(con, key)
                except sqlite3.Error:
                    blocked.append(key)
                    break
                if value is not None:
                    db.set_control(node_con, key, value)
                    adopted[key] = value
                    break
        node_con.execute("COMMIT")
    except BaseException:
        node_con.execute("ROLLBACK")
        raise
    if blocked:
        return Move(adopted, tuple(blocked))
    keys = db.NODE_CONTROL_KEYS
    for con in legacy_cons:
        if con is None:
            continue
        try:
            con.execute(f"DELETE FROM control WHERE key IN ({','.join('?' * len(keys))})",
                        keys)
        except sqlite3.Error:
            pass    # an inert copy: nothing reads it, and the next boot retries
    return Move(adopted)
