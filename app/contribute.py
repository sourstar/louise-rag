"""Contribute mode: lease work, embed it, pack it, upload it.

architecture-v2.md §7's producer side of the loop. One round is:

    GET /work  ->  ensure those uids are embedded locally  ->  export a signed
    bundle (P1.1)  ->  POST /bundles

DEMAND ORDERING IS THE COORDINATOR'S JOB, NOT THIS MODULE'S. /work already
returns uids ordered by what consumers are asking for, so a contributor that
simply works its lease in order is automatically computing the scarce things
first. Re-sorting here would only fight that.

EVERY BYTE MOVED DRAWS FROM THE TRANSFER BUDGET (CLAUDE.md invariant 5). The
upload is bound to the SEEDING priority class: contributing is background work
and must yield to interactive queries, which is exactly what §5.2's priority
ordering is for.

FAILURE IS CHEAP BY DESIGN. A lease that expires unfinished is re-issued to
somebody else and the work is idempotent, so this module never needs to
checkpoint, roll back, or hold a lock. The worst case is one wasted GPU-hour,
which §7 explicitly accepts in exchange for skipping BOINC's hardest machinery.
"""
import io
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

from . import chunking, config, db, embeddings, plugin_io
from .bundles import export as bundle_export
from .bundles.keys import load_signing_key, public_key_hex
from .transfer.bucket import Priority
from .transfer.manager import Direction, get_manager

DEFAULT_LEASE_SIZE = 64
UPLOAD_TIMEOUT = 300


class ContributeError(RuntimeError):
    """A contribution round could not complete. Always recoverable: the lease
    simply expires and is reissued."""


def _archive(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(directory.iterdir()):
            if item.is_file():
                archive.add(item, arcname=item.name)
    return buffer.getvalue()


def _ensure_embedded(con, collection, uids: list[str], *, stop=None) -> list[str]:
    """Embed any leased document this node has not embedded yet.

    Returns the uids that ended up with vectors — a document whose raw text is
    missing locally is skipped rather than failing the round, because a partial
    contribution is still worth uploading.
    """
    ready = []
    for uid in uids:
        if stop is not None and stop.is_set():
            break
        row = con.execute(
            "SELECT uid, native_id, path, lang, embed_done FROM documents WHERE uid=?",
            (uid,)).fetchone()
        if row is None:
            continue
        if row["embed_done"]:
            ready.append(uid)
            continue
        try:
            from . import layers as layers_mod
            plugin = collection.plugin_instance()
            native_id = int(row["native_id"])
            chapters = plugin_io.sections(plugin, row)
            chunks = layers_mod.chunks_for_document(collection, row, chapters)
            if not chunks:
                continue
            embeddings.upsert_book(native_id, row["lang"], chunks, collection,
                                   lock_con=con)
            con.execute(
                "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? "
                "WHERE uid=?", (config.EMBED_VERSION, int(time.time()), uid))
            ready.append(uid)
        except Exception as e:      # noqa: BLE001 - one bad document must not end the round
            db.log_event(con, "warn", "contribute",
                         f"{uid}: could not embed ({type(e).__name__}: {e})"[:300])
    return ready


def run_round(con, collection, coordinator_url: str, *, space_id: str | None = None,
              count: int = DEFAULT_LEASE_SIZE, stop=None, signing_key=None,
              client=None) -> dict:
    """One lease -> embed -> pack -> upload cycle."""
    key = signing_key if signing_key is not None else load_signing_key()
    producer = public_key_hex(key)

    if space_id is None:
        row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
        space_id = row["space_id"] if row else None
    if not space_id:
        raise ContributeError("this collection has no pinned space_id")

    owns_client = client is None
    http = client or httpx.Client(timeout=UPLOAD_TIMEOUT)
    try:
        response = http.get(f"{coordinator_url.rstrip('/')}/work",
                            params={"space": space_id, "collection": collection.name,
                                    "producer": producer, "n": count})
        response.raise_for_status()
        lease = response.json()
        uids = lease.get("uids") or []
        if not uids:
            return {"ok": True, "leased": 0, "reason": lease.get("reason", "no work")}

        ready = _ensure_embedded(con, collection, uids, stop=stop)
        if not ready:
            return {"ok": False, "leased": len(uids), "uploaded": 0,
                    "reason": "no leased document could be embedded locally"}

        with tempfile.TemporaryDirectory() as scratch:
            bundle_dir = Path(scratch) / "bundle"
            summary = bundle_export.export_bundle(con, collection, ready, bundle_dir,
                                                   signing_key=key)
            payload = _archive(bundle_dir)

            # Contributing is background work: it yields to interactive traffic.
            manager = get_manager()
            with manager.bind(Priority.SEEDING, Direction.UP, stop=stop):
                from .acquire import budget
                budget.acquire_bytes(len(payload))
                upload = http.post(f"{coordinator_url.rstrip('/')}/bundles",
                                   content=payload,
                                   headers={"content-type": "application/x-tar"})
        if upload.status_code >= 400:
            return {"ok": False, "leased": len(uids), "uploaded": 0,
                    "reason": f"coordinator refused the bundle: {upload.text[:300]}"}
        result = upload.json()
        db.log_event(con, "info", "contribute",
                     f"uploaded {summary['chunks']} chunks across {len(ready)} "
                     f"document(s): {result.get('bundle_id', '?')[:16]}…")
        return {"ok": True, "leased": len(uids), "uploaded": summary["chunks"],
                "uids": len(ready), "bundle_id": result.get("bundle_id"),
                "duplicate": bool(result.get("duplicate"))}
    except httpx.HTTPError as e:
        raise ContributeError(f"coordinator unreachable: {type(e).__name__}") from None
    finally:
        if owns_client:
            http.close()


def report_demand(con, collection, coordinator_url: str, *, space_id: str | None = None,
                  limit: int = 500, client=None) -> dict:
    """Publish this node's lazy-embed queue so the network knows what it wants.

    This is the consumer half of §7's demand feed, and it is what gives a
    contributor's own queue scheduling priority.
    """
    if space_id is None:
        row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
        space_id = row["space_id"] if row else None
    if not space_id:
        raise ContributeError("this collection has no pinned space_id")

    uids = [r["uid"] for r in con.execute(
        "SELECT uid FROM embed_queue ORDER BY priority DESC, ts LIMIT ?", (limit,))]
    if not uids:
        return {"reported": 0}

    node_id = db.node_id(con)      # F1-D2: never the collection name — every
    owns_client = client is None   # node defaulting to "gutenberg" collapsed
                                   # N consumers into one in the demand feed
    http = client or httpx.Client(timeout=60)
    try:
        response = http.post(f"{coordinator_url.rstrip('/')}/demand",
                             json={"space": space_id, "uids": uids,
                                   "reported_by": node_id})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as e:
        raise ContributeError(f"coordinator unreachable: {type(e).__name__}") from None
    finally:
        if owns_client:
            http.close()
