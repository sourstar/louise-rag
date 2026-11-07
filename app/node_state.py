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
#: ENCODER USE (Perugia, 2026-10-19, the same night): when a model last
#: answered an embedding request from ANY process on this node — the
#: worker's, the web's search, a command's. The sweep judged "idle" by
#: LocalAI's `/system` cpu_percent, which is the VeGa WRAPPER's process
#: (0.07–2 %, never exactly 0.0) and not the llama.cpp child's — so on
#: Perugia an idle encoder whose HIP runtime busy-polls 2.8 cores was
#: never unloaded, and a busy one read idle. The embeddings client writes
#: one row per (server, model) after a request (rate-limited); the sweep
#: unloads what nobody has asked for IDLE_BEATS beats and nobody leases.
_USE = ("CREATE TABLE IF NOT EXISTS encoder_use (server TEXT NOT NULL, model TEXT NOT NULL, "
        "last_used REAL NOT NULL, PRIMARY KEY (server, model))")

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


def mode_runs(state: str, activity: str) -> bool:
    """Does this MODE, by the table alone, run this? A mode this does not
    know runs nothing — the reading of an unreadable state everywhere else."""
    return activity in MODE_RUNS.get(state, frozenset())


def profile_runs(activity: str) -> bool:
    """Does the node PROFILE in force let this run? Its `mode`
    (app/node_profile.py; operator 2026-11-02: "suspend processing during
    the quiet time frames"), read through app/cpu_limit.py's cache — one file
    read per 30 s or at the schedule's own boundary, the last good one kept
    when the file does not load. No profile, or one in `running`, holds
    nothing."""
    from . import cpu_limit
    lim = cpu_limit.current()
    return lim.mode is None or mode_runs(lim.mode, activity)


def runs(state: str, activity: str) -> bool:
    """Does the node, in this mode and in the profile in force now, do this?
    The STRICTER of the two wins (node-profiles-design.md decision 6): the
    activity runs only where both the node's mode and the profile's mode run
    it. Every gate asks here, so the worker, the seeder, `node health`, the
    jobs and the Library cannot disagree about what runs."""
    return mode_runs(state, activity) and profile_runs(activity)


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
    con.execute(_USE)
    return con


def touch_encoder(server: str, model: str, *, con: sqlite3.Connection | None = None,
                  now: float | None = None) -> None:
    """`model` on `server` answered an embedding request now."""
    row = (server, model, time.time() if now is None else now)
    sql = "INSERT OR REPLACE INTO encoder_use (server, model, last_used) VALUES (?, ?, ?)"
    if con is not None:
        con.execute(sql, row)
        return
    with closing(connect()) as c:
        c.execute(sql, row)


def encoder_last_used(con: sqlite3.Connection | None = None) -> dict[tuple[str, str], float]:
    """When each (server, model) last answered a request, from every process
    of this node; absent = never since the row was first kept."""
    def read(c):
        return {(r["server"], r["model"]): float(r["last_used"]) for r in
                c.execute("SELECT server, model, last_used FROM encoder_use")}
    if con is not None:
        return read(con)
    with closing(connect()) as c:
        return read(c)


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


#: the last mode writes, newest last: when, what, and who (2026-10-23: the
#: node set seed-only at 12:25 read "running" again by 12:34, and nothing
#: said who had written it — the web's log went with its container)
MODE_HISTORY_KEY = "worker_state_history"
MODE_HISTORY_KEEP = 50


def set_worker_state(state: str, *, by: str = "") -> None:
    """THE node mode, written, and the write recorded with `by` — the
    command or the route and its caller."""
    import json
    import time
    with closing(connect()) as c:
        db.set_worker_state(c, state)
        try:
            hist = json.loads(db.get_control(c, MODE_HISTORY_KEY) or "[]")
        except ValueError:
            hist = []
        hist = (hist + [{"at": time.time(), "state": state, "by": by or "unnamed"}])[-MODE_HISTORY_KEEP:]
        db.set_control(c, MODE_HISTORY_KEY, json.dumps(hist))


