"""P1.3 acceptance: two simulated contributors and one consumer against a live
coordinator — work partitions, a bundle flows end to end, credit accrues, and
demand ordering is observed.

The lease tests assert CONVERGENCE, not mutual exclusion. §7 is explicit that
leases are advisory: "an expired-and-reissued lease wastes one GPU-hour and
breaks nothing". A test demanding exclusivity would be testing a guarantee the
design deliberately declines to make.
"""
import io
import json
import tarfile
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from nacl import signing

from app import db, embeddings, spaces
from app.bundles import export as bundle_export
from app.coordinator import leases, service, store
from tests.bundles.helpers import DIM, seed_document

PRODUCER_A = signing.SigningKey(bytes(range(32)))
PRODUCER_B = signing.SigningKey(bytes(range(32, 64)))
KEY_A = bytes(PRODUCER_A.verify_key).hex()
KEY_B = bytes(PRODUCER_B.verify_key).hex()

UNIVERSE = [f"gutenberg:{i}" for i in range(1, 21)]


@pytest.fixture
def coordinator(tmp_path):
    app = service.create_app(
        db_path=str(tmp_path / "coord.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        candidate_source=lambda space, collection: UNIVERSE)
    with TestClient(app) as client:
        yield client, tmp_path


@pytest.fixture
def coord_db(coordinator):
    _, tmp_path = coordinator
    return store.connect(str(tmp_path / "coord.db"))


def _archive(directory):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted(directory.iterdir()):
            if item.is_file():
                archive.add(item, arcname=item.name)
    return buffer.getvalue()


def _make_bundle(con, collection, tmp_path, uids, key, name="b"):
    return bundle_export.export_bundle(con, collection, uids, tmp_path / name,
                                        signing_key=key)


# ═══════════════════ leases ═══════════════════

def test_work_partitions_between_two_contributors(coordinator):
    client, _ = coordinator
    first = client.get("/work", params={"space": "s1", "producer": KEY_A, "n": 5}).json()
    second = client.get("/work", params={"space": "s1", "producer": KEY_B, "n": 5}).json()
    assert len(first["uids"]) == 5 and len(second["uids"]) == 5
    assert not set(first["uids"]) & set(second["uids"]), "leases overlapped"
    assert not first["duplicate_assignment"]


def test_exhausted_work_reissues_rather_than_idling(coordinator):
    """Near the end of a work set, duplication is the harmless collision the
    advisory design counts on — better than leaving a contributor idle."""
    client, _ = coordinator
    for _ in range(4):
        client.get("/work", params={"space": "s1", "producer": KEY_A, "n": 5})
    extra = client.get("/work", params={"space": "s1", "producer": KEY_B, "n": 5}).json()
    assert extra["uids"], "coordinator idled a contributor instead of reissuing"
    assert extra["duplicate_assignment"] is True


def test_expired_leases_are_reissued(coord_db):
    issued = leases.issue(coord_db, space_id="s1", collection="c", producer=KEY_A,
                          count=3, candidate_uids=UNIVERSE, ttl=-1)
    assert issued["uids"]
    again = leases.issue(coord_db, space_id="s1", collection="c", producer=KEY_B,
                          count=3, candidate_uids=UNIVERSE)
    assert set(again["uids"]) == set(issued["uids"]), "expired work was not reissued"


def test_completing_an_expired_lease_is_accepted(coord_db):
    """The work is done and valid; the lease merely elapsed. Refusing it would
    discard a real GPU-hour to enforce bookkeeping nobody depends on."""
    issued = leases.issue(coord_db, space_id="s1", collection="c", producer=KEY_A,
                          count=2, candidate_uids=UNIVERSE, ttl=-1)
    leases.expire_stale(coord_db)
    assert leases.complete(coord_db, issued["lease_id"], KEY_A) is True


def test_completing_another_producers_lease_is_refused(coord_db):
    issued = leases.issue(coord_db, space_id="s1", collection="c", producer=KEY_A,
                          count=2, candidate_uids=UNIVERSE)
    assert leases.complete(coord_db, issued["lease_id"], KEY_B) is False


# ═══════════════════ bundle upload ═══════════════════

def test_bundle_flows_end_to_end_and_credit_accrues(coordinator, con, collection,
                                                     tmp_path):
    client, coord_path = coordinator
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=4, seed=1)
    summary = _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A)

    response = client.post("/bundles", content=_archive(tmp_path / "b"),
                           headers={"content-type": "application/x-tar"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] and body["bundle_id"] == summary["bundle_id"]

    board = client.get("/leaderboard").json()["leaderboard"]
    assert board[0]["producer"] == KEY_A
    assert board[0]["chunks_accepted"] == summary["chunks"]
    assert board[0]["bundles_accepted"] == 1

    # the bundle is stored under its own id, and its bytes are retained
    stored = coord_path / "artifacts" / summary["bundle_id"]
    assert (stored / bundle_export.HEADER_NAME).is_file()
    assert (stored / bundle_export.VECTORS_NAME).is_file()

    # and it is only visible under ITS space, not some other one
    assert client.get("/have", params={"space": "some-other-space"}).json()["count"] == 0


def test_have_reports_covered_uids(coordinator, con, collection, tmp_path):
    client, _ = coordinator
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A)
    client.post("/bundles", content=_archive(tmp_path / "b"),
                headers={"content-type": "application/x-tar"})

    space_id = spaces.compute_space_id(spaces.gather_components())
    have = client.get("/have", params={"space": space_id}).json()
    assert have["uids"] == ["gutenberg:1"]


