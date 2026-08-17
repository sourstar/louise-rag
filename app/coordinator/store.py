"""Coordinator state — its OWN SQLite database, deliberately separate.

architecture-v2.md §7: the coordinator "holds no bulk data". §16.1: "SQLite
state is local truth and never travels". A node's manifest.db and the
coordinator's store are therefore different databases with different schemas
that happen to use the same engine — and app/bundles/ledger.py's `bundles`
table is NOT this one. A node records what it imported; the coordinator records
what it accepted for redistribution. Conflating them would put a node's private
stage flags on a shared service.

Everything here is small, keyed, and boring, which is the point: §7 calls the
coordinator "deliberately boring", and the leases are advisory precisely so
this service never has to be a distributed lock manager.
"""
import json
import sqlite3
import time

SCHEMA = """
-- Advisory work assignments. NOT a lock: §7 says "leases can be advisory
-- precisely because the work is idempotent: an expired-and-reissued lease
-- wastes one GPU-hour and breaks nothing."
CREATE TABLE IF NOT EXISTS leases (
  lease_id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  collection TEXT NOT NULL,
  uids TEXT NOT NULL,              -- JSON array
  producer TEXT NOT NULL,
  issued_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  state TEXT NOT NULL              -- open | completed | expired
);
CREATE INDEX IF NOT EXISTS idx_leases_space ON leases(space_id, state);
CREATE INDEX IF NOT EXISTS idx_leases_producer ON leases(producer);

-- Accepted bundle metadata. The coordinator stores METADATA plus a pointer;
-- the bytes live in the configured artifact directory or an HF repo.
CREATE TABLE IF NOT EXISTS bundles (
  bundle_id TEXT PRIMARY KEY,
  space_id TEXT NOT NULL,
  collection TEXT,
  producer TEXT NOT NULL,
  uids TEXT,                       -- JSON array
  chunk_count INTEGER DEFAULT 0,
  tier_reached INTEGER,
  verdict TEXT,
  accepted_at INTEGER,
  artifact_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_bundles_space ON bundles(space_id);
CREATE INDEX IF NOT EXISTS idx_bundles_producer ON bundles(producer);

-- §7's "incentives v1: credit ledger + leaderboard".
CREATE TABLE IF NOT EXISTS credits (
  producer TEXT PRIMARY KEY,
  chunks_accepted INTEGER DEFAULT 0,
  bundles_accepted INTEGER DEFAULT 0,
  bundles_rejected INTEGER DEFAULT 0,
  first_seen INTEGER,
  updated_at INTEGER
);

-- Spot-check pass rate, fed by importers via POST /reports. A flat probation
-- flag is deliberate: the task's non-goals rule out reputation decay curves.
CREATE TABLE IF NOT EXISTS producer_stats (
  producer TEXT PRIMARY KEY,
  spot_checks INTEGER DEFAULT 0,
  spot_failures INTEGER DEFAULT 0,
  probation INTEGER DEFAULT 0,
  updated_at INTEGER
);

-- §7's "aggregated lazy-queues of consumers": what the network wants that
-- somebody could compute.
CREATE TABLE IF NOT EXISTS demand (
  space_id TEXT NOT NULL,
  uid TEXT NOT NULL,
  reported_by TEXT NOT NULL,
  weight INTEGER DEFAULT 1,
  ts INTEGER,
  PRIMARY KEY (space_id, uid, reported_by)
);
CREATE INDEX IF NOT EXISTS idx_demand_space ON demand(space_id);
"""

LEASE_TTL_SECONDS = 24 * 3600
PROBATION_MIN_CHECKS = 4
PROBATION_FAILURE_RATE = 0.25


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(SCHEMA)
    return con


# ---------------- credits and reputation ----------------

def bump_credit(con, producer: str, *, chunks: int = 0, accepted: int = 0,
                rejected: int = 0) -> None:
    now = int(time.time())
    con.execute(
        "INSERT INTO credits(producer,chunks_accepted,bundles_accepted,"
        "bundles_rejected,first_seen,updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(producer) DO UPDATE SET "
        "chunks_accepted=chunks_accepted+excluded.chunks_accepted,"
        "bundles_accepted=bundles_accepted+excluded.bundles_accepted,"
        "bundles_rejected=bundles_rejected+excluded.bundles_rejected,"
        "updated_at=excluded.updated_at",
        (producer, chunks, accepted, rejected, now, now))


def record_spot_check(con, producer: str, *, passed: bool) -> dict:
    """An importer's T3/T4 outcome, reported back. Flat probation flag only."""
    now = int(time.time())
    con.execute(
        "INSERT INTO producer_stats(producer,spot_checks,spot_failures,updated_at) "
        "VALUES(?,1,?,?) ON CONFLICT(producer) DO UPDATE SET "
        "spot_checks=spot_checks+1,spot_failures=spot_failures+excluded.spot_failures,"
        "updated_at=excluded.updated_at",
        (producer, 0 if passed else 1, now))
    row = con.execute("SELECT * FROM producer_stats WHERE producer=?",
                      (producer,)).fetchone()
    checks, failures = row["spot_checks"], row["spot_failures"]
    probation = int(checks >= PROBATION_MIN_CHECKS
                    and failures / checks >= PROBATION_FAILURE_RATE)
    con.execute("UPDATE producer_stats SET probation=? WHERE producer=?",
                (probation, producer))
    return {"producer": producer, "spot_checks": checks, "spot_failures": failures,
            "probation": bool(probation)}


def is_on_probation(con, producer: str) -> bool:
    row = con.execute("SELECT probation FROM producer_stats WHERE producer=?",
                      (producer,)).fetchone()
    return bool(row and row["probation"])


def leaderboard(con, limit: int = 20) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT c.*, COALESCE(s.probation,0) probation FROM credits c "
        "LEFT JOIN producer_stats s ON s.producer=c.producer "
        "ORDER BY c.chunks_accepted DESC LIMIT ?", (limit,))]


# ---------------- demand ----------------

def report_demand(con, space_id: str, uids: list[str], reported_by: str) -> int:
    now = int(time.time())
    con.executemany(
        "INSERT INTO demand(space_id,uid,reported_by,weight,ts) VALUES(?,?,?,1,?) "
        "ON CONFLICT(space_id,uid,reported_by) DO UPDATE SET "
        "weight=weight+1, ts=excluded.ts",
        [(space_id, uid, reported_by, now) for uid in uids])
    return len(uids)


def aggregated_demand(con, space_id: str, limit: int = 500) -> list[dict]:
    """What the network wants, most-wanted first.

    Demand is counted by DISTINCT REPORTERS rather than raw weight, so one
    consumer hammering its own queue cannot dominate the ordering.
    """
    return [dict(r) for r in con.execute(
        "SELECT uid, COUNT(DISTINCT reported_by) consumers, SUM(weight) weight "
        "FROM demand WHERE space_id=? AND uid NOT IN "
        "  (SELECT value FROM bundles, json_each(bundles.uids) "
        "   WHERE bundles.space_id=?) "
        "GROUP BY uid ORDER BY consumers DESC, weight DESC LIMIT ?",
        (space_id, space_id, limit))]


def covered_uids(con, space_id: str) -> list[str]:
    """§7's GET /have: which uids already have an accepted bundle."""
    rows = con.execute("SELECT uids FROM bundles WHERE space_id=?", (space_id,))
    seen: set[str] = set()
    for row in rows:
        try:
            seen.update(json.loads(row["uids"] or "[]"))
        except json.JSONDecodeError:
            continue
    return sorted(seen)
