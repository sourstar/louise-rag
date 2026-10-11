"""08 §8.2's aggregate limit, and the ledger of who has actually worked.

Migration plan step 4g. This is what replaces trust on first use, and the
replacement is not a better way of deciding whom to believe — it is a
refusal to decide at all. 08 §8.1: "no countermeasure may require knowing
whom to trust in advance". `app/bundles/trust.py` pinned the first producer
key a collection saw and refused every later one until an operator
intervened (until 2026-10-08), which is a trust bootstrap wearing a small hat: the first
stranger to arrive becomes the authority, and an honest second peer is
refused for being second.

WHAT REPLACES IT, in 8.2's words: "limit the aggregate, not the sender".
A peer accepts only so much NEW UNVERIFIED material per hour in total,
whoever sends it; beyond that, new material waits in a bounded queue. A
thousand fresh keys fill that queue no faster than one, because the
scarce resource being protected is unverified novelty and not identity.
Per-sender rate limiting is explicitly rejected there as too weak:
minting a key is free for the attacker and keeping per-sender state costs
the defender.

WHAT MAKES MATERIAL VERIFIED. 8.2 defines unverified material as that
"signed by keys none of whose work this peer has seen reproduced — no
bundle of theirs accepted through the ladder (§3.5), no attestation of
theirs this peer re-ran and confirmed". So the budget needs one durable
fact: which keys have had work accepted here. That is the `reproduced`
ledger below, and it is why this lands with step 4's importer rather than
with step 9's automatic intake — the fact ACCUMULATES. A ledger that
started on the day `share fetch` was written would count every peer who
had already contributed as a stranger.

THE QUEUE DROPS THE NEWEST. When it is full the arriving object is
refused rather than an older one evicted: otherwise a flooder empties the
queue of legitimate material simply by arriving later, which is the
cheapest attack there is. A refusal is not a judgement about the sender
and is NOT REMEMBERED — the object is announced again by whoever holds
it, and is admitted when there is room.

THE NUMBERS ARE THE PEER'S, not the protocol's (`platform.toml`'s
`[admission]`, 07 §7.8). Nothing in the protocol reads another peer's
setting and no peer can tell what another's is, so unlike a threshold
that had to be agreed it cannot drift, cannot be negotiated, and cannot
be gamed by anyone but its owner.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: 08 §8.2's window: "per hour", and both allowances share it.
WINDOW_S = 3600

#: The verdicts. `KNOWN` is not `ADMITTED` on purpose — a caller that
#: logs them apart can see whether its peer set is warming up at all.
ADMITTED, KNOWN, QUEUED, REFUSED = "admitted", "known", "queued", "refused"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reproduced (
    pubkey     TEXT PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    bundles    INTEGER NOT NULL DEFAULT 0,
    chunks     INTEGER NOT NULL DEFAULT 0,
    last_ref   TEXT
);
CREATE TABLE IF NOT EXISTS arrivals (
    at     INTEGER NOT NULL,
    bytes  INTEGER NOT NULL,
    pubkey TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_arrivals_at ON arrivals(at);
CREATE TABLE IF NOT EXISTS admission_queue (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     INTEGER NOT NULL,
    pubkey TEXT NOT NULL,
    kind   TEXT NOT NULL,
    bytes  INTEGER NOT NULL,
    ref    TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Limits:
    """`platform.toml`'s `[admission]`, with 08 §8.2's defaults.

    "64 MB and one thousand objects per hour, whichever binds first — an
    attestation is a few hundred bytes, so a million of them fit inside
    any byte budget and would swamp a catalogue by count alone."
    """
    bytes_per_hour: int = 64 * 1024 ** 2
    objects_per_hour: int = 1000
    queue_bytes: int = 256 * 1024 ** 2

    @property
    def closed(self) -> bool:
        """"A peer that lowers them to zero accepts nothing from strangers
        and is complete in every other respect.\""""
        return self.bytes_per_hour <= 0 or self.objects_per_hour <= 0


def state_dir(root) -> Path:
    """`<LOUISE_ROOT>/state/`, beside `collections/` (07 §7.8).

    `root` is the collections directory every `hold` command takes, so
    the node's own state is its sibling — never inside it, because
    `collections/<name>/` is the SHARD a peer serves and this is exactly
    what no peer sees.
    """
    return Path(root).parent / "state"


def connect(root) -> sqlite3.Connection:
    """`state/node.sqlite`, created on first use (07 §7.8)."""
    d = state_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(d / "node.sqlite"))
    con.row_factory = sqlite3.Row
    from app import db as _db
    _db.wal(con)                       # the same file node_state opens: the loser waits
    con.executescript(_SCHEMA)
    con.commit()
    return con


# --------------------------------------------------------------------------
# the ledger: whose work this peer has seen reproduced

def reproduced(con, pubkey: str) -> dict | None:
    row = con.execute("SELECT * FROM reproduced WHERE pubkey=?",
                      (pubkey,)).fetchone()
    return dict(row) if row else None


def record_reproduced(con, pubkey: str, *, ref: str, chunks: int = 0,
                      now: int | None = None) -> None:
    """This key's work was accepted here — 8.2's "a bundle of theirs
    accepted through the ladder (§3.5)".

    Recorded only on a bundle that PASSED, never on one that arrived: the
    whole point is that the key is known by what it produced and not by
    having sent something.
    """
    at = int(time.time() if now is None else now)
    con.execute(
        "INSERT INTO reproduced(pubkey,first_seen,last_seen,bundles,chunks,last_ref) "
        "VALUES(?,?,?,1,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET last_seen=excluded.last_seen, "
        "bundles=bundles+1, chunks=chunks+excluded.chunks, last_ref=excluded.last_ref",
        (pubkey, at, at, int(chunks), ref))
    con.commit()


# --------------------------------------------------------------------------
# the aggregate limit

def spent(con, *, now: int | None = None) -> tuple[int, int]:
    """`(bytes, objects)` inside the last hour, pruning what fell out."""
    at = int(time.time() if now is None else now)
    con.execute("DELETE FROM arrivals WHERE at <= ?", (at - WINDOW_S,))
    row = con.execute(
        "SELECT COALESCE(SUM(bytes),0) AS b, COUNT(*) AS n FROM arrivals").fetchone()
    return int(row["b"]), int(row["n"])


def queued_bytes(con) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(bytes),0) AS b FROM admission_queue").fetchone()
    return int(row["b"])


def offer(con, pubkey: str, n_bytes: int, *, kind: str, ref: str,
          limits: Limits | None = None, now: int | None = None) -> str:
    """One object arrives from a peer. Returns what happened to it.

      * `KNOWN`   — this key's work has been reproduced here, so the
                    object is not unverified material at all: 8.2 says it
                    "is never queued", and it does not spend the budget
                    either, because the budget is defined over what is
                    unverified;
      * `ADMITTED` — inside both allowances; recorded against them;
      * `QUEUED`  — beyond one of them, and the queue had room. It "waits
                    in arrival order and is neither forwarded nor
                    announced by this peer until admitted";
      * `REFUSED` — the queue was full. The NEWEST is dropped, and the
                    refusal is not remembered.
    """
    limits = limits or Limits()
    at = int(time.time() if now is None else now)
    if reproduced(con, pubkey):
        return KNOWN

    n_bytes = max(0, int(n_bytes))
    b, n = spent(con, now=at)
    if not limits.closed and b + n_bytes <= limits.bytes_per_hour \
            and n + 1 <= limits.objects_per_hour:
        con.execute("INSERT INTO arrivals(at,bytes,pubkey) VALUES(?,?,?)",
                    (at, n_bytes, pubkey))
        con.commit()
        return ADMITTED

    if queued_bytes(con) + n_bytes > limits.queue_bytes:
        return REFUSED
    con.execute(
        "INSERT INTO admission_queue(at,pubkey,kind,bytes,ref) VALUES(?,?,?,?,?)",
        (at, pubkey, kind, n_bytes, ref))
    con.commit()
    return QUEUED


def release(con, *, limits: Limits | None = None,
            now: int | None = None) -> list[dict]:
    """Admit what the queue can now afford, IN ARRIVAL ORDER.

    Called when the window has moved on. Stops at the first object that
    does not fit rather than skipping it for a smaller one behind: a
    queue that reordered itself by size would let a flood of small
    objects starve a legitimate large one indefinitely.
    """
    limits = limits or Limits()
    at = int(time.time() if now is None else now)
    out: list[dict] = []
    if limits.closed:
        return out
    for row in con.execute(
            "SELECT * FROM admission_queue ORDER BY id").fetchall():
        b, n = spent(con, now=at)
        if b + row["bytes"] > limits.bytes_per_hour \
                or n + 1 > limits.objects_per_hour:
            break
        con.execute("INSERT INTO arrivals(at,bytes,pubkey) VALUES(?,?,?)",
                    (at, row["bytes"], row["pubkey"]))
        con.execute("DELETE FROM admission_queue WHERE id=?", (row["id"],))
        out.append(dict(row))
    con.commit()
    return out