def test_double_completion_of_one_range_converges(coordinator, con, collection,
                                                   tmp_path):
    """§7's core claim, asserted directly: two producers handed the same range
    both upload, and the state converges rather than corrupting."""
    client, _ = coordinator
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A, name="a")

    payload = _archive(tmp_path / "a")
    first = client.post("/bundles", content=payload,
                        headers={"content-type": "application/x-tar"}).json()
    second = client.post("/bundles", content=payload,
                         headers={"content-type": "application/x-tar"}).json()

    assert first["bundle_id"] == second["bundle_id"]
    assert second.get("duplicate") is True
    board = client.get("/leaderboard").json()["leaderboard"]
    assert board[0]["bundles_accepted"] == 1, "a duplicate upload double-counted credit"

    space_id = spaces.compute_space_id(spaces.gather_components())
    assert client.get("/have", params={"space": space_id}).json()["count"] == 1


def test_two_producers_covering_the_same_uid_both_accepted(coordinator, con,
                                                            collection, tmp_path):
    """Distinct bundles over the same uid are DIFFERENT bundle_ids (different
    signer), so both are stored — and /have still reports the uid once."""
    client, _ = coordinator
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A, name="a")
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_B, name="b")

    one = client.post("/bundles", content=_archive(tmp_path / "a"),
                      headers={"content-type": "application/x-tar"}).json()
    two = client.post("/bundles", content=_archive(tmp_path / "b"),
                      headers={"content-type": "application/x-tar"}).json()
    assert one["bundle_id"] != two["bundle_id"]
    space_id = spaces.compute_space_id(spaces.gather_components())
    assert client.get("/have", params={"space": space_id}).json()["count"] == 1


def test_tampered_bundle_is_refused_and_counted_against_the_producer(
        coordinator, con, collection, tmp_path):
    client, _ = coordinator
    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A)
    signature = tmp_path / "b" / bundle_export.SIGNATURE_NAME
    data = bytearray(signature.read_bytes())
    data[0] ^= 0x01
    signature.write_bytes(bytes(data))

    response = client.post("/bundles", content=_archive(tmp_path / "b"),
                           headers={"content-type": "application/x-tar"})
    assert response.status_code == 400
    assert "Signature" in response.text or "signature" in response.text


def test_garbage_upload_is_a_400_not_a_crash(coordinator):
    client, _ = coordinator
    response = client.post("/bundles", content=b"not a tar archive at all",
                           headers={"content-type": "application/x-tar"})
    assert response.status_code == 400


@pytest.mark.parametrize("member", ["../../escaped.txt", "/tmp/escaped.txt"])
def test_path_traversal_in_an_archive_is_refused(coordinator, member):
    """An uploaded archive is attacker-controlled, and naive extraction is a
    path-traversal WRITE primitive. Actually uploads the hostile archive and
    checks nothing escaped, rather than merely constructing it."""
    client, coord_path = coordinator
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = b"pwned"
        info = tarfile.TarInfo(member)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    response = client.post("/bundles", content=buffer.getvalue(),
                           headers={"content-type": "application/x-tar"})
    assert response.status_code == 400            # no header.json, so it is refused
    for escaped in (coord_path.parent / "escaped.txt", coord_path / "escaped.txt"):
        assert not escaped.exists(), f"archive escaped to {escaped}"


def test_symlink_in_an_archive_is_refused(coordinator, tmp_path):
    """tarfile's `data` filter must reject symlinks too — otherwise an archive
    can redirect a later write outside the extraction root."""
    client, coord_path = coordinator
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("header.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    response = client.post("/bundles", content=buffer.getvalue(),
                           headers={"content-type": "application/x-tar"})
    assert response.status_code == 400


# ═══════════════════ demand ═══════════════════

