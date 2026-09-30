"""Work the web process asks for and the worker does: what needs the key.

The web process holds only the public half of this node's key
(app/identity.py; operator, 2026-09-29), so the two Library actions that
SIGN — creating a collection (its calibration member, 09 §9.12) and
exporting a bundle — are queued here and run by the worker, which holds the
private key. A job is a row in `state/node.sqlite` (07 §7.8: the node's
state database), written by the web and claimed by the worker.

States: queued -> running -> done | failed. A done export becomes `expired`
when its file is collected or after `EXPORT_KEEP_S`; finished rows go after
`KEEP_ROWS_S`. Both are swept by the worker, so the table stays bounded by
what an operator clicks in a week.

INTERRUPTED IS QUEUED AGAIN. Neither kind writes anything durable until its
end — `create` embeds the calibration set before it writes a byte, and is
idempotent by its hash; an export is built in a scratch directory named for
its job — so a job left `running` by a stopped worker is queued again at the
next start (`requeue_running`), and its scratch is discarded.

A CREATE WAITS FOR THE MODE. It starts the encoder, which runs only where
everything runs (app/node_state.py, MODE_RUNS; operator, 2026-09-29): while
the node is paused, seeding or downloading only, it stays queued and says
why. An export reads what is stored and signs it; it runs in any mode, as
it did from the web.
"""
import json
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import node_state

CREATE, EXPORT = "collection-create", "bundle-export"
#: A deposit collection's writes, queued by the web's LocalAGI routes (07
#: §7.4): the web process must not write a collection's indexes, because
#: tantivy allows ONE writer per index directory and the worker keeps its
#: writers open — its catalog backfill alone holds one for every collection.
#: Run by their own worker thread (`serve_deposits`), never behind a create
#: that is embedding a calibration set for minutes; in every mode, because
#: an agent writing its memory is not the node's background work.
DEPOSIT = "deposit"
KINDS = (CREATE, EXPORT, DEPOSIT)

#: A deposit job's row names a key, and 02 §2.9 lets nothing name an erased
#: deposit: the web deletes the row when it has read the result (`forget`),
#: and the sweep deletes one nobody read after this long.
DEPOSIT_KEEP_S = 600
QUEUED, RUNNING, DONE, FAILED, EXPIRED = "queued", "running", "done", "failed", "expired"

#: a finished export's file is kept this long for its download
EXPORT_KEEP_S = 24 * 3600
#: a finished row is kept this long for the page that asked
KEEP_ROWS_S = 7 * 24 * 3600

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS jobs ("
    " id INTEGER PRIMARY KEY, kind TEXT NOT NULL, args TEXT NOT NULL,"
    " state TEXT NOT NULL, result TEXT, error TEXT,"
    " created REAL NOT NULL, started REAL, finished REAL)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, id)",
)

#: the claim: the oldest queued job of the kinds the mode allows. Queued
#: rows are the operator's pending clicks — a handful — and the index walks
#: exactly those (plan-checked in tests/test_jobs.py).
CLAIM_SQL = "SELECT * FROM jobs WHERE state=? AND kind IN ({}) ORDER BY id LIMIT 1"


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    args: dict[str, Any]
    state: str
    result: dict[str, Any] | None
    error: str | None
    created: float
    started: float | None
    finished: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "state": self.state,
                "result": self.result, "error": self.error,
                "created": self.created, "started": self.started,
                "finished": self.finished}


def connect() -> sqlite3.Connection:
    con = ensure_schema(node_state.connect())
    con.execute("PRAGMA secure_delete=ON")     # a forgotten row is zeroed (02 §2.9)
    return con


def ensure_schema(con: sqlite3.Connection) -> sqlite3.Connection:
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def _job(row) -> Job:
    return Job(id=row["id"], kind=row["kind"], args=json.loads(row["args"]),
               state=row["state"],
               result=json.loads(row["result"]) if row["result"] else None,
               error=row["error"], created=row["created"],
               started=row["started"], finished=row["finished"])


