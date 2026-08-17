"""The coordinator — architecture-v2.md §7's four endpoints, deliberately boring.

    GET  /work?space=...&n=1000   lease a uid range, TTL 24h (advisory)
    POST /bundles                 signed upload, runs T0-T2 immediately
    GET  /have?space=...          uid ranges already provided
    GET  /demand?space=...        aggregated lazy-queues of consumers

Plus POST /reports, which the P1.3 task adds so importers can feed back
spot-check outcomes, and POST /demand so consumers can publish the queues that
GET /demand aggregates.

A SEPARATE APP, not a router on app/api.py. §7 describes a standalone service
that "holds no bulk data", and a node must be able to run without one — or to
point at somebody else's. It shares libraries with the node (app/verify,
app/bundles/format) but no state.

WHY ONLY T0-T2 HERE. The coordinator has no corpus and no embed endpoint, so it
cannot re-derive chunk text and cannot recompute a vector. T3 is structurally
impossible for it and the ladder's max_tier makes that explicit rather than
silently skipping. §7 says exactly this: "POST /bundles ... triggers T0-T2
immediately". T3 and T4 are the importing node's job, and their outcomes come
back through POST /reports.

AUTHENTICATION IS BY SIGNATURE, NOT BY TOKEN. A bundle already carries an
Ed25519 signature over its own bytes, so the upload authenticates itself: the
producer identity IS the key that signed it. There is no separate credential to
issue, leak or rotate.
"""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from . import leases, store
from .limits import (MAX_BODY_BYTES, MAX_JSON_BYTES, RateLimiter, client_key,
                     read_capped_body)

DEFAULT_LEASE_SIZE = 1000
MAX_LEASE_SIZE = 10_000

_upload_limiter = RateLimiter(limit=30, window_seconds=60)
_read_limiter = RateLimiter(limit=300, window_seconds=60)
_report_limiter = RateLimiter(limit=120, window_seconds=60)


