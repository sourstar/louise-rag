"""The node-local bundles ledger.

Records every import attempt, accepted or not. Three jobs beyond bookkeeping:

  1. IDEMPOTENCE (CLAUDE.md invariant 2). `bundle_id` is the primary key, so a
     re-import is a cheap no-op rather than a duplicate write.
  2. CRASH RECOVERY. A row moves to 'applying' BEFORE the destructive Lance
     write, so a crash is visible on restart and can be swept to 'aborted'.
  3. QUARANTINE AND FORGERY PROOF (architecture-v2.md §6). Retaining the exact
     uids written lets a later quarantine delete precisely that producer's
     rows; retaining the signature and digests turns "this producer forged
     vectors" from an accusation into something checkable by a third party.

This is local state and never travels (invariant 6). P1.3's coordinator keeps
its own ledger, in its own service database.
"""
import json
import time

APPLYING = "applying"
APPLIED = "applied"
REJECTED = "rejected"
ABORTED = "aborted"


def get(con, bundle_id: str) -> dict | None:
    row = con.execute("SELECT * FROM bundles WHERE bundle_id=?", (bundle_id,)).fetchone()
    return dict(row) if row else None


def already_applied(con, bundle_id: str) -> bool:
    row = get(con, bundle_id)
    return bool(row and row["state"] == APPLIED)


def record_applying(con, *, bundle_id: str, producer_pubkey: str, space_id: str,
                    collection: str, chunk_count: int, signature_hex: str,
                    parquet_sha256: str, content_digest: str, created_at: int,
                    space_weak: bool) -> None:
    """Durable statement of intent, written BEFORE any destructive Lance
    operation so a crash mid-import is recoverable rather than silent."""
    con.execute(
        "INSERT INTO bundles(bundle_id,producer_pubkey,space_id,collection,state,"
        "uids,chunk_count,signature,parquet_sha256,content_digest,space_weak,"
        "created_at,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(bundle_id) DO UPDATE SET state=excluded.state,"
        "imported_at=excluded.imported_at,error=NULL",
        (bundle_id, producer_pubkey, space_id, collection, APPLYING, None,
         chunk_count, signature_hex, parquet_sha256, content_digest,
         1 if space_weak else 0, created_at, int(time.time())))


def record_applied(con, bundle_id: str, uids_written: dict[str, int],
                   verdict_summary: str, tier_reached: int | None = None) -> None:
    con.execute(
        "UPDATE bundles SET state=?,uids=?,chunk_count=?,verdict=?,tier_reached=?,"
        "error=NULL,imported_at=? WHERE bundle_id=?",
        (APPLIED, json.dumps(uids_written, sort_keys=True),
         sum(uids_written.values()), verdict_summary,
         None if tier_reached is None else int(tier_reached),
         int(time.time()), bundle_id))
    row = get(con, bundle_id)
    if row:
        _bump_provider(con, row["producer_pubkey"], accepted=1)


def record_rejected(con, *, bundle_id: str | None, producer_pubkey: str | None,
                    reason: str, verdict_summary: str = "",
                    collection: str | None = None) -> None:
    """A rejection is recorded here and NEVER in documents.error.

    worker._next_book filters `error IS NULL` for the rdf and fts stages too,
    so writing an import failure onto the document would let one malicious
    bundle remove that document from the ENTIRE pipeline, permanently.
    """
    key = bundle_id or f"unidentified:{int(time.time() * 1000)}"
    con.execute(
        "INSERT INTO bundles(bundle_id,producer_pubkey,collection,state,verdict,"
        "error,imported_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(bundle_id) DO UPDATE SET state=excluded.state,"
        "verdict=excluded.verdict,error=excluded.error,imported_at=excluded.imported_at",
        (key, producer_pubkey or "unknown", collection, REJECTED,
         verdict_summary, reason[:1000], int(time.time())))
    if producer_pubkey:
        _bump_provider(con, producer_pubkey, rejected=1)


