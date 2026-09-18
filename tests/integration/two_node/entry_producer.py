"""Producer node: discover the corpus, embed it through the mock endpoint,
then contribute rounds against the coordinator until its documents are
covered. Idempotent by construction — a second run discovers nothing new,
skips embedded documents, and gets "no work" from the coordinator.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "/srv")

from app import collections as collections_mod, config, contribute, db, discovery
from app import spaces, worker
from app.bundles.keys import load_signing_key

COORDINATOR = "http://coordinator:8750"


def _already_covered(con, collection) -> bool:
    import httpx
    row = con.execute("SELECT space_id FROM space WHERE id=1").fetchone()
    if not row or not row["space_id"]:
        return False
    try:
        have = httpx.get(f"{COORDINATOR}/have", params={"space": row["space_id"]},
                          timeout=10).json()
    except httpx.HTTPError:
        return False
    local = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"]
    return have.get("count", 0) >= local > 0


def main() -> int:
    key_path = Path("/data/node.key")
    if not key_path.exists():
        import os
        from app.bundles.keys import generate_seed
        key_path.write_bytes(generate_seed())
        os.chmod(key_path, 0o600)      # the loader (rightly) refuses a lax key file
    config.NODE_KEY_PATH = key_path

    collection = collections_mod.default_collection()
    spaces.validate_boot([collection])
    con = db.connect(collection)
    discovery.sync_collection(con, collection)

    # rdf -> fts -> embed, through the worker's own step (it commits what a
    # stage leaves open — entry_consumer.py says why) and the mock endpoint
    for uid in [r["uid"] for r in con.execute(
            "SELECT uid FROM documents WHERE in_scope=1")]:
        for stage in ("rdf", "fts", "embed"):
            row = dict(con.execute("SELECT * FROM documents WHERE uid=?",
                                   (uid,)).fetchone())
            if not row[f"{stage}_done"]:
                worker._execute_step(con, collection, stage, row)
    assert not con.in_transaction, "a stage left the manifest's write lock held"
    total = con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"]
    print(f"producer: {total} documents embedded", flush=True)

    signing_key = load_signing_key()
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            result = contribute.run_round(con, collection, COORDINATOR,
                                           signing_key=signing_key)
            print(f"producer: contribute -> {result}", flush=True)
            if result.get("bundle_id"):
                break
            # "no work" before the consumer's demand lands just means we are
            # early — UNLESS the docs are already covered (the rerun case).
            if result.get("leased") == 0 and _already_covered(con, collection):
                print("producer: work already covered from a previous run", flush=True)
                break
        except contribute.ContributeError as e:
            print(f"producer: coordinator not ready ({e}); retrying", flush=True)
        time.sleep(2)

    print("producer: done, idling", flush=True)
    while True:
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
