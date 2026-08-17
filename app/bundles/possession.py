"""Possession binding — proving a node holds the same raw bytes the vectors
were computed from (architecture-v2.md §5: "confirm local raw_hash").

Shared by export (the producer states its hashes) and import (the consumer
checks its own against them).

WHY THIS IS COMPUTED ON DEMAND RATHER THAN READ FROM THE MANIFEST:
app/discovery.py writes `documents.txt_hash` only on a LATER pass, when a
file's mtime/size changed AND the recomputed hash differs. On a freshly
populated node it is NULL for every document. So the obvious implementation —
"compare against txt_hash when we have it" — degrades to NO CHECK AT ALL in
exactly the common case. Here, a missing hash is computed rather than skipped,
and backfilled while we are at it.

The cost is near zero on the import path, which must re-read the raw file
anyway to rebuild chunk text (the bundle carries none, and Lance's schema has
a non-null `text` column).

WHAT THIS DOES AND DOES NOT BUY:
  * It proves the importer holds the SAME REVISION of a document — Gutenberg
    re-issues texts, PubMed revises records, and without this, revision-A
    vectors attach silently to revision-B text with no structural check able
    to notice.
  * It does NOT authorize anything. A producer poisoning documents you
    legitimately hold passes possession binding trivially. That is the
    verification ladder's job.
"""
import time

from ..plugins.base import DocRef
from .format import BundleError


class PossessionError(BundleError):
    """A document's local bytes do not match the producer's claim, or the
    claim is absent. Absence is a failure, never a pass."""


def _doc_ref(row) -> DocRef:
    return DocRef(
        source_id=row["source"],
        native_id=str(row["native_id"]),
        locator=row["path"],
        mtime=row["mtime"],
        size=row["size"],
    )


def raw_hash_for(con, collection, uid: str, *, backfill: bool = True) -> str:
    """The blake2b(digest_size=16) hash of a document's raw bytes.

    Uses the manifest's cached value when present; otherwise asks the plugin to
    fetch-and-hash (a single streaming pass) and backfills the manifest.
    """
    row = con.execute(
        "SELECT uid, source, native_id, path, mtime, size, txt_hash "
        "FROM documents WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise PossessionError(f"{uid} is not in this node's manifest")
    if row["txt_hash"]:
        return row["txt_hash"]

    plugin = collection.plugin_instance()
    try:
        raw = plugin.fetch(_doc_ref(row))
    except Exception as e:
        # The plugin's own message is included: it names paths and missing
        # files, which is what an operator needs, and invariant 8 governs KEY
        # MATERIAL, not filesystem layout. Truncated, and `from None` so no
        # chained traceback reaches worker.py's traceback.format_exc() logging.
        raise PossessionError(
            f"{uid}: local raw document could not be read "
            f"({type(e).__name__}: {e})"[:300]) from None
    if not raw.raw_hash:
        raise PossessionError(f"{uid}: plugin returned no raw_hash")
    if backfill:
        con.execute("UPDATE documents SET txt_hash=?, updated_at=? WHERE uid=?",
                    (raw.raw_hash, int(time.time()), uid))
    return raw.raw_hash


def build_map(con, collection, uids) -> dict[str, str]:
    """uid -> raw_hash for every uid, computing and backfilling as needed."""
    return {uid: raw_hash_for(con, collection, uid) for uid in sorted(uids)}


def verify_map(con, collection, claimed: dict[str, str], uids) -> None:
    """Check every uid's local bytes against the producer's claim.

    Raises PossessionError naming the offending documents — the reason the
    per-uid sidecar exists rather than a single combined digest.
    """
    missing_claim = [u for u in uids if u not in claimed]
    if missing_claim:
        raise PossessionError(
            f"bundle states no raw_hash for {len(missing_claim)} uid(s): "
            f"{sorted(missing_claim)[:5]}")
    mismatched = []
    for uid in sorted(uids):
        local = raw_hash_for(con, collection, uid)
        if local != claimed[uid]:
            mismatched.append(uid)
    if mismatched:
        raise PossessionError(
            f"local corpus differs from the producer's for {len(mismatched)} "
            f"document(s): {mismatched[:5]} — these vectors were computed from "
            f"a different revision of the text")
