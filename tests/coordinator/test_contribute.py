"""Contribute mode, against a live in-process coordinator.

The acceptance scenario the task asks for, end to end: two contributors and one
consumer, work partitioning, a bundle flowing all the way through, credit
accruing, and demand ordering observed.
"""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from nacl import signing

from app import contribute, db, embeddings, spaces
from app.coordinator import service, store
from tests.bundles.helpers import DIM, local_chunks, seed_document, unit_vectors

# The space these bundles are cut in is the default collection's
# (`spaces.gather_components()` with no collection): the test node's Codex
# gutenberg, never whatever `collections/` the working directory holds.
pytestmark = pytest.mark.usefixtures("isolated_config")

PRODUCER_A = signing.SigningKey(bytes(range(32)))
PRODUCER_B = signing.SigningKey(bytes(range(32, 64)))
KEY_A = bytes(PRODUCER_A.verify_key).hex()


class _ClientAdapter:
    """Bridge httpx-style calls in contribute.py onto a FastAPI TestClient."""

    def __init__(self, client):
        self._client = client

    def get(self, url, params=None):
        return self._client.get(_path(url), params=params)

    def post(self, url, content=None, json=None, headers=None):
        return self._client.post(_path(url), content=content, json=json,
                                  headers=headers)

    def close(self):
        pass


def _path(url: str) -> str:
    return "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url


@pytest.fixture
def coordinator(tmp_path):
    universe = [f"gutenberg:{i}" for i in range(1, 6)]
    app = service.create_app(db_path=str(tmp_path / "coord.db"),
                             artifact_dir=str(tmp_path / "artifacts"),
                             candidate_source=lambda space, collection: universe)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def node(con, collection, monkeypatch):
    """A node holding raw documents but no vectors yet.

    The vectors seed_document writes are REMOVED here, so the fixture is
    what it says it is. Clearing embed_done alone is not enough any more:
    upsert_book resumes from vectors already on disk when their text is
    byte-identical to the current chunking (the crash-recovery case of
    invariant 2), so leaving them in place meant contribute had nothing
    left to compute and this fixture silently tested packaging only."""
    for native_id in range(1, 4):
        seed_document(con, collection, f"gutenberg:{native_id}", native_id,
                       n_chunks=3, seed=native_id)
    con.execute("UPDATE documents SET embed_done=0")
    store_db = embeddings._conn(collection)
    if embeddings.TABLE in store_db.table_names():
        store_db.drop_table(embeddings.TABLE)
    # deterministic stand-in for the embed endpoint
    counter = {"calls": 0}

    def fake_embed(texts, **kwargs):
        counter["calls"] += 1
        return unit_vectors(len(texts), seed=len(texts))
    monkeypatch.setattr(embeddings, "_embed_texts", fake_embed)
    return con, collection, counter


def test_contribution_round_uploads_a_bundle(node, coordinator):
    con, collection, _ = node
    result = contribute.run_round(con, collection, "http://coord", count=3,
                                   signing_key=PRODUCER_A,
                                   client=_ClientAdapter(coordinator))
    assert result["ok"], result.get("reason")
    assert result["uploaded"] > 0
    assert result["bundle_id"]

    board = coordinator.get("/leaderboard").json()["leaderboard"]
    assert board[0]["producer"] == KEY_A
    assert board[0]["chunks_accepted"] == result["uploaded"]


def test_contribution_embeds_what_it_leased(node, coordinator):
    """The point of contribute mode: it does the WORK, not just the packaging."""
    con, collection, counter = node
    assert con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"] == 0
    contribute.run_round(con, collection, "http://coord", count=3,
                         signing_key=PRODUCER_A, client=_ClientAdapter(coordinator))
    assert counter["calls"] > 0, "no embedding happened"
    assert con.execute(
        "SELECT COUNT(*) c FROM documents WHERE embed_done=1").fetchone()["c"] > 0