def mode_history() -> list[dict]:
    """The recorded mode writes, oldest first; [] when none."""
    import json
    try:
        with closing(connect()) as c:
            return json.loads(db.get_control(c, MODE_HISTORY_KEY) or "[]")
    except (ValueError, sqlite3.Error):
        return []


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


#: `louise node stop` (07 §7.3): the worker finishes its current units,
#: commits and exits 0 — where a signal exits 130 (§7.7). Through the
#: control table, as the digest request is: the CLI and the worker are two
#: processes, often two containers, and a pid does not cross a container.
STOP_REQUEST_KEY = "stop_requested"


def request_stop() -> None:
    with closing(connect()) as c:
        db.set_control(c, STOP_REQUEST_KEY, "1")
        c.commit()


def take_stop_request() -> bool:
    """True once per request: read and cleared together. An unreadable
    store is no request — the worker keeps running, a signal still stops it."""
    try:
        with closing(connect()) as c:
            if db.get_control(c, STOP_REQUEST_KEY) != "1":
                return False
            db.set_control(c, STOP_REQUEST_KEY, "")
            c.commit()
            return True
    except sqlite3.Error:
        return False


#: 04 §4.5 (amended 2026-10-20): a collection's embed mode or sharing
#: switched → the next digest goes out at once, so the parts this peer led
#: are released (or claimed) without waiting the hour. The web sets it, the
#: worker's gossip beat takes it: through the control table, as the
#: bandwidth override is, because they are two processes.
DIGEST_REQUEST_KEY = "digest_requested"


def request_digest(con: sqlite3.Connection | None = None) -> None:
    if con is not None:
        db.set_control(con, DIGEST_REQUEST_KEY, "1")
        return
    with closing(connect()) as c:
        db.set_control(c, DIGEST_REQUEST_KEY, "1")
        c.commit()


def take_digest_request(con: sqlite3.Connection | None = None) -> bool:
    """True once per request: read and cleared together."""
    def take(c) -> bool:
        if db.get_control(c, DIGEST_REQUEST_KEY) != "1":
            return False
        db.set_control(c, DIGEST_REQUEST_KEY, "")
        return True
    if con is not None:
        return take(con)
    try:
        with closing(connect()) as c:
            got = take(c)
            c.commit()
            return got
    except sqlite3.Error:
        return False


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


#: every collection pause and resume, newest last: when, which, and who
#: (2026-10-31: normattiva was found paused, and neither the operator nor
#: the node session had paused it; nothing recorded who had)
PAUSED_HISTORY_KEY = "paused_collections_history"
PAUSED_HISTORY_KEEP = 200


def invoker() -> str:
    """Who is running this command, for a history entry: the parent
    process's program and script, by their names only (never their
    arguments, which could carry anything), and the user."""
    import getpass
    import os
    parts = []
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as f:
            argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        names = [os.path.basename(argv[0])] if argv else []
        names += [os.path.basename(a) for a in argv[1:3] if a.endswith((".sh", ".py"))]
        if names:
            parts.append("from " + " ".join(names))
    except OSError:
        pass
    try:
        parts.append(f"user {getpass.getuser()}")
    except Exception:   # noqa: BLE001 — no user name to give
        pass
    return ", ".join(parts)


def set_paused(name: str, paused: bool, *, by: str = "") -> set[str]:
    """Pause or resume one collection; the set as it is after. The write is
    recorded with `by` — the command or the route and its caller."""
    import json
    import time
    with closing(connect()) as c:
        names = paused_collections(c)
        (names.add if paused else names.discard)(name)
        db.set_control(c, PAUSED_KEY, json.dumps(sorted(names)))
        try:
            hist = json.loads(db.get_control(c, PAUSED_HISTORY_KEY) or "[]")
        except ValueError:
            hist = []
        hist = (hist + [{"at": time.time(), "collection": name, "paused": bool(paused),
                         "by": by or "unnamed"}])[-PAUSED_HISTORY_KEEP:]
        db.set_control(c, PAUSED_HISTORY_KEY, json.dumps(hist))
        return names