def submit(kind: str, args: dict[str, Any]) -> int:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    with closing(connect()) as con:
        cur = con.execute(
            "INSERT INTO jobs (kind, args, state, created) VALUES (?, ?, ?, ?)",
            (kind, json.dumps(args, sort_keys=True), QUEUED, time.time()))
        return int(cur.lastrowid)


def get(job_id: int) -> Job | None:
    with closing(connect()) as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job(row) if row else None


def kinds_allowed(mode: str) -> tuple[str, ...]:
    """What the general job thread may run in this mode. Deposits are not
    among them: they have a thread of their own (`serve_deposits`)."""
    return (CREATE, EXPORT) if node_state.runs(mode, node_state.EMBED) else (EXPORT,)


def waiting_because(job: Job, mode: str) -> str | None:
    """Why a queued job is not running, in the Library's words."""
    if job.state != QUEUED:
        return None
    if job.kind not in kinds_allowed(mode):
        label = dict(node_state.MODE_LABELS).get(mode, mode)
        return (f"waiting: the node is set to “{label}”, and creating a "
                f"collection embeds the calibration set, which runs only "
                f"when everything runs")
    return "queued: the worker takes it within a few seconds"


def view(job: Job) -> dict[str, Any]:
    """What the page is told: the job, why it waits, and never where the
    worker built an export — that is served by its own route."""
    out = job.to_dict()
    out["waiting"] = waiting_because(job, node_state.worker_state())
    if out["result"]:
        out["result"] = {k: v for k, v in out["result"].items()
                         if k not in ("scratch", "file")}
    return out


def claim(con: sqlite3.Connection, kinds: tuple[str, ...]) -> Job | None:
    """The oldest queued job of `kinds`, marked running. A read then a
    write, so BEGIN IMMEDIATE (CLAUDE.md)."""
    if not kinds:
        return None
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(CLAIM_SQL.format(",".join("?" * len(kinds))),
                          (QUEUED, *kinds)).fetchone()
        if row is None:
            con.execute("COMMIT")
            return None
        now = time.time()
        con.execute("UPDATE jobs SET state=?, started=? WHERE id=?",
                    (RUNNING, now, row["id"]))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return _job(con.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())


def finish(con: sqlite3.Connection, job_id: int, result: dict[str, Any]) -> None:
    con.execute("UPDATE jobs SET state=?, result=?, finished=? WHERE id=?",
                (DONE, json.dumps(result, sort_keys=True), time.time(), job_id))


def fail(con: sqlite3.Connection, job_id: int, error: str) -> None:
    con.execute("UPDATE jobs SET state=?, error=?, finished=? WHERE id=?",
                (FAILED, error[:2000], time.time(), job_id))


def requeue_running(con: sqlite3.Connection) -> list[int]:
    """At the worker's start: a job the last worker left running is queued
    again, and an export's scratch is discarded."""
    ensure_schema(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute("SELECT * FROM jobs WHERE state=?", (RUNNING,)).fetchall()
        con.execute("UPDATE jobs SET state=?, started=NULL WHERE state=?",
                    (QUEUED, RUNNING))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    for row in rows:
        _discard_partial(_job(row))
    return [r["id"] for r in rows]


def _scratch_prefix(job_id: int) -> str:
    return f"bundle-export-job{job_id}-"


def _discard_partial(job: Job) -> None:
    """What an interrupted export left in its workspace, by the job's own
    prefix: nothing another job made."""
    if job.kind != EXPORT:
        return
    from . import collections as collections_mod
    from .bundles import export as bundle_export
    try:
        collection = collections_mod.by_name(job.args["collection"])
        where = bundle_export.workspace(collection) if collection else None
    except Exception:                                   # noqa: BLE001
        return
    for d in (where.glob(_scratch_prefix(job.id) + "*") if where else ()):
        shutil.rmtree(d, ignore_errors=True)


def collected(job_id: int) -> None:
    """The web has served an export's file: it is removed and the job
    expires."""
    with closing(connect()) as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row["state"] != DONE:
            return
        job = _job(row)
        _discard(job)
        con.execute("UPDATE jobs SET state=? WHERE id=? AND state=?",
                    (EXPIRED, job_id, DONE))


def _discard(job: Job) -> None:
    scratch = (job.result or {}).get("scratch")
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)


