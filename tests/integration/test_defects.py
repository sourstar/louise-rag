"""Regression tests for the cross-module defects F1 found.

Written FAILING-FIRST against the pre-fix behavior; each names its finding in
docs/notes/phase1-integration-notes.md. These are the seams no module-level suite
could see: each half of the behavior was correct in isolation.
"""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, embeddings, spaces, worker
from app.bundles import export as bundle_export
from app.bundles import importer, ledger, verify_on_use
from app.coordinator import leases, service, store
from tests.bundles.helpers import make_collection
from tests.integration.conftest import (PRODUCER_KEY, mirror_corpus, model_embed,
                                         seed_node)

DOCS = {"gutenberg:1": 1, "gutenberg:2": 2}


@pytest.fixture
def imported_pair(producer, consumer, tmp_path, monkeypatch):
    """Producer's bundle covering two documents, imported by the consumer."""
    producer_con, producer_col = producer
    consumer_con, consumer_col = consumer
    rows = seed_node(producer_con, producer_col, DOCS)
    mirror_corpus(producer_col, consumer_con, consumer_col, DOCS)
    monkeypatch.setattr(embeddings, "embed_texts", model_embed)
    bundle_export.export_bundle(producer_con, producer_col, list(DOCS),
                                 tmp_path / "b", signing_key=PRODUCER_KEY)
    result = importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")
    assert result["ok"], result.get("error")
    return consumer_con, consumer_col, rows, result["producer"]


# ═══ F1-D1: quarantine must purge the producer's vectors, not just the key ═══

def test_quarantine_requeues_every_document_the_producer_wrote(imported_pair):
    """§6: "quarantine the key, re-embed locally". Before the fix, verify-on-use
    quarantined the KEY and stopped: the producer's other vectors — including
    whole documents the failed sample never touched — stayed live in the index
    and kept surfacing in retrieval. Trust said "never again"; the index said
    "still serving". The defined outcome is that every document that producer's
    bundles wrote is re-queued for LOCAL embedding.
    """
    consumer_con, consumer_col, rows, producer_id = imported_pair
    poisoned_id = rows[0]["id"]
    honest = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    corrupted = dict(honest)
    corrupted[rows[0]["text"]] = model_embed([rows[0]["text"]], salt="poison")[0]

    outcome = verify_on_use.verify_chunks(
        consumer_con, consumer_col, [r["id"] for r in rows],
        embed_fn=lambda batch: [corrupted[t] for t in batch])
    assert outcome["quarantined"] == [producer_id]

    docs = {r["uid"]: dict(r) for r in consumer_con.execute(
        "SELECT uid, embed_done FROM documents")}
    for uid in DOCS:
        assert docs[uid]["embed_done"] == 0, \
            f"{uid} still marked embedded with a quarantined producer's vectors"
    queued = {r["uid"] for r in consumer_con.execute("SELECT uid FROM embed_queue")}
    assert queued == set(DOCS), "quarantined documents were not queued for re-embedding"


def test_local_reembed_after_quarantine_restores_local_provenance(imported_pair):
    consumer_con, consumer_col, rows, producer_id = imported_pair
    corrupted = {r["text"]: np.asarray(r["vector"], dtype=np.float32) for r in rows}
    corrupted[rows[0]["text"]] = model_embed([rows[0]["text"]], salt="poison")[0]
    verify_on_use.verify_chunks(consumer_con, consumer_col, [r["id"] for r in rows],
                                 embed_fn=lambda batch: [corrupted[t] for t in batch])

    for row in consumer_con.execute("SELECT * FROM documents"):
        worker.stage_embed(consumer_con, row, consumer_col)
    hits = [{"chunk_id": r["id"]} for r in rows]
    verify_on_use.annotate(consumer_con, hits)
    assert all(h["verification"] == "local" for h in hits)


# ═══ F1-D2: every consumer must have a distinct node identity ═══