def pause_history() -> list[dict]:
    """The recorded collection pauses and resumes, oldest first; [] when none."""
    import json
    try:
        with closing(connect()) as c:
            return json.loads(db.get_control(c, PAUSED_HISTORY_KEY) or "[]")
    except (ValueError, sqlite3.Error):
        return []


# ---- one collection frozen (07 §7.3 `node freeze <collection>`) --------------
#
# Operator, 2026-11-07: "no updated are fetched and it stays as it is (you
# can still embed locally) … but you can still share … the parts that are
# still aligned with the non frozen state" (docs/notes/collection-freeze.md).
# A frozen collection is RUN — parsed, indexed, embedded, searched, checked —
# on the version it holds; what stops is its updates: the origin fetch, the
# scan of its origin, and every intake from peers. Not a pause (that runs
# nothing), not a daughter (no new collection_id, 02 §2.7).
#
# Runtime state like the pause, and it survives a restart for the same
# reason (invariant 10's exception, 07 §7.8): no file sets it, so there is
# nothing to reset it to, and an expiry would let a restart fetch the very
# updates the operator froze out.
FROZEN_KEY = "frozen_collections"
#: every freeze and unfreeze, newest last: when, which, and who
FROZEN_HISTORY_KEY = "frozen_collections_history"
FROZEN_HISTORY_KEEP = 200


def _names(c: sqlite3.Connection, key: str) -> set[str]:
    import json
    raw = db.get_control(c, key, "[]")
    try:
        names = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    return {n for n in names if isinstance(n, str)} if isinstance(names, list) else set()


def frozen_collections(con: sqlite3.Connection | None = None) -> set[str]:
    """The collections frozen now. `con` is a `connect()` the caller holds;
    without one a connection is opened for the read and closed."""
    if con is not None:
        return _names(con, FROZEN_KEY)
    with closing(connect()) as c:
        return _names(c, FROZEN_KEY)


def set_frozen(name: str, frozen: bool, *, by: str = "") -> set[str]:
    """Freeze or unfreeze one collection; the set as it is after. The write
    is recorded with `by` — the command or the route and its caller."""
    import json
    with closing(connect()) as c:
        c.execute("BEGIN IMMEDIATE")           # a read, then a write: never a lost update
        try:
            names = _names(c, FROZEN_KEY)
            (names.add if frozen else names.discard)(name)
            db.set_control(c, FROZEN_KEY, json.dumps(sorted(names)))
            try:
                hist = json.loads(db.get_control(c, FROZEN_HISTORY_KEY) or "[]")
            except ValueError:
                hist = []
            if not isinstance(hist, list):
                hist = []
            hist = (hist + [{"at": time.time(), "collection": name, "frozen": bool(frozen),
                             "by": by or "unnamed"}])[-FROZEN_HISTORY_KEEP:]
            db.set_control(c, FROZEN_HISTORY_KEY, json.dumps(hist))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return names


def freeze_history() -> list[dict]:
    """The recorded freezes and unfreezes, oldest first; [] when none."""
    import json
    try:
        with closing(connect()) as c:
            got = json.loads(db.get_control(c, FROZEN_HISTORY_KEY) or "[]")
            return got if isinstance(got, list) else []
    except (ValueError, sqlite3.Error):
        return []


def _freeze_entry(c: sqlite3.Connection, name: str) -> dict | None:
    """Collection `name`'s freeze — `{"by", "at", "when"}` from the latest
    history entry for it, each None where nothing recorded it — or None
    when it is not frozen."""
    import json
    if name not in _names(c, FROZEN_KEY):
        return None
    try:
        hist = json.loads(db.get_control(c, FROZEN_HISTORY_KEY) or "[]")
    except (TypeError, ValueError):
        hist = []
    latest = next((h for h in reversed(hist if isinstance(hist, list) else [])
                   if isinstance(h, dict) and h.get("collection") == name), None)
    if latest is None or not latest.get("frozen"):
        return {"by": None, "at": None, "when": None}     # the history cut short
    return {"by": latest.get("by") or None, "at": latest.get("at"), "when": _when(latest.get("at"))}


