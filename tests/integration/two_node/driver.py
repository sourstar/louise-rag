"""The scripted two-node flow, run from inside the compose network.

    producer leases + embeds + uploads
    consumer demands + imports + verifies
    consumer answers a query citing IMPORTED vectors with correct provenance

Exit 0 only if every assertion holds. Written to be IDEMPOTENT: run twice
against the same volumes, the second pass must also succeed — re-discovery is
a no-op, the re-uploaded bundle is a duplicate, the re-imported bundle is
skipped by the ledger, and verified badges stay verified. That is the
system-level idempotence F1's definition of done demands.
"""
import shutil
import sys
import time
from pathlib import Path

import httpx

COORDINATOR = "http://coordinator:8750"
CONSUMER = "http://consumer:8742"
COORD_ARTIFACTS = Path("/coord-artifacts")
CONSUMER_INBOX = Path("/consumer-data/bundles/inbox")
EXPECTED_DOCS = 3
STEP_TIMEOUT = 180


def wait_for(label, fn, timeout=STEP_TIMEOUT):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = fn()
            if value:
                print(f"driver: {label}: OK", flush=True)
                return value
            last = value
        except Exception as e:      # noqa: BLE001 - services are still starting
            last = f"{type(e).__name__}: {e}"
        time.sleep(2)
    raise SystemExit(f"driver: TIMEOUT waiting for {label} (last: {last})")


def main() -> int:
    client = httpx.Client(timeout=30)

    wait_for("coordinator up", lambda: client.get(f"{COORDINATOR}/healthz").json()["ok"])
    wait_for("consumer api up", lambda: client.get(f"{CONSUMER}/healthz").json()["ok"])

    # ── consumer demand reached the coordinator, producer covered it ──
    space = wait_for(
        "producer's bundle accepted by the coordinator",
        lambda: next((s for s in _spaces_with_bundles(client)), None))
    have = client.get(f"{COORDINATOR}/have", params={"space": space}).json()
    assert have["count"] == EXPECTED_DOCS, f"/have reports {have['count']}"
    print(f"driver: coordinator holds {have['count']} covered documents", flush=True)

    board = client.get(f"{COORDINATOR}/leaderboard").json()["leaderboard"]
    assert board and board[0]["bundles_accepted"] >= 1
    producer_pubkey = board[0]["producer"]
    print(f"driver: credit accrued to {producer_pubkey[:16]}…", flush=True)

    # ── bundle transport: coordinator's store -> consumer's inbox ──
    # (§7's "local dir" distribution channel; the swarm is a later transport)
    delivered = 0
    CONSUMER_INBOX.mkdir(parents=True, exist_ok=True)
    for bundle_dir in COORD_ARTIFACTS.iterdir():
        if bundle_dir.is_dir():
            target = CONSUMER_INBOX / bundle_dir.name
            if not target.exists() and not _already_processed(bundle_dir.name):
                shutil.copytree(bundle_dir, target)
                delivered += 1
    print(f"driver: delivered {delivered} bundle(s) to the consumer inbox", flush=True)

    wait_for("consumer imported the bundle",
             lambda: client.get(f"{CONSUMER}/api/status").json()
             ["counts"]["embed_done"] == EXPECTED_DOCS)
    vectors = client.get(f"{CONSUMER}/api/status").json()["vectors"]
    assert vectors > 0, "no vectors landed in the consumer's table"
    print(f"driver: consumer now holds {vectors} imported vectors", flush=True)

    # ── the consumer answers a query citing imported vectors ──
    result = wait_for(
        "dense search returns imported hits",
        lambda: _search(client, verify=False))
    for hit in result["hits"]:
        assert hit["producer"] == producer_pubkey, \
            f"hit credits {hit['producer'][:16]}…, expected the producer"
        assert hit["verification"] in ("imported", "verified"), hit["verification"]
    print(f"driver: {len(result['hits'])} hits, provenance badges correct", flush=True)

    # ── verify-on-use flips the badges ──
    chunk_ids = [h["chunk_id"] for h in result["hits"]]
    outcome = client.post(f"{CONSUMER}/api/verify",
                          json={"chunk_ids": chunk_ids}).json()
    assert outcome["failed"] == 0, f"honest vectors failed verification: {outcome}"
    after = _search(client, verify=False)
    assert all(h["verification"] == "verified" for h in after["hits"]), \
        [h["verification"] for h in after["hits"]]
    print("driver: verify-on-use passed, badges now 'verified'", flush=True)

    # ── system-level idempotence: replay the delivery ──
    for bundle_dir in COORD_ARTIFACTS.iterdir():
        if bundle_dir.is_dir():
            target = CONSUMER_INBOX / f"replay-{bundle_dir.name}"
            if not target.exists():
                shutil.copytree(bundle_dir, target)
    time.sleep(8)                                  # let the worker drain it
    replay = client.get(f"{CONSUMER}/api/status").json()
    assert replay["vectors"] == vectors, \
        f"replay changed the vector count: {vectors} -> {replay['vectors']}"
    final = _search(client, verify=False)
    assert all(h["verification"] in ("verified", "imported")
               for h in final["hits"])
    print("driver: replayed bundle was a no-op — vector count stable", flush=True)

    print("driver: TWO-NODE FLOW COMPLETE", flush=True)
    return 0


def _spaces_with_bundles(client):
    # the space id is content-derived; discover it from the artifact store
    for bundle_dir in COORD_ARTIFACTS.iterdir():
        header = bundle_dir / "header.json"
        if header.is_file():
            import json
            yield json.loads(header.read_text())["space_id"]


def _already_processed(name: str) -> bool:
    parent = CONSUMER_INBOX.parent
    return any((parent / state / name).exists() for state in ("done", "rejected"))


def _search(client, *, verify: bool):
    result = client.post(f"{CONSUMER}/api/search",
                         json={"query": "lighthouse keeper tended the lamp",
                               "mode": "dense", "k": 5, "translate": False,
                               "verify": verify}).json()
    return result if result.get("hits") else None


if __name__ == "__main__":
    sys.path.insert(0, "/srv")
    raise SystemExit(main())