def record_evidence(con, bundle_id: str, evidence: str) -> None:
    """The FULL verification evidence, stored beside (not inside) the bundles
    row. Untruncated on purpose — see the bundle_evidence schema comment."""
    con.execute(
        "INSERT INTO bundle_evidence(bundle_id,evidence,created_at) VALUES(?,?,?) "
        "ON CONFLICT(bundle_id) DO UPDATE SET evidence=excluded.evidence,"
        "created_at=excluded.created_at",
        (bundle_id, evidence, int(time.time())))


def get_evidence(con, bundle_id: str) -> str | None:
    row = con.execute("SELECT evidence FROM bundle_evidence WHERE bundle_id=?",
                       (bundle_id,)).fetchone()
    return row["evidence"] if row else None


def sweep_stale(con) -> list[str]:
    """Move any 'applying' row to 'aborted'. Call at worker startup.

    No compensating action is needed: the importer clears embed_done BEFORE
    touching Lance, so an interrupted import leaves the document looking
    un-embedded and _next_book re-queues it.
    """
    stale = [r["bundle_id"] for r in
             con.execute("SELECT bundle_id FROM bundles WHERE state=?", (APPLYING,))]
    if stale:
        con.execute("UPDATE bundles SET state=?,error=? WHERE state=?",
                    (ABORTED, "interrupted before completion", APPLYING))
    return stale


def uids_written_by(con, producer_pubkey: str) -> dict[str, int]:
    """Every uid this producer's applied bundles wrote — the input to a
    quarantine cleanup."""
    out: dict[str, int] = {}
    for row in con.execute(
            "SELECT uids FROM bundles WHERE producer_pubkey=? AND state=?",
            (producer_pubkey, APPLIED)):
        if not row["uids"]:
            continue
        try:
            for uid, count in json.loads(row["uids"]).items():
                out[uid] = out.get(uid, 0) + int(count)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return out


def recent(con, limit: int = 50) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT * FROM bundles ORDER BY imported_at DESC LIMIT ?", (limit,))]


def _bump_provider(con, pubkey: str, *, accepted: int = 0, rejected: int = 0) -> None:
    now = int(time.time())
    con.execute(
        "INSERT INTO providers(pubkey,bundles_accepted,bundles_rejected,"
        "first_seen,last_seen) VALUES(?,?,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET "
        "bundles_accepted=bundles_accepted+excluded.bundles_accepted,"
        "bundles_rejected=bundles_rejected+excluded.bundles_rejected,"
        "last_seen=excluded.last_seen",
        (pubkey, accepted, rejected, now, now))


# ---------------- per-chunk verification state (T4) ----------------

def mark_imported(con, chunk_ids: list[str], uid: str, producer: str,
                  bundle_id: str) -> None:
    """Imported chunks start unverified. Locally embedded chunks get NO row —
    absence is what 'local' means, which keeps this table proportional to
    imported content rather than to the whole corpus."""
    now = int(time.time())
    con.executemany(
        "INSERT INTO chunk_verification(chunk_id,uid,state,producer,bundle_id,checked_at) "
        "VALUES(?,?,'imported',?,?,?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET state='imported',producer=excluded.producer,"
        "bundle_id=excluded.bundle_id,cosine=NULL,checked_at=excluded.checked_at",
        [(cid, uid, producer, bundle_id, now) for cid in chunk_ids])


def clear_verification(con, uid: str) -> None:
    """Called when a document is re-embedded locally: its chunks are no longer
    imported, so their rows must go rather than linger as stale provenance."""
    con.execute("DELETE FROM chunk_verification WHERE uid=?", (uid,))


def set_verified(con, chunk_id: str, cosine: float, passed: bool) -> None:
    con.execute(
        "UPDATE chunk_verification SET state=?,cosine=?,checked_at=? WHERE chunk_id=?",
        ("verified" if passed else "failed", float(cosine), int(time.time()), chunk_id))


def verification_states(con, chunk_ids: list[str]) -> dict[str, dict]:
    if not chunk_ids:
        return {}
    marks = ",".join("?" for _ in chunk_ids)
    return {r["chunk_id"]: dict(r) for r in con.execute(
        f"SELECT * FROM chunk_verification WHERE chunk_id IN ({marks})", chunk_ids)}