def sweep(con: sqlite3.Connection, now: float | None = None) -> None:
    """Exports whose file has waited `EXPORT_KEEP_S` expire; finished rows
    older than `KEEP_ROWS_S` go. The worker, every few rounds."""
    now = time.time() if now is None else now
    for row in con.execute("SELECT * FROM jobs WHERE state=? AND kind=? AND finished<?",
                           (DONE, EXPORT, now - EXPORT_KEEP_S)).fetchall():
        _discard(_job(row))
        con.execute("UPDATE jobs SET state=? WHERE id=?", (EXPIRED, row["id"]))
    con.execute("DELETE FROM jobs WHERE state IN (?, ?, ?) AND finished<?",
                (DONE, FAILED, EXPIRED, now - KEEP_ROWS_S))
    con.execute("DELETE FROM jobs WHERE kind=? AND state IN (?, ?) AND finished<?",
                (DEPOSIT, DONE, FAILED, now - DEPOSIT_KEEP_S))


def forget(job_id: int) -> None:
    """A finished deposit job's row deleted, and the node state's write-ahead
    log truncated, so neither holds the key it named (02 §2.9). Best effort
    on the log: a reader in the middle of a transaction keeps it for now,
    and the next forget truncates it."""
    with closing(connect()) as con:
        con.execute("DELETE FROM jobs WHERE id=? AND kind=?", (job_id, DEPOSIT))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# ---- what the worker runs -------------------------------------------------------

def run_next(con: sqlite3.Connection, mode: str, log=print,
             kinds: tuple[str, ...] | None = None) -> Job | None:
    """Claim the oldest job this mode allows — or of `kinds` — do it, and
    record how it ended; the job as it ended, or None when nothing was
    queued. The worker's job threads call this; so do the tests."""
    job = claim(con, kinds_allowed(mode) if kinds is None else kinds)
    if job is None:
        return None
    what = job.args.get("collection") or job.args.get("plugin") or "?"
    log(f"[info:jobs] {what}: job {job.id} ({job.kind}) started")
    try:
        result = run(job)
    except sqlite3.OperationalError as e:
        if _contended(e):
            # deferral is not failure (CLAUDE.md): a busy store queues it again
            con.execute("UPDATE jobs SET state=?, started=NULL WHERE id=?",
                        (QUEUED, job.id))
            log(f"[info:jobs] {what}: job {job.id} ({job.kind}) deferred: {e}")
        else:
            fail(con, job.id, f"{type(e).__name__}: {e}")
            log(f"[warn:jobs] {what}: job {job.id} ({job.kind}) failed: {e}")
    except Exception as e:                              # noqa: BLE001
        fail(con, job.id, f"{type(e).__name__}: {e}")
        log(f"[warn:jobs] {what}: job {job.id} ({job.kind}) failed: "
            f"{type(e).__name__}: {e}")
    else:
        finish(con, job.id, result)
        log(f"[info:jobs] {what}: job {job.id} ({job.kind}) done")
    return _job(con.execute("SELECT * FROM jobs WHERE id=?", (job.id,)).fetchone())


def _contended(e: sqlite3.OperationalError) -> bool:
    text = str(e).lower()
    return "locked" in text or "busy" in text


def run(job: Job) -> dict[str, Any]:
    """Do the job; its result, or an exception whose text is the failure."""
    if job.kind == CREATE:
        return _create(job.args)
    if job.kind == EXPORT:
        return _export(job.id, job.args)
    if job.kind == DEPOSIT:
        return _deposit(job.args)
    raise ValueError(f"unknown job kind {job.kind!r}")


def _deposit(args: dict[str, Any]) -> dict[str, Any]:
    """One deposit-collection write, by the command the CLI runs (07 §7.4:
    the route is a client of the command, not a second implementation).
    An upload's bytes wait in the collection's `state/incoming/`, written
    there by the web, and go whatever the outcome."""
    from . import louise
    from .hold import deposit
    op, name = args["op"], args["collection"]
    if op == "create":
        codex, _held = louise.create_deposit(louise._default_root(), name)
        return {"collection_id": codex.collection_id}
    if op == "add":
        staged = Path(args["staged"])
        try:
            got = deposit.add(name, args["key"], staged.read_bytes())
        finally:
            staged.unlink(missing_ok=True)
        return {"key": got["key"], "chunks": got["chunks"]}
    if op == "erase":
        return {"remaining": deposit.erase(name, args["key"])}
    if op == "reset":
        return {"erased": deposit.reset(name)}
    raise ValueError(f"unknown deposit operation {op!r}")