def test_demand_reports_from_two_nodes_count_as_two_consumers(tmp_path, monkeypatch,
                                                               confirmed_space):
    """The coordinator counts DISTINCT reporters precisely so one noisy node
    cannot dominate demand ordering. Before the fix, report_demand defaulted
    the node id to the COLLECTION NAME — so every node in the network reported
    as "gutenberg" and N consumers collapsed into one, silently defeating the
    anti-gaming design it fed into.
    """
    from app import contribute
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    app = service.create_app(db_path=str(tmp_path / "coord.db"),
                             artifact_dir=str(tmp_path / "artifacts"),
                             candidate_source=lambda s, c: [])
    space_id = spaces.compute_space_id(spaces.gather_components())

    class _Adapter:
        def __init__(self, client):
            self._client = client

        def post(self, url, json=None, **kw):
            return self._client.post("/" + url.split("/", 3)[-1], json=json)

        def close(self):
            pass

    with TestClient(app) as client:
        for name in ("node-a", "node-b"):
            collection = make_collection(tmp_path / name, name="gutenberg")
            con = db.connect(collection)
            con.execute("INSERT OR REPLACE INTO space(id,space_id,dim,components,"
                        "pinned_at) VALUES(1,?,8,'{}',0)", (space_id,))
            con.execute("INSERT OR REPLACE INTO embed_queue(uid,priority,ts) "
                        "VALUES('gutenberg:9',100,0)")
            contribute.report_demand(con, collection, "http://coord",
                                      client=_Adapter(client))
        demand = client.get("/demand", params={"space": space_id}).json()["demand"]
    assert demand and demand[0]["uid"] == "gutenberg:9"
    assert demand[0]["consumers"] == 2, \
        "two distinct nodes were counted as one consumer"


def test_node_id_is_generated_once_and_persisted(tmp_path, monkeypatch):
    collection = make_collection(tmp_path / "n", name="gutenberg")
    con = db.connect(collection)
    first = db.node_id(con)
    assert first and first != collection.name
    assert db.node_id(con) == first, "node id must be stable across calls"
    fresh_con = db.connect(collection)
    assert db.node_id(fresh_con) == first, "node id must survive reconnect"


# ═══ F1-D3: an accepted upload must close the producer's matching lease ═══

def test_upload_completes_the_lease_it_fulfils(producer, tmp_path, monkeypatch):
    """Before the fix, nothing ever completed a lease: /bundles had dead
    completion code keyed on a lease_id the wire format (correctly) does not
    carry, and contribute.run_round never called back. Every fulfilled lease
    sat open until TTL expiry, so lease listings lied for 24h about work that
    finished in minutes. Fix is server-side, with no wire change: an accepted
    upload completes the uploader's open leases whose uids it covers.
    """
    import io
    import tarfile
    producer_con, producer_col = producer
    seed_node(producer_con, producer_col, DOCS)
    app = service.create_app(db_path=str(tmp_path / "coord.db"),
                             artifact_dir=str(tmp_path / "artifacts"),
                             candidate_source=lambda s, c: list(DOCS))
    summary = bundle_export.export_bundle(producer_con, producer_col, list(DOCS),
                                           tmp_path / "b", signing_key=PRODUCER_KEY)
    space_id = summary and spaces.compute_space_id(spaces.gather_components())

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for item in sorted((tmp_path / "b").iterdir()):
            archive.add(item, arcname=item.name)

    with TestClient(app) as client:
        lease = client.get("/work", params={"space": space_id,
                                             "producer": summary["producer"],
                                             "n": 2}).json()
        assert set(lease["uids"]) <= set(DOCS)
        upload = client.post("/bundles", content=buffer.getvalue(),
                             headers={"content-type": "application/x-tar"})
        assert upload.status_code == 200

    coord_db = store.connect(str(tmp_path / "coord.db"))
    row = coord_db.execute("SELECT state FROM leases WHERE lease_id=?",
                            (lease["lease_id"],)).fetchone()
    assert row["state"] == "completed", \
        "a fulfilled lease stayed open until TTL expiry"


# ═══ F1-D6: a consumer-role node must be able to SEARCH what it imported ═══

