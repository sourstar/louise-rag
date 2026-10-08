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

THE THIRD TRIGGER, 05 §5.3 / 03 §3.5 (migration step 11): every chunk a
search returns to its caller is CITED — queued in the manifest's
`verify_queue` by the search itself (`cite`) — and the worker re-embeds the
queue in bounded passes (`drain`), asynchronously, so a search never waits
on the encoder. A miss is a proof only when it REPRODUCES on a second
recompute (03 §3.5); one that does not is left unverified, not condemned.
A reproduced miss is handed to `on_forgery` — the worker writes the proof
and burns the key there (app/hold/forgery.py, app/hold/trust_file.py).
"""
import time
from dataclasses import dataclass

import numpy as np

from .. import embeddings
from ..hold import trust_file
from . import ledger, trust

DRAIN_BATCH = 32                 # chunks per worker pass: a few seconds of encoder
_QUEUE_SCHEMA = ("CREATE TABLE IF NOT EXISTS verify_queue (chunk_id TEXT PRIMARY KEY, ts INTEGER NOT NULL)",)


@dataclass(frozen=True)
class Forgery:
    """A T4 miss that reproduced: what a proof (09 §9.8 `forgery`) needs."""
    chunk_id: str
    uid: str | None
    producer: str | None
    bundle_id: str | None
    text: str
    claimed: np.ndarray
    recomputed: np.ndarray
    cosine: float


key_text = trust_file.key_text      # the ledger keeps bare hex; a proof and the trust file `ed25519:<hex>`


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return 0.0 if denominator == 0 else float(np.dot(a, b) / denominator)

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


def ensure_queue(con) -> None:
    for stmt in _QUEUE_SCHEMA:
        con.execute(stmt)


def cite(con, chunk_ids: list[str]) -> int:
    """05 §5.3: the chunks a search returns are queued for the worker to
    re-embed once. Only imported, unverified ones — a local chunk has
    nothing to verify and a verified one is verified forever. How many
    were queued."""
    targets = pending(con, chunk_ids)
    if not targets:
        return 0
    ensure_queue(con)
    now = int(time.time())
    con.executemany("INSERT OR IGNORE INTO verify_queue(chunk_id, ts) VALUES(?,?)",
                    [(cid, now) for cid in targets])
    return len(targets)


def queued(con, limit: int) -> list[str]:
    """The oldest citations, up to `limit`."""
    ensure_queue(con)
    return [r[0] for r in con.execute(
        "SELECT chunk_id FROM verify_queue ORDER BY ts, chunk_id LIMIT ?", (int(limit),))]


def drain(con, collection, *, limit: int = DRAIN_BATCH, embed_fn=None, on_forgery=None) -> dict:
    """One bounded pass of T4 for the worker: the oldest `limit` citations
    re-embedded and marked, then taken off the queue — verified, failed,
    inconclusive or no longer imported alike (a chunk cited again is
    queued again). An encoder that could not answer leaves the queue as it
    was: deferral is not failure. `queued` in the answer is what remains."""
    targets = queued(con, limit)
    if not targets:
        return {"checked": 0, "verified": 0, "failed": 0, "inconclusive": 0, "quarantined": [], "queued": 0}
    out = verify_chunks(con, collection, targets, embed_fn=embed_fn, on_forgery=on_forgery)
    if not out.get("unavailable"):
        marks = ",".join("?" for _ in targets)
        con.execute(f"DELETE FROM verify_queue WHERE chunk_id IN ({marks})", targets)
    out["queued"] = con.execute("SELECT COUNT(*) FROM verify_queue").fetchone()[0]
    return out


def verify_chunks(con, collection, chunk_ids: list[str], *, embed_fn=None,
                  quarantine_on_failure: bool = True, recheck: bool = True,
                  on_forgery=None) -> dict:
    """Re-embed and check the given chunks. Never raises: a verification pass
    must not be able to break the query that triggered it. `recheck`: a
    miss is recomputed once more and is a failure only when it reproduces
    (03 §3.5) — otherwise it is left unverified and counted `inconclusive`.
    `on_forgery(Forgery)` is called for each reproduced miss."""
    targets = pending(con, chunk_ids)
    if not targets:
        return {"checked": 0, "verified": 0, "failed": 0, "inconclusive": 0, "quarantined": []}

    stored = _texts_and_vectors(collection, targets)
    targets = [cid for cid in targets if cid in stored and stored[cid][0]]
    if not targets:
        return {"checked": 0, "verified": 0, "failed": 0, "inconclusive": 0, "quarantined": []}

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
        return {"checked": 0, "verified": 0, "failed": 0, "inconclusive": 0, "quarantined": [],
                "unavailable": True}

    floor, floor_source = _verify_floor(con)
    states = ledger.verification_states(con, targets)
    verified = failed = inconclusive = 0
    culprits: set[str] = set()
    misses: list[tuple[str, np.ndarray, float]] = []
    for cid, fresh_raw in zip(targets, recomputed):
        fresh = np.asarray(fresh_raw, dtype=np.float32)
        cosine = _cosine(fresh, stored[cid][1])
        if cosine >= floor:
            ledger.set_verified(con, cid, cosine, True)
            verified += 1
        else:
            misses.append((cid, fresh, cosine))
    if misses and recheck:
        # 03 §3.5: "a T4 miss that reproduces on a second recompute" is the
        # proof; a miss that does not is nobody's forgery — the chunk stays
        # imported and is cited again by the next search that returns it
        try:
            again = embed([stored[cid][0] for cid, _, _ in misses])
        except Exception:   # noqa: BLE001 — the encoder went away between the two: not evidence
            inconclusive += len(misses)
            misses = []
        else:
            kept = []
            for (cid, fresh, cosine), raw in zip(misses, again):
                second = np.asarray(raw, dtype=np.float32)
                c2 = _cosine(second, stored[cid][1])
                if c2 >= floor:
                    inconclusive += 1
                else:
                    kept.append((cid, second, min(cosine, c2)))
            misses = kept
    for cid, fresh, cosine in misses:
        ledger.set_verified(con, cid, cosine, False)
        failed += 1
        row = states.get(cid, {})
        producer = row.get("producer")
        if producer:
            culprits.add(producer)
        if on_forgery is not None:
            try:
                on_forgery(Forgery(chunk_id=cid, uid=row.get("uid"), producer=producer,
                                   bundle_id=row.get("bundle_id"), text=stored[cid][0],
                                   claimed=stored[cid][1], recomputed=fresh, cosine=cosine))
            except Exception:   # noqa: BLE001 — a proof that cannot be written never breaks the pass
                pass

    quarantined = []
    requeued: set[str] = set()
    if failed and quarantine_on_failure:
        for producer in sorted(culprits):
            # burned in the peer-wide trust file (03 §3.5): refused from
            # here on by every collection's inbox and every attestation count
            affected = trust.quarantine_key(con, producer)
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
            "inconclusive": inconclusive,
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