def frozen_since(name: str, con: sqlite3.Connection | None = None) -> dict | None:
    """Who froze collection `name` and when — `{"by", "at", "when"}`, `when`
    as the Library writes times — or None when it is not frozen. For the
    Library card and the status display (docs/notes/collection-freeze.md
    §11). Without `con` the store is read creating nothing (`peek`); a
    store that cannot be read is None. Never raises."""
    try:
        if con is not None:
            return _freeze_entry(con, name)
        return peek(lambda c: _freeze_entry(c, name), None)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


# ---- what stops one collection from embedding (CLAUDE.md RULE 2) ------------
#
# 2026-11-06: wikidata's embedding was turned on in the Library and nothing
# happened for hours. The collection was paused on its own — carried over by
# `hold collection rename` — and neither the Library nor `hold collection
# new` said so; the switch answered ok. RULE 2: "it proceeds with what it has
# and says in plain words what is missing and what would supply it". Every
# surface that turns embedding on, or shows it, asks here.


def _when(at) -> str | None:
    """A history entry's `at`, as the Library writes times; None for none."""
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _pause_entry(c: sqlite3.Connection, name: str) -> dict | None:
    """Collection `name`'s pause — `{"by", "at", "when"}` from the latest
    history entry for it, each None where nothing recorded it — or None
    when it is not paused."""
    import json
    if name not in paused_collections(c):
        return None
    try:
        hist = json.loads(db.get_control(c, PAUSED_HISTORY_KEY) or "[]")
    except (TypeError, ValueError):
        hist = []
    latest = next((h for h in reversed(hist if isinstance(hist, list) else [])
                   if isinstance(h, dict) and h.get("collection") == name), None)
    if latest is None or not latest.get("paused"):
        # paused before the history was kept, or the history cut short
        return {"by": None, "at": None, "when": None}
    return {"by": latest.get("by") or None, "at": latest.get("at"), "when": _when(latest.get("at"))}


def pause_of(name: str, con: sqlite3.Connection | None = None) -> dict | None:
    """Who paused collection `name` and when (`_pause_entry`), or None when
    it is not paused. Without `con` the store is read creating nothing
    (`peek`). A store that cannot be read is None here too:
    `embedding_blockers` is the one that says so."""
    try:
        if con is not None:
            return _pause_entry(con, name)
        return peek(lambda c: _pause_entry(c, name), None)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None


def _pause_says(name: str, pause: dict) -> str:
    if pause.get("by") or pause.get("when"):
        head = (f'{name} is paused on its own, by "{pause.get("by") or "unnamed"}"'
                + (f" on {pause['when']}" if pause.get("when") else ""))
    else:
        head = f"{name} is paused on its own (nothing recorded who paused it, or when)"
    return (f"{head}: the worker runs nothing for it, embedding included. Resume it in the Library "
            f"(Options › resume, on its card), or run `louise node resume {name}`.")


def embedding_blockers(name: str, embed: str | None,
                       con: sqlite3.Connection | None = None) -> list[str]:
    """What stops collection `name` from embedding, one plain sentence each
    saying what blocks and what lifts it; [] when nothing does.

    Read where the worker reads it: the node's mode (`mode_runs(…, EMBED)`),
    this collection's own pause (`paused_collections`, who and when from its
    latest history entry), and `embed`, its embed setting, when it is "off".
    `embed=None` leaves the setting out, for a caller that says it itself.
    `con` is a `connect()` the caller holds; without one the store is read
    creating nothing (`peek`), and an absent store reads as the worker reads
    it. NEVER RAISES: a store that cannot be read is said as unreadable.

    THE ONE REASON FUNCTION is app/status.py `holds` (2026-11-07): this is
    its embed stage without a collection — the node's facts and the setting
    — in the sentences the card, `node show` and `node health` say, with
    the node profile's hold where one is in force."""
    from . import status
    return [h.says for h in status.holds(name, "embed", embed_mode=embed, node_con=con)]