def test_imported_vectors_are_searchable_with_embed_mode_off(producer, consumer,
                                                              tmp_path, monkeypatch):
    """§14's consumer role is import-only: embed_mode=off, vectors arrive from
    the network. Before the fix, retrieval gated its dense leg on
    embed_mode != "off" — conflating "don't SPEND on embedding" with "don't
    USE embeddings" — so the one node type built entirely around imported
    vectors was the one node type that refused to search them. Found by the
    two-node flow: the consumer imported everything and then answered dense
    queries with nothing.
    """
    from app import retrieval
    producer_con, producer_col = producer
    consumer_con, consumer_col = consumer
    rows = seed_node(producer_con, producer_col, DOCS)
    mirror_corpus(producer_col, consumer_con, consumer_col, DOCS)
    monkeypatch.setattr(embeddings, "embed_texts", model_embed)
    bundle_export.export_bundle(producer_con, producer_col, list(DOCS),
                                 tmp_path / "b", signing_key=PRODUCER_KEY)
    assert importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")["ok"]

    db.set_scope(consumer_con, {"embed_mode": "off"})
    result = retrieval.search(consumer_con, rows[0]["text"][:80], k=4, mode="dense",
                               translate=False, collection=consumer_col)
    assert result["hits"], "an import-only node could not search its own vectors"
    assert all(h["verification"] == "imported" for h in result["hits"])

    # ...while the worker still refuses to SPEND: embed_mode=off must keep
    # every embedding stage off, exactly as before
    scope = db.get_scope(consumer_con)
    stage, row = worker._next_book(consumer_con, scope)
    assert stage != "embed"


# ═══ F1-D5 (minor): import must clear the demand it satisfies ═══

def test_import_clears_the_lazy_queue_entries_it_satisfies(producer, consumer,
                                                            tmp_path, monkeypatch):
    """Import is an alternative implementation of the embed stage, and the
    embed stage clears its document's lazy-queue row. Before the fix the
    import path didn't — so an imported document's demand entry lingered and
    kept being re-published to the coordinator as need the network had
    already met."""
    producer_con, producer_col = producer
    consumer_con, consumer_col = consumer
    seed_node(producer_con, producer_col, DOCS)
    mirror_corpus(producer_col, consumer_con, consumer_col, DOCS)
    monkeypatch.setattr(embeddings, "embed_texts", model_embed)
    for uid in DOCS:                                   # demand exists BEFORE import
        consumer_con.execute("INSERT OR REPLACE INTO embed_queue(uid,priority,ts) "
                             "VALUES(?,100,0)", (uid,))
    bundle_export.export_bundle(producer_con, producer_col, list(DOCS),
                                 tmp_path / "b", signing_key=PRODUCER_KEY)
    assert importer.import_bundle(consumer_con, consumer_col, tmp_path / "b")["ok"]
    remaining = [r["uid"] for r in consumer_con.execute("SELECT uid FROM embed_queue")]
    assert remaining == [], f"satisfied demand still queued: {remaining}"


# ═══ F1-D4: a weak first-boot pin must not brick the node later ═══

def test_weak_pin_upgrades_when_the_endpoint_comes_up(tmp_path, monkeypatch):
    """Boot-order trap: a node whose FIRST boot ran while the embed endpoint
    was down pinned a weak (name-derived) model hash. When the endpoint came
    up, the freshly confirmed hash differed from the pin and validate_boot
    REFUSED TO BOOT — permanently, with a message telling the operator to wipe
    their vectors. But under the ollama API a weak hash means the endpoint was
    unreachable, so no vector can ever have been embedded under the weak
    identity: there is nothing to protect, and upgrading the pin is the only
    behavior that does not punish an innocent startup ordering.
    """
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    collection = make_collection(tmp_path / "n", name="gutenberg")

    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda: ("weak-name-hash00", True))
    spaces.validate_boot([collection])                       # first boot: endpoint down

    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda: ("confirmed-digest0", False))
    spaces.validate_boot([collection])                       # must NOT refuse

    con = db.connect(collection)
    import json as json_mod
    pinned = json_mod.loads(con.execute(
        "SELECT components FROM space WHERE id=1").fetchone()["components"])
    assert pinned["embed_model_hash"] == "confirmed-digest0"
    assert pinned["weak"] is False, "the pin was not upgraded"

    # ...and a LATER confirmed change still refuses, exactly as before
    monkeypatch.setattr(spaces, "resolve_embed_model_hash",
                        lambda: ("a-different-model", False))
    with pytest.raises(spaces.BootValidationError):
        spaces.validate_boot([collection])
