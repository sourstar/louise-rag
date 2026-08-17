"""Verify-on-use — recompute the vectors a query actually surfaced.

architecture-v2.md §6's T4 tier, and the ONLY thing that catches targeted
poisoning: a handful of deliberately mismapped vectors inside a bundle that is
otherwise honest, correctly signed, and produced with the right model. Import's
admission checks sample a few chunks out of thousands, so they cannot find
three bad ones; but a poisoned vector is inert until retrieval surfaces it, and
at that moment we know exactly which chunks to check.

DEFAULT OFF, two triggers:
  * per query   — `verify: true` on a search/chat request, blocking, so an
                  answer that matters is checked before it is given
  * after a query — POST /api/verify with the chunk ids just returned

VERIFIED STATE IS PERMANENT PER CHUNK. Query distributions are Zipfian, so the
hot set verifies within days and cold chunks never cost anything — which is why
§6 puts the lifetime overhead at ~2% rather than per-query.

A FAILURE IS PROOF OF FORGERY, not an error: the text, the claimed vector and
the recomputed one are all recorded, and the producer key is quarantined so its
other bundles can be rolled back.
"""
import time

import numpy as np

from .. import embeddings
from . import ledger, trust

# The fallback when no space artifact is available: coarse, same as import-time
# sample re-embed — it separates "same model on different hardware" (>=0.999)
# from "different model or tampered vector" (0.3-0.7) without pretending to a
# calibrated precision it does not have. When an artifact IS available,
# _verify_floor() uses its calibrated tau through t3's tighten-only path, so an
# untrusted artifact can raise this bar but never lower it.
VERIFY_FLOOR = 0.95


def _verify_floor(con) -> tuple[float, str]:
    """(threshold, source) for this collection's space. Never raises — a
    missing or malformed artifact degrades to the coarse floor, because a
    verification pass must not be able to break the query that triggered it."""
    try:
        from ..verify.t3 import _tau_for
        from .importer import find_space_artifact
        row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
        if not row or not row["space_id"]:
            return VERIFY_FLOOR, "coarse-fallback"
        artifact = find_space_artifact(row["space_id"])
        if artifact is None:
            return VERIFY_FLOOR, "coarse-fallback"
        tau, _f0, _f1, source = _tau_for(artifact, "", "f32")
        return tau, source
    except Exception:
        return VERIFY_FLOOR, "coarse-fallback"


def _texts_and_vectors(collection, chunk_ids: list[str]) -> dict[str, tuple]:
    """Pull the stored text and vector for the given chunks out of Lance."""
    conn = embeddings._conn(collection)
    if embeddings.TABLE not in conn.table_names():
        return {}
    table = conn.open_table(embeddings.TABLE)
    quoted = ",".join("'" + c.replace("'", "") + "'" for c in chunk_ids)
    if not quoted:
        return {}
    arrow = table.search().where(f"id IN ({quoted})").limit(len(chunk_ids) * 2).to_arrow()
    out = {}
    for cid, text, vector in zip(arrow.column("id").to_pylist(),
                                  arrow.column("text").to_pylist(),
                                  arrow.column("vector").to_pylist()):
        out[cid] = (text, np.asarray(vector, dtype=np.float32))
    return out


def pending(con, chunk_ids: list[str]) -> list[str]:
    """Which of these chunks are imported and not yet verified.

    Chunks with no row were embedded locally — there is nothing to verify, and
    re-embedding them would only burn GPU time confirming our own arithmetic.
    """
    states = ledger.verification_states(con, chunk_ids)
    return [cid for cid in chunk_ids
            if cid in states and states[cid]["state"] == "imported"]