def serve_deposits(stop, poll_s: float = 0.2, log=print) -> None:
    """The deposit thread: every queued deposit job, oldest first, until
    `stop` is set. The worker runs it; a test runs it beside its client. It
    first finishes any erase a crash cut short (02 §2.9)."""
    from .hold import deposit
    try:
        deposit.finish_pending()
    except Exception as e:                              # noqa: BLE001
        log(f"[warn:jobs] deposits: finishing a pending purge: {type(e).__name__}: {e}")
    con = connect()
    try:
        while not stop.is_set():
            try:
                done = run_next(con, "running", log=log, kinds=(DEPOSIT,))
            except sqlite3.Error:
                stop.wait(poll_s)
                continue
            if done is None:
                stop.wait(poll_s)
    finally:
        con.close()


def _create(args: dict[str, Any]) -> dict[str, Any]:
    """`hold collection create`, as the route ran it before (07 §7.4). The
    encoder call is made at the level of a person waiting, as it was from
    the web (docs/notes/scheduling.md §12)."""
    import tomllib
    from . import dispatch, louise
    from .dispatch import Level
    recipe = args["recipe"]
    recipe = tomllib.loads(recipe) if isinstance(recipe, str) else dict(recipe)
    root = louise._default_root()
    root.mkdir(parents=True, exist_ok=True)
    with dispatch.working_as(Level.QUERY):
        codex, held = louise.create_from(root, args["plugin"], recipe,
                                         platform_image=args.get("platform_image"),
                                         space_from=args.get("space_from"),
                                         encoder=args.get("encoder"),
                                         query_encoder=args.get("query_encoder"))
    return {"exit": 0, "collection_id": codex.collection_id, "name": held.stem,
            "held": str(held), "cli": args.get("cli", "")}


def _export(job_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """The Library's bundle export (the code path of `bundle export`), built
    in the collection's export workspace and left there for the web to
    serve. Invariant 9 is asked again here: the acknowledgement may have
    gone between the click and the run."""
    from . import collections as collections_mod, db, licensing
    from .bundles import export as bundle_export
    import tarfile
    collection = collections_mod.by_name(args["collection"])
    if collection is None:
        raise LookupError(f"no collection {args['collection']!r}")
    licensing.require_ack(collection, use="its vectors are exported as a bundle")
    where = bundle_export.workspace(collection)
    scratch = Path(tempfile.mkdtemp(prefix=_scratch_prefix(job_id), dir=where))
    try:
        con = db.connect(collection)
        result = bundle_export.export_bundle(con, collection, list(args["uids"]),
                                             scratch / "bundle")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"bundle-{collection.name}-{stamp}"
        tar_path = scratch / f"{name}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(scratch / "bundle", arcname=name)
        shutil.rmtree(scratch / "bundle", ignore_errors=True)
        db.log_event(con, "info", "bundle",
                     f"exported {result['chunks']} chunks / {result['uids']} docs "
                     f"as bundle {result['bundle_id'][:12]}… (job {job_id})")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return {"collection": collection.name, "chunks": result["chunks"],
            "uids": result["uids"], "bundle_id": result["bundle_id"],
            "scratch": str(scratch), "file": str(tar_path),
            "filename": tar_path.name}


def export_file(job: Job, workspace: Path) -> Path | None:
    """The file a done export left, only if it lies in its collection's
    export workspace — the row is in `state/`, and a path read from it is
    not trusted to point anywhere else."""
    if job.kind != EXPORT or job.state != DONE or not job.result:
        return None
    f = Path(job.result.get("file", ""))
    try:
        ok = f.resolve().is_relative_to(Path(workspace).resolve())
    except OSError:
        return None
    return f if ok and f.is_file() else None