def create_app(*, db_path: str | None = None, artifact_dir: str | None = None,
               candidate_source=None) -> FastAPI:
    """Build the coordinator app.

    `candidate_source(space_id, collection) -> list[str]` supplies the uid
    universe work is leased from. Injected rather than hardcoded because a
    coordinator has no corpus of its own: in deployment it is fed from the
    space's published uid manifest, and in tests it is a list.
    """
    db_path = db_path or os.getenv("COORDINATOR_DB", "coordinator.db")
    artifact_dir = Path(artifact_dir or os.getenv("COORDINATOR_ARTIFACTS", "bundles"))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="louise-rag coordinator")
    app.state.db_path = db_path
    app.state.artifact_dir = artifact_dir
    app.state.candidate_source = candidate_source or (lambda space, collection: [])

    def _con():
        return store.connect(app.state.db_path)

    # ---------------- GET /work ----------------

    @app.get("/work")
    def get_work(request: Request, space: str = Query(..., min_length=1, max_length=128),
                 collection: str = Query("", max_length=128),
                 producer: str = Query(..., min_length=16, max_length=128),
                 n: int = Query(DEFAULT_LEASE_SIZE, ge=1, le=MAX_LEASE_SIZE)):
        """Lease a uid range. Advisory, TTL-bounded, and ordered by demand.

        Demand-first ordering is what makes §7's "contributors' demand-sets get
        scheduling priority" real: the scarce things get computed first.
        """
        _read_limiter.check(client_key(request))
        con = _con()
        if store.is_on_probation(con, producer):
            # Probation is a flat flag, not a ban: a producer under suspicion
            # still gets work, just less of it, so it can earn its way back.
            n = max(1, n // 10)

        wanted = [row["uid"] for row in store.aggregated_demand(con, space, limit=n * 4)]
        universe = list(app.state.candidate_source(space, collection))
        ordered = wanted + [u for u in universe if u not in set(wanted)]
        return leases.issue(con, space_id=space, collection=collection,
                            producer=producer, count=n, candidate_uids=ordered)

    # ---------------- POST /bundles ----------------

    @app.post("/bundles")
    async def post_bundle(request: Request):
        """Accept a signed bundle archive, verify it, store its metadata.

        The body is a tar of the bundle directory. Extraction uses tarfile's
        `data` filter, which refuses absolute paths, parent-directory escapes,
        symlinks and device nodes — an uploaded archive is attacker-controlled
        and naive extraction is a path-traversal write primitive.
        """
        _upload_limiter.check(client_key(request))
        body = await read_capped_body(request, MAX_BODY_BYTES)
        con = _con()

        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "bundle"
            root.mkdir()
            try:
                with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
                    archive.extractall(root, filter="data")
            except (tarfile.TarError, ValueError, OSError) as e:
                raise HTTPException(400, f"malformed bundle archive: {type(e).__name__}")

            inner = root
            entries = [p for p in root.iterdir()]
            if len(entries) == 1 and entries[0].is_dir():
                inner = entries[0]

            result = _verify_upload(inner)
            if not result["ok"]:
                if result.get("producer"):
                    store.bump_credit(con, result["producer"], rejected=1)
                raise HTTPException(400, result["error"])

            bundle_id = result["bundle_id"]
            if con.execute("SELECT 1 FROM bundles WHERE bundle_id=?",
                           (bundle_id,)).fetchone():
                # Idempotent: two producers handed the same lease both upload,
                # and the second is a no-op. This is the harmless collision the
                # advisory-lease design counts on.
                return {"ok": True, "bundle_id": bundle_id, "duplicate": True}

            destination = app.state.artifact_dir / bundle_id
            destination.mkdir(parents=True, exist_ok=True)
            for item in inner.iterdir():
                if item.is_file():
                    (destination / item.name).write_bytes(item.read_bytes())

            con.execute(
                "INSERT INTO bundles(bundle_id,space_id,collection,producer,uids,"
                "chunk_count,tier_reached,verdict,accepted_at,artifact_path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (bundle_id, result["space_id"], result["collection"],
                 result["producer"], json.dumps(result["uids"]),
                 result["chunk_count"], result["tier_reached"], result["verdict"],
                 int(time.time()), str(destination)))
            store.bump_credit(con, result["producer"], chunks=result["chunk_count"],
                              accepted=1)
            # F1-D3: leases are matched by coverage, not by a lease id — the
            # wire format carries none, and the old code here keyed completion
            # on that nonexistent field, so fulfilled leases sat open for 24h.
            leases.complete_covered(con, space_id=result["space_id"],
                                     producer=result["producer"],
                                     covered=result["uids"])
            return {"ok": True, "bundle_id": bundle_id,
                    "tier_reached": result["tier_reached"],
                    "chunks": result["chunk_count"], "verdict": result["verdict"]}

    # ---------------- GET /have ----------------

    @app.get("/have")
    def get_have(request: Request, space: str = Query(..., min_length=1, max_length=128)):
        _read_limiter.check(client_key(request))
        uids = store.covered_uids(_con(), space)
        return {"space": space, "count": len(uids), "uids": uids[:10_000]}

    # ---------------- demand ----------------

    class DemandBody(BaseModel):
        space: str
        uids: list[str]
        reported_by: str

    @app.post("/demand")
    async def post_demand(request: Request):
        """Consumers publish their lazy queues; GET /demand aggregates them."""
        _report_limiter.check(client_key(request))
        raw = await read_capped_body(request, MAX_JSON_BYTES)
        try:
            body = DemandBody(**json.loads(raw))
        except Exception:
            raise HTTPException(400, "malformed demand report") from None
        if len(body.uids) > 10_000:
            raise HTTPException(400, "at most 10000 uids per report")
        return {"recorded": store.report_demand(_con(), body.space, body.uids,
                                                 body.reported_by)}

    @app.get("/demand")
    def get_demand(request: Request, space: str = Query(..., min_length=1, max_length=128),
                   limit: int = Query(500, ge=1, le=5000)):
        _read_limiter.check(client_key(request))
        return {"space": space,
                "demand": store.aggregated_demand(_con(), space, limit=limit)}

    # ---------------- POST /reports ----------------

    class ReportBody(BaseModel):
        producer: str
        bundle_id: str | None = None
        passed: bool

    @app.post("/reports")
    async def post_report(request: Request):
        """Importers feed back T3/T4 outcomes, which is the only way the
        coordinator learns anything it structurally cannot check itself."""
        _report_limiter.check(client_key(request))
        raw = await read_capped_body(request, MAX_JSON_BYTES)
        try:
            body = ReportBody(**json.loads(raw))
        except Exception:
            raise HTTPException(400, "malformed report") from None
        return store.record_spot_check(_con(), body.producer, passed=body.passed)

    # ---------------- observability ----------------

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/leaderboard")
    def get_leaderboard(request: Request, limit: int = Query(20, ge=1, le=200)):
        _read_limiter.check(client_key(request))
        return {"leaderboard": store.leaderboard(_con(), limit=limit)}

    return app