def verify_chunks(con, collection, chunk_ids: list[str], *, embed_fn=None,
                  quarantine_on_failure: bool = True) -> dict:
    """Re-embed and check the given chunks. Never raises: a verification pass
    must not be able to break the query that triggered it."""
    targets = pending(con, chunk_ids)
    if not targets:
        return {"checked": 0, "verified": 0, "failed": 0, "quarantined": []}

    stored = _texts_and_vectors(collection, targets)
    targets = [cid for cid in targets if cid in stored and stored[cid][0]]
    if not targets:
        return {"checked": 0, "verified": 0, "failed": 0, "quarantined": []}

    if embed_fn is not None:
        embed = embed_fn
    else:
        from ..collections import recipe_for
        recipe = recipe_for(collection)
        embed = lambda texts: embeddings.embed_texts(texts, recipe=recipe)  # noqa: E731
    try:
        recomputed = embed([stored[cid][0] for cid in targets])
    except Exception:
        # Transient: the endpoint is down. Leave the chunks unverified rather
        # than marking them failed — an outage is not evidence of forgery.
        return {"checked": 0, "verified": 0, "failed": 0, "quarantined": [],
                "unavailable": True}

    floor, floor_source = _verify_floor(con)
    states = ledger.verification_states(con, targets)
    verified = failed = 0
    culprits: set[str] = set()
    for cid, fresh_raw in zip(targets, recomputed):
        fresh = np.asarray(fresh_raw, dtype=np.float32)
        claimed = stored[cid][1]
        denominator = float(np.linalg.norm(fresh) * np.linalg.norm(claimed))
        cosine = 0.0 if denominator == 0 else float(np.dot(fresh, claimed) / denominator)
        passed = cosine >= floor
        ledger.set_verified(con, cid, cosine, passed)
        if passed:
            verified += 1
        else:
            failed += 1
            producer = states.get(cid, {}).get("producer")
            if producer:
                culprits.add(producer)

    quarantined = []
    requeued: set[str] = set()
    if failed and quarantine_on_failure:
        for producer in sorted(culprits):
            affected = trust.quarantine_key(
                con, producer,
                f"verify-on-use: recomputed vectors did not match at "
                f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
            quarantined.append(producer)
            # F1-D1: quarantining the KEY is not enough — the producer's other
            # vectors, including whole documents the failed sample never
            # touched, are still live in the index. §6 says "quarantine the
            # key, re-embed locally", so every document this producer's
            # bundles wrote goes back to un-embedded and into the priority
            # queue. The stale vectors keep serving only until the worker's
            # delete-then-insert replaces them — a bounded window, traded
            # deliberately against making those documents unsearchable now.
            requeued.update(_requeue_for_local_embed(con, affected))
    return {"checked": len(targets), "verified": verified, "failed": failed,
            "quarantined": quarantined, "requeued": sorted(requeued),
            "floor": floor, "floor_source": floor_source}


def _requeue_for_local_embed(con, affected: dict[str, int]) -> list[str]:
    now = int(time.time())
    for uid in affected:
        con.execute("UPDATE documents SET embed_done=0, updated_at=? WHERE uid=?",
                    (now, uid))
        con.execute(
            "INSERT INTO embed_queue(uid,priority,ts) VALUES(?,100,?) "
            "ON CONFLICT(uid) DO UPDATE SET priority=100, ts=excluded.ts",
            (uid, now))
    return list(affected)


def resweep(con, collection, *, embed_fn=None) -> dict:
    """Re-verify bundles that were admitted with less than full verification.

    The ledger records tier_reached and space_weak precisely so that a bundle
    imported while the embed endpoint was down (T3 skipped) or while the local
    space was unconfirmed can be re-checked once conditions improve. This is
    that check: every still-'imported' chunk of each under-verified bundle is
    recomputed against the local model. A clean bundle is promoted to
    tier_reached=3; a dirty one goes through the normal failure path —
    quarantine, re-queue for local embedding.

    Safe to run any time: verified chunks are skipped (verification is
    permanent per chunk), so a second sweep costs nothing.
    """
    swept, promoted, failed_bundles = 0, 0, []
    for bundle in ledger.bundles_below_tier(con, 3):
        chunk_rows = con.execute(
            "SELECT chunk_id FROM chunk_verification WHERE bundle_id=?",
            (bundle["bundle_id"],)).fetchall()
        chunk_ids = [r["chunk_id"] for r in chunk_rows]
        if not chunk_ids:
            continue
        swept += 1
        outcome = verify_chunks(con, collection, chunk_ids, embed_fn=embed_fn)
        if outcome.get("unavailable"):
            # the endpoint is still down; leave the bundle for the next sweep
            swept -= 1
            continue
        if outcome["failed"] == 0:
            ledger.set_tier_reached(con, bundle["bundle_id"], 3)
            promoted += 1
        else:
            failed_bundles.append(bundle["bundle_id"])
    return {"swept": swept, "promoted": promoted, "failed": failed_bundles}


def annotate(con, hits: list[dict]) -> None:
    """Attach a per-hit verification badge, in place.

    Replaces P0.6's hardcoded 'local ✓' placeholder in the provenance drawer.
    Absence of a row means locally embedded, which is the common case and the
    strongest state — this node computed it itself.
    """
    chunk_ids = [h.get("chunk_id") for h in hits if h.get("chunk_id")]
    states = ledger.verification_states(con, chunk_ids) if chunk_ids else {}
    for hit in hits:
        row = states.get(hit.get("chunk_id"))
        if row is None:
            hit["verification"] = "local"
            hit["producer"] = "local"
        else:
            hit["verification"] = row["state"]      # imported | verified | failed
            hit["producer"] = row["producer"] or "unknown"
            if row["cosine"] is not None:
                hit["verification_cosine"] = row["cosine"]