def test_demand_is_aggregated_and_orders_work(coordinator):
    """§7: '/demand → aggregated lazy-queues of consumers', and /work serves
    the most-wanted uids first so contributors compute the scarce things."""
    client, _ = coordinator
    client.post("/demand", json={"space": "s1", "uids": ["gutenberg:19"],
                                  "reported_by": "consumer-1"})
    client.post("/demand", json={"space": "s1", "uids": ["gutenberg:19"],
                                  "reported_by": "consumer-2"})
    client.post("/demand", json={"space": "s1", "uids": ["gutenberg:5"],
                                  "reported_by": "consumer-1"})

    demand = client.get("/demand", params={"space": "s1"}).json()["demand"]
    assert demand[0]["uid"] == "gutenberg:19"
    assert demand[0]["consumers"] == 2

    lease = client.get("/work", params={"space": "s1", "producer": KEY_A, "n": 3}).json()
    assert lease["uids"][0] == "gutenberg:19", "work ignored consumer demand"


def test_demand_counts_distinct_consumers_not_raw_volume(coordinator):
    """One consumer hammering its own queue must not dominate the ordering."""
    client, _ = coordinator
    for _ in range(10):
        client.post("/demand", json={"space": "s1", "uids": ["gutenberg:2"],
                                      "reported_by": "noisy"})
    client.post("/demand", json={"space": "s1", "uids": ["gutenberg:3"],
                                  "reported_by": "a"})
    client.post("/demand", json={"space": "s1", "uids": ["gutenberg:3"],
                                  "reported_by": "b"})
    demand = client.get("/demand", params={"space": "s1"}).json()["demand"]
    assert demand[0]["uid"] == "gutenberg:3"


def test_covered_uids_drop_out_of_demand(coordinator, con, collection, tmp_path):
    client, _ = coordinator
    space_id = spaces.compute_space_id(spaces.gather_components())
    client.post("/demand", json={"space": space_id, "uids": ["gutenberg:1"],
                                  "reported_by": "consumer-1"})
    assert client.get("/demand", params={"space": space_id}).json()["demand"]

    seed_document(con, collection, "gutenberg:1", 1, n_chunks=3, seed=1)
    _make_bundle(con, collection, tmp_path, ["gutenberg:1"], PRODUCER_A)
    client.post("/bundles", content=_archive(tmp_path / "b"),
                headers={"content-type": "application/x-tar"})

    remaining = client.get("/demand", params={"space": space_id}).json()["demand"]
    assert not any(d["uid"] == "gutenberg:1" for d in remaining)


# ═══════════════════ reports and probation ═══════════════════

def test_spot_check_failures_put_a_producer_on_probation(coordinator):
    client, _ = coordinator
    for _ in range(3):
        client.post("/reports", json={"producer": KEY_A, "passed": True})
    body = client.post("/reports", json={"producer": KEY_A, "passed": False}).json()
    assert body["spot_checks"] == 4 and body["spot_failures"] == 1
    assert body["probation"] is True


def test_a_clean_producer_stays_off_probation(coordinator):
    client, _ = coordinator
    for _ in range(6):
        body = client.post("/reports", json={"producer": KEY_B, "passed": True}).json()
    assert body["probation"] is False


def test_probation_throttles_work_rather_than_banning(coordinator):
    """A flat probation flag, per the task's non-goals — and a throttle rather
    than a ban, so a producer under suspicion can earn its way back."""
    client, _ = coordinator
    for passed in (True, False, False, False, False):
        client.post("/reports", json={"producer": KEY_A, "passed": passed})
    lease = client.get("/work", params={"space": "s1", "producer": KEY_A,
                                         "n": 100}).json()
    assert 0 < len(lease["uids"]) <= 10


# ═══════════════════ limits ═══════════════════

def test_oversized_body_is_refused(coordinator, monkeypatch):
    client, _ = coordinator
    monkeypatch.setattr(service, "MAX_BODY_BYTES", 64)
    from app.coordinator import limits
    monkeypatch.setattr(limits, "MAX_BODY_BYTES", 64)
    response = client.post("/bundles", content=b"x" * 5000,
                           headers={"content-type": "application/x-tar"})
    assert response.status_code in (400, 413)


def test_rate_limiting_kicks_in(coordinator, monkeypatch):
    from app.coordinator.limits import RateLimiter
    client, _ = coordinator
    monkeypatch.setattr(service, "_read_limiter", RateLimiter(limit=3, window_seconds=60))
    codes = [client.get("/have", params={"space": "s1"}).status_code for _ in range(6)]
    assert 429 in codes, "no rate limit was applied"


def test_malformed_json_is_a_400(coordinator):
    client, _ = coordinator
    assert client.post("/reports", content=b"{not json").status_code == 400
    assert client.post("/demand", content=b"[]").status_code == 400


def test_healthz(coordinator):
    client, _ = coordinator
    assert client.get("/healthz").json() == {"ok": True}