def _verify_upload(directory: Path) -> dict:
    """Signature, then T0-T2. Never raises: a malformed upload is a 400, not a
    stack trace."""
    from ..bundles import export as bundle_export
    from ..bundles.format import (bundle_id_of, parse_header, parse_possession,
                                   seq_of, signed_payload, sort_rows, uid_of,
                                   verify_signature)
    from ..verify import Tier, run_ladder
    from ..verify import artifact as artifact_mod

    try:
        header_bytes = (directory / bundle_export.HEADER_NAME).read_bytes()
        signature = (directory / bundle_export.SIGNATURE_NAME).read_bytes()
        parquet_bytes = (directory / bundle_export.VECTORS_NAME).read_bytes()
        possession_bytes = (directory / bundle_export.POSSESSION_NAME).read_bytes()
    except OSError:
        return {"ok": False, "error": "bundle archive is missing required files"}

    try:
        header = parse_header(header_bytes)
        payload = signed_payload(header_bytes,
                                 hashlib.sha256(parquet_bytes).digest(),
                                 hashlib.sha256(possession_bytes).digest())
        verify_signature(payload, signature, header.producer_pubkey)
        parse_possession(possession_bytes)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300],
                "producer": None}

    bundle_id = bundle_id_of(payload)
    try:
        import numpy as np
        import pyarrow.parquet as pq
        table = pq.read_table(directory / bundle_export.VECTORS_NAME)
        chunk_ids = table.column("chunk_id").to_pylist()
        vectors = [np.asarray(v, dtype=np.float32)
                   for v in table.column("vector").to_pylist()]
    except Exception as e:
        return {"ok": False, "error": f"unreadable vectors.parquet ({type(e).__name__})",
                "producer": header.producer_pubkey}

    search = [Path(os.getenv("SPACE_ARTIFACT_DIR",
                             Path(os.getenv("CONFIG_DIR", ".")) / "spaces"))]
    space_artifact = None
    try:
        space_artifact = artifact_mod.find(header.space_id, search)
    except Exception:
        space_artifact = None

    # max_tier=T2: the coordinator has no corpus and no embed endpoint, so T3 is
    # structurally impossible here rather than merely skipped.
    verdict = run_ladder(chunk_ids, vectors, dim=header.dim, dtype=header.dtype,
                         artifact=space_artifact, header=header, texts=None,
                         embed_fn=None, uid_of=uid_of, seq_of=seq_of,
                         max_tier=Tier.T2)
    if verdict.failed:
        return {"ok": False,
                "error": "verification failed — " + "; ".join(
                    f"{r.tier.name}: {r.detail}" for r in verdict.failures)[:400],
                "producer": header.producer_pubkey}

    tier = verdict.tier_reached
    return {"ok": True, "bundle_id": bundle_id, "space_id": header.space_id,
            "collection": header.collection, "producer": header.producer_pubkey,
            "uids": list(header.uids), "chunk_count": header.chunk_count,
            "tier_reached": int(tier) if tier is not None else None,
            "verdict": verdict.summary()[:500],
            "lease_id": None}


app = None    # created by main(); import-time construction would touch the filesystem


def main() -> int:
    import uvicorn
    service = create_app()
    uvicorn.run(service, host="0.0.0.0", port=int(os.getenv("COORDINATOR_PORT", "8750")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
