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

The mode SURVIVES a restart — the one runtime override that does, by
operator decision (2026-09-13; docs/07 §7.8, CLAUDE.md invariant 10). No
file sets it, so there is nothing to reset it to, and an expiry would turn
a node held at acquire-only back into a running one, encoder and all, on
every deploy. Do not "fix" it.
"""
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import config, db

_SCHEMA = "CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT)"
#: ENCODER LEASES (Perugia, 2026-10-19): a process embedding documents with
#: a runner's model that the worker does not run itself — `share contribute`,
#: `hold vectors build` — says so here, renewed per batch, so the worker's
#: sweep of idle embedders (worker._sweep_unheld_embedders) leaves the model
#: alone. The sweep judged "idle" by the backend's CPU share, which a
#: GPU-bound backend shows as 0.0 one sample in four while embedding
#: non-stop: seven shutdowns in 3.4 h, a 19 s cold start each. A lease
#: expires on its own, so a killed process frees the model within the TTL.
_LEASES = ("CREATE TABLE IF NOT EXISTS encoder_leases (server TEXT NOT NULL, model TEXT NOT NULL, "
           "holder TEXT NOT NULL, until REAL NOT NULL, PRIMARY KEY (server, model, holder))")
#: how long a lease outlives its last renewal: longer than any one batch
#: (the largest book is hours of them, but a batch is seconds), shorter
#: than the idle unload is worth waiting for
ENCODER_LEASE_TTL_S = 600.0

# ---- what each mode runs -------------------------------------------------------
#
# THE ONE TABLE (operator, 2026-09-19: "I want to redefine the behavior"). A
# mode is a statement of what the node does, and the Library's four buttons
# are these four rows and nothing else:
#
#     pause                   nothing runs — no fetching, parsing, seeding,
#                             indexing
#     seeding only            seeding only
#     download/seeding only   download and seeding only
#     run everything          everything
#
# EMBEDDING IS NOT A MODE'S BUSINESS beyond this: it runs only where
# everything runs, and THERE it is set per collection by its own drop-down
# (`embed` in the collection's file: off, on-demand, all). No mode changes
# that setting and no mode is changed by it.
#
# Before this, "download/seeding only" also SCANNED what it downloaded into
# the manifest and kept the vector-index maintainer running — parsing and
# indexing under a button that named neither — and a scan already running
# ignored a pause until it ended (1,083 s on wikivec). Every gate in the
# worker, the seeder, `louise node health` and the Library now reads this table,
# so they cannot come to disagree about what a mode means.
FETCH, SEED, PARSE, INDEX, EMBED = "fetch", "seed", "parse", "index", "embed"

#: PARSE is reading what was fetched: the discovery scan, rdf, the parse pool.
#: INDEX is the text index (fts), the vector-index maintainer, and taking in
#: vectors others computed (archive and bundle imports). EMBED is this node's
#: own encoder, and only ever beside the other two.
MODE_RUNS: dict[str, frozenset[str]] = {
    "paused": frozenset(),
    "seed-only": frozenset({SEED}),
    "acquire-only": frozenset({FETCH, SEED}),
    "running": frozenset({FETCH, SEED, PARSE, INDEX, EMBED}),
}

#: the Library's words for the four, in the order it shows them
MODE_LABELS = (("running", "run everything"),
               ("acquire-only", "download/seeding only"),
               ("seed-only", "seeding only"),
               ("paused", "pause"))

MODE_SAYS = {
    "running": "everything runs: fetching, seeding, parsing, indexing — and "
               "embedding as each collection's own embed setting says",
    "acquire-only": "download and seeding only: nothing is parsed, indexed or "
                    "embedded; what lands is scanned when everything runs again",
    "seed-only": "seeding only: nothing is fetched, parsed, indexed or embedded",
    "paused": "nothing runs: no fetching, no parsing, no seeding, no indexing",
}


def runs(state: str, activity: str) -> bool:
    """Does the node, in this mode, do this? A mode this does not know runs
    nothing — the reading of an unreadable state everywhere else too."""
    return activity in MODE_RUNS.get(state, frozenset())


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
    db.wal(con)                        # two openers of a fresh file: the loser waits
    con.execute("PRAGMA busy_timeout=60000")
    con.execute(_SCHEMA)
    con.execute(_LEASES)
    return con


def lease_encoder(server: str, model: str, holder: str, *, ttl_s: float = ENCODER_LEASE_TTL_S,
                  con: sqlite3.Connection | None = None, now: float | None = None) -> None:
    """`holder` is using `model` on `server` now; the sweep leaves it alone
    until `now + ttl_s` unless renewed. One row per (server, model, holder),
    replaced on renewal — a write per batch, never a growing table."""
    until = (time.time() if now is None else now) + ttl_s
    row = (server, model, holder, until)
    if con is not None:
        con.execute("INSERT OR REPLACE INTO encoder_leases (server, model, holder, until) "
                    "VALUES (?, ?, ?, ?)", row)
        return
    with closing(connect()) as c:
        c.execute("INSERT OR REPLACE INTO encoder_leases (server, model, holder, until) "
                  "VALUES (?, ?, ?, ?)", row)


def release_encoder(holder: str, *, con: sqlite3.Connection | None = None) -> None:
    """Every lease `holder` holds goes: the command ended."""
    if con is not None:
        con.execute("DELETE FROM encoder_leases WHERE holder=?", (holder,))
        return
    with closing(connect()) as c:
        c.execute("DELETE FROM encoder_leases WHERE holder=?", (holder,))


def leased_encoders(con: sqlite3.Connection | None = None,
                    now: float | None = None) -> set[tuple[str, str]]:
    """The (server, model) pairs some process is embedding with now;
    expired leases are dropped on the way. The table holds a handful of
    rows at most, read once a settle beat."""
    now = time.time() if now is None else now

    def read(c):
        c.execute("DELETE FROM encoder_leases WHERE until <= ?", (now,))
        return {(r["server"], r["model"]) for r in
                c.execute("SELECT server, model FROM encoder_leases WHERE until > ?", (now,))}
    if con is not None:
        return read(con)
    with closing(connect()) as c:
        return read(c)


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


def peek_worker_state() -> str:
    """THE node state, for a reader that must create NOTHING — `louise
    health` is a check, and a check that made the directory, the file or
    the table would change what it reports on. Read-only; an absent file
    reads as the worker reads it ('paused', db.worker_state's default),
    and a file that cannot be read says 'unknown' rather than guessing.

    NOT EVEN SQLITE'S SIDECARS (review, 2026-09-13, reproduced): `mode=ro`
    alone still creates `-wal` and `-shm` beside a closed WAL database, so
    a store with neither is opened `immutable=1` — there is no WAL content
    to miss (a write racing the check can be missed, and the next look sees
    it). The URI comes from `as_uri()`: a raw path with `#` or `?` in it
    cut the URI short, dropped `mode=ro`, and CREATED a stray database. And
    a file hold's admission ledger made before any worker booted has no
    `control` table yet — it reads 'paused', as the worker would read it,
    not 'unknown'."""
    return peek(db.worker_state, "paused", error="unknown")


def peek(read, absent, *, error=None, table: str = "control"):
    """`read(con)` over the node's store opened READ-ONLY, creating nothing
    (see `peek_worker_state`): `absent` when there is no store or no
    `table` yet, `error` when it cannot be read."""
    p = path()
    if not p.is_file():
        return absent
    # beside the file SQLite actually opens: a symlinked store keeps its
    # sidecars next to the TARGET, and checking beside the link opened a
    # live WAL immutable and read a stale mode (verification pass, reproduced)
    real = p.resolve()
    closed = not any(real.with_name(real.name + s).exists() for s in ("-wal", "-shm"))
    uri = real.as_uri() + ("?mode=ro&immutable=1" if closed else "?mode=ro")
    try:
        with closing(sqlite3.connect(uri, uri=True)) as c:
            c.row_factory = sqlite3.Row
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name=?", (table,)).fetchone() is None:
                return absent
            return read(c)
    except sqlite3.Error:
        return error


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


# ---- one collection paused (07 §7.3 `node pause <collection>`, §7.7) --------
#: control key: the collections the operator paused one by one — the worker
#: releases each one's write lock at the end of its current unit and runs
#: nothing for it until `node resume <collection>`. Runtime state like the
#: mode; it survives a restart for the same reason (the mode's exception).
PAUSED_KEY = "paused_collections"


def paused_collections(con: sqlite3.Connection | None = None) -> set[str]:
    import json

    def read(c) -> set[str]:
        raw = db.get_control(c, PAUSED_KEY, "[]")
        try:
            names = json.loads(raw)
        except (TypeError, ValueError):
            return set()
        return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()
    if con is not None:
        return read(con)
    with closing(connect()) as c:
        return read(c)


def set_paused(name: str, paused: bool) -> set[str]:
    """Pause or resume one collection; the set as it is after."""
    import json
    with closing(connect()) as c:
        names = paused_collections(c)
        (names.add if paused else names.discard)(name)
        db.set_control(c, PAUSED_KEY, json.dumps(sorted(names)))
        return names
