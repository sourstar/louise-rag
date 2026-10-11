"""tests/share/test_reproduce.py — another key's forgery proof is a claim
until reproduced (03 §3.5): a chunk held imported from the producer it
names is cited for T4; one not held from that producer, or this peer's
own proof, is left a claim; each proof is acted on once."""
import sqlite3
from types import SimpleNamespace

from nacl import signing

from app.bundles import verify_on_use
from app.hold import envelope as env, shard as shard_mod
from app.hold.codex import canonical_json
from app.share import accept
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import a_root, pubmed

STRANGER = signing.SigningKey(b"\x0b" * 32)
PRODUCER_HEX = "ab" * 32
PRODUCER = "ed25519:" + PRODUCER_HEX


def a_manifest():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE chunk_verification (chunk_id TEXT PRIMARY KEY, uid TEXT, state TEXT NOT NULL, "
                "producer TEXT, bundle_id TEXT, cosine REAL, checked_at INTEGER)")
    con.executemany("INSERT INTO chunk_verification(chunk_id, uid, state, producer) VALUES(?,?,?,?)",
                    [("pubmed:1:0:1", "pubmed:1", "imported", PRODUCER_HEX),
                     ("pubmed:2:0:1", "pubmed:2", "verified", PRODUCER_HEX),
                     ("pubmed:3:0:1", "pubmed:3", "imported", "cd" * 32)])
    return con


def a_proof(codex, key, *, chunk_id, producer=PRODUCER, time="2026-10-01T00:00:00Z"):
    body = {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": "2026-08", "snapshot_hash": "sha256:" + "11" * 32, "check": "forgery", "seed": None,
            "shards": [], "result": "forgery",
            "detail": {"chunk_id": chunk_id, "producer": producer, "bundle": "sha256:" + "22" * 32,
                       "claimed": "sha256:" + "33" * 32, "recomputed": "sha256:" + "44" * 32, "text": None,
                       "content_hash": "55" * 16},
            "platform_image": "sha256:" + "66" * 32, "encoder_engine": "sha256:" + "77" * 32,
            "encoder_weights": "sha256:" + "88" * 32, "key": env.key_text(key.verify_key), "time": time}
    return env.sign(env.ATTESTATION, body, key)


def keep(node, codex, name, doc):
    d = node / name / "state" / "attestations" / shard_mod._hex(codex.collection_id)
    d.mkdir(parents=True, exist_ok=True)
    aid = env.body_id(doc["body"])
    (d / f"{shard_mod._hex(aid)}.json").write_bytes(canonical_json(doc))
    return aid


def test_a_claimed_forgery_on_a_chunk_held_from_that_producer_is_cited_once(node):  # noqa: F811
    from app.hold import collection as col
    codex = pubmed()
    a_root(node, codex)
    name = col.name_of(node, codex.collection_id)
    con = a_manifest()
    node_con = sqlite3.connect(":memory:")
    node_con.row_factory = sqlite3.Row
    c = SimpleNamespace(name=name, codex=codex)
    a1 = keep(node, codex, name, a_proof(codex, STRANGER, chunk_id="pubmed:1:0:1"))
    keep(node, codex, name, a_proof(codex, STRANGER, chunk_id="pubmed:2:0:1", time="2026-10-01T00:00:01Z"))
    keep(node, codex, name, a_proof(codex, STRANGER, chunk_id="pubmed:3:0:1", time="2026-10-01T00:00:02Z"))
    keep(node, codex, name, a_proof(codex, STRANGER, chunk_id="pubmed:9:0:1", time="2026-10-01T00:00:03Z"))
    said = []
    out = accept.reproduce_forgeries(node, [c], {name: con}, node_con, own_key="ed25519:" + "00" * 32, now=5.0,
                                     log=said.append)
    assert out == {"seen": 4, "cited": 1, "not_held": 3}, \
        "imported from the named producer: cited; verified, another producer's, not held: claims"
    assert verify_on_use.queued(con, 10) == ["pubmed:1:0:1"] and "cited for T4" in said[0]
    assert node_con.execute("SELECT outcome FROM claims_acted WHERE attestation_id=?", (a1,)).fetchone()[0] == "cited"
    out = accept.reproduce_forgeries(node, [c], {name: con}, node_con, own_key="ed25519:" + "00" * 32, now=6.0)
    assert out == {"seen": 0, "cited": 0, "not_held": 0}, "acted on once"
    # this peer's own proof is not a claim to reproduce
    own = signing.SigningKey(b"\x0c" * 32)
    keep(node, codex, name, a_proof(codex, own, chunk_id="pubmed:1:0:1", time="2026-10-01T00:00:04Z"))
    out = accept.reproduce_forgeries(node, [c], {name: con}, node_con, own_key=env.key_text(own.verify_key), now=7.0)
    assert out["seen"] == 0
    # a collection without a manifest connection or a Codex is skipped
    assert accept.reproduce_forgeries(node, [SimpleNamespace(name="x", codex=None)], {}, node_con, own_key=None,
                                      now=8.0) == {"seen": 0, "cited": 0, "not_held": 0}