def test_two_contributors_partition_the_work(node, coordinator, tmp_path):
    con, collection, _ = node
    first = contribute.run_round(con, collection, "http://coord", count=2,
                                  signing_key=PRODUCER_A,
                                  client=_ClientAdapter(coordinator))
    second = contribute.run_round(con, collection, "http://coord", count=2,
                                   signing_key=PRODUCER_B,
                                   client=_ClientAdapter(coordinator))
    assert first["ok"] and second["ok"]
    assert first["bundle_id"] != second["bundle_id"]

    board = {row["producer"]: row for row in
             coordinator.get("/leaderboard").json()["leaderboard"]}
    assert len(board) == 2, "both contributors should have credit"


def test_consumer_demand_steers_the_next_contribution(node, coordinator):
    """§7's loop closing: a consumer publishes its lazy queue, and the next
    contributor computes that uid first."""
    con, collection, _ = node
    space_id = spaces.compute_space_id(spaces.gather_components())
    coordinator.post("/demand", json={"space": space_id, "uids": ["gutenberg:3"],
                                       "reported_by": "consumer-1"})

    lease = coordinator.get("/work", params={"space": space_id, "producer": KEY_A,
                                              "n": 1}).json()
    assert lease["uids"] == ["gutenberg:3"]


def test_report_demand_publishes_the_lazy_queue(node, coordinator):
    con, collection, _ = node
    con.execute("INSERT OR REPLACE INTO embed_queue(uid,priority,ts) "
                "VALUES('gutenberg:2',100,0)")
    result = contribute.report_demand(con, collection, "http://coord",
                                       client=_ClientAdapter(coordinator))
    assert result["recorded"] == 1

    space_id = spaces.compute_space_id(spaces.gather_components())
    demand = coordinator.get("/demand", params={"space": space_id}).json()["demand"]
    assert any(d["uid"] == "gutenberg:2" for d in demand)


def test_no_work_available_is_not_an_error(node, coordinator):
    """An exhausted work set must read as 'nothing to do', not as a failure —
    a contributor polls this endlessly."""
    con, collection, _ = node
    app_state = coordinator.app.state
    app_state.candidate_source = lambda space, collection_name: []
    result = contribute.run_round(con, collection, "http://coord", count=4,
                                   signing_key=PRODUCER_A,
                                   client=_ClientAdapter(coordinator))
    assert result["ok"] and result["leased"] == 0


def test_an_unreachable_coordinator_raises_a_recoverable_error(node):
    """The lease simply expires and is reissued, so this never needs to
    checkpoint or roll back."""
    con, collection, _ = node

    class _Broken:
        def get(self, *a, **kw):
            import httpx
            raise httpx.ConnectError("refused")

        def close(self):
            pass

    with pytest.raises(contribute.ContributeError, match="unreachable"):
        contribute.run_round(con, collection, "http://coord", signing_key=PRODUCER_A,
                             client=_Broken())


def test_a_document_with_no_local_text_is_skipped_not_fatal(node, coordinator):
    """A partial contribution is still worth uploading."""
    con, collection, _ = node
    con.execute("UPDATE documents SET path='/nonexistent' WHERE uid='gutenberg:2'")
    result = contribute.run_round(con, collection, "http://coord", count=3,
                                   signing_key=PRODUCER_A,
                                   client=_ClientAdapter(coordinator))
    assert result["ok"], result.get("reason")
    assert result["uids"] >= 1


def test_a_contribution_is_built_under_export_dir(node, coordinator, tmp_path, monkeypatch):
    """Operator, 2026-09-21: exports are made on bulk storage only."""
    from app import config
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", bulk)
    con, collection, _ = node
    result = contribute.run_round(con, collection, "http://coord", count=3,
                                   signing_key=PRODUCER_A,
                                   client=_ClientAdapter(coordinator))
    assert result["ok"], result.get("reason")
    assert (bulk / collection.name).is_dir()
    assert list((bulk / collection.name).iterdir()) == [], "scratch cleaned up"


def test_an_unmounted_export_dir_is_refused_before_the_lease(node, tmp_path, monkeypatch):
    """Not after it: a lease taken and embedded for nothing is work lost."""
    from app import config
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "not-mounted")
    con, collection, _ = node

    class _NoLease:
        def get(self, *a, **kw):
            raise AssertionError("leased before the export location was checked")

        def close(self):
            pass

    with pytest.raises(contribute.ContributeError, match="EXPORT_DIR"):
        contribute.run_round(con, collection, "http://coord", signing_key=PRODUCER_A,
                             client=_NoLease())
