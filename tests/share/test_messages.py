"""tests/share/test_messages.py — the five peer messages (09 §9.9; 04 §4.2;
migration step 9c): bodies as the spec shapes them, the rules a receiver
applies before reading a field, pulls answered with the exact stored
bytes, and the peers store's neighbours, bans and seen events."""
import base64
import json
import time

import pytest
from nacl import signing

from app.hold import collection as col, drag, envelope as env, snapshot as sn
from app.hold.codex import canonical_json
from app.share import members as mem, messages, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, NOW, a_root, pubmed

PEER = signing.SigningKey(b"\x07" * 32)
PEER_KEY = env.key_text(PEER.verify_key)


def a_drag(node):  # noqa: F811
    """A held collection with one shard, a signed snapshot and one attestation."""
    codex = a_root(node, pubmed())
    name = col.name_of(node, codex.collection_id)
    d = col.drag_of(node, name)
    data = b"r" * 20_000
    (d / "records/year=2023").mkdir(parents=True)
    (d / "records/year=2023/00001.jsonl.zst").write_bytes(data)
    shard = sn.Shard(slice="year=2023", n=1, root=drag.pieces_root(data),
                     records_root="sha256:" + "11" * 32, first_id="a", last_id="z", records=2,
                     chunks=4, chunks_root="sha256:" + "22" * 32, tokens=2048, bytes=len(data))
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None,
                       shards=(shard,), delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots/2026-08.json").write_bytes(snap.canonical())
    side = sn.sidecar_add(None, snap.hash, KEY, now=NOW)
    (d / "snapshots/2026-08.sig.json").write_bytes(canonical_json(side))
    att_dir = node / name / "state" / "attestations" / drag._hex(codex.collection_id)
    att_dir.mkdir(parents=True)
    for i, t in enumerate(("2026-10-01T00:00:00Z", "2026-10-05T00:00:00Z")):
        body = {"kind": "louise-attestation", "version": 1, "key": PEER_KEY, "time": t,
                "check": "roots", "result": "pass", "n": i}
        att_dir.joinpath(f"{'ab' * 16}{i:02d}.json").write_bytes(
            canonical_json(env.sign(env.ATTESTATION, body, PEER)))
    return codex, name, d, snap


# ---- bodies and the receiver's rules ---------------------------------------------------

def test_a_digest_round_trips_and_is_refused_from_the_future_or_under_another_domain():
    body = messages.digest_body(key=PEER_KEY, address="http://peer.test:8742", neighbours=[],
                                benchmarks=[], collections=[])
    assert body["kind"] == "louise-digest" and body["version"] == 2
    assert set(body) == {"kind", "version", "key", "time", "address", "neighbours",
                         "benchmarks", "collections"}
    now = time.time()
    opened = messages.open_digest(env.sign(env.DIGEST, body, PEER), now=now)
    assert opened["address"] == "http://peer.test:8742"
    ahead = dict(body, time=messages.time_text(now + 2 * peers.INTERVAL_S))
    with pytest.raises(messages.MessageError, match="future"):
        messages.open_digest(env.sign(env.DIGEST, ahead, PEER), now=now)
    with pytest.raises(env.SignatureError):
        messages.open_digest(env.sign(env.EVENT, body, PEER), now=now)
    tampered = env.sign(env.DIGEST, body, PEER)
    tampered["body"]["address"] = "http://evil"
    with pytest.raises(env.SignatureError):
        messages.open_digest(tampered, now=now)


def test_an_event_has_an_id_the_receiver_computes_and_hops_outside_the_signature():
    now = time.time()
    body = messages.event_body(key=PEER_KEY, type_="snapshot",
                               payload={"collection_id": "sha256:" + "0" * 64})
    envelope = env.sign(env.EVENT, body, PEER)
    opened, hops = messages.open_event({**envelope, "hops": 8}, now=now)
    assert opened == body and hops == 8
    assert messages.event_id(body) == env.body_id(body) and "id" not in body
    for bad in ({**envelope, "hops": 9}, {**envelope, "hops": -1}, {**envelope, "hops": True},
                envelope):
        with pytest.raises(messages.MessageError):
            messages.open_event(bad, now=now)
    with pytest.raises(messages.MessageError):
        messages.event_body(key=PEER_KEY, type_="weird", payload={})
    old = dict(body, time=messages.time_text(now - peers.EVENT_TTL_S - 60))
    with pytest.raises(messages.MessageError, match="older than seven days"):
        messages.open_event({**env.sign(env.EVENT, old, PEER), "hops": 3}, now=now)


def test_a_pull_names_what_it_wants():
    body = messages.pull_body(key=PEER_KEY, what="snapshot", collection_id="sha256:" + "0" * 64)
    assert body["kind"] == "louise-pull" and body["snapshot_id"] is None and body["since"] is None
    opened = messages.open_pull(env.sign(env.EVENT, body, PEER), now=time.time())
    assert opened["what"] == "snapshot"
    with pytest.raises(messages.MessageError):
        messages.pull_body(key=PEER_KEY, what="everything", collection_id="x")


# ---- pulls answered with the exact bytes ------------------------------------------------

def test_pulls_answer_the_exact_stored_bytes_and_items_verify(node):  # noqa: F811
    codex, name, d, snap = a_drag(node)
    cid = codex.collection_id
    got = messages.answer_pull(node, messages.pull_body(key=PEER_KEY, what="codex",
                                                        collection_id=cid))
    (path, data), = messages.verify_items(got["items"])
    assert path == f"codex/{drag._hex(cid)}.json" and data == (d / "codex.json").read_bytes()
    got = messages.answer_pull(node, messages.pull_body(key=PEER_KEY, what="snapshot",
                                                        collection_id=cid))
    items = dict(messages.verify_items(got["items"]))
    assert items["snapshots/2026-08.json"] == snap.canonical()
    assert json.loads(items["snapshots/2026-08.sig.json"])["snapshot"] == snap.hash
    got = messages.answer_pull(node, messages.pull_body(key=PEER_KEY, what="snapshot",
                                                        collection_id=cid, snapshot_id="2026-09"))
    assert got["items"] == []
    got = messages.answer_pull(node, messages.pull_body(key=PEER_KEY, what="attestations",
                                                        collection_id=cid))
    assert len(got["items"]) == 2
    got = messages.answer_pull(node, messages.pull_body(key=PEER_KEY, what="attestations",
                                                        collection_id=cid,
                                                        since="2026-10-03T00:00:00Z"))
    assert len(got["items"]) == 1 and json.loads(base64.b64decode(got["items"][0]["b64"]))["body"]["n"] == 1
    assert messages.answer_pull(node, messages.pull_body(
        key=PEER_KEY, what="codex", collection_id="sha256:" + "f" * 64)) == {"items": []}
    lie = dict(got["items"][0], b64=base64.b64encode(b"other bytes").decode())
    with pytest.raises(messages.MessageError, match="a lie"):
        messages.verify_items([lie])


def test_a_collection_entry_is_9_9_s_shape(node):  # noqa: F811
    codex, name, d, snap = a_drag(node)
    members = mem.members_of(snap, drag_dir=d)
    entry = messages.collection_entry(codex, snap, members, codex_bytes=(d / "codex.json").read_bytes(),
                                      lineage_id=codex.collection_id)
    assert set(entry) == {"collection_id", "lineage_id", "title", "snapshot_id", "snapshot_hash",
                          "codex_hash", "license", "privacy", "members", "coverage"}
    assert entry["snapshot_id"] == "2026-08" and entry["snapshot_hash"] == snap.hash
    assert entry["members"] == [{"name": "records-year=2023", "root": snap.shards[0].root,
                                 "bytes": 20_000}]
    assert entry["license"] == codex.license.cls and entry["privacy"] == "public"


# ---- the peers store ----------------------------------------------------------------------

def test_neighbours_bans_and_seen_events(node, monkeypatch):  # noqa: F811
    con = peers.connect()
    now = 1_000_000.0
    body = messages.digest_body(key=PEER_KEY, address="http://peer.test", neighbours=[],
                                benchmarks=[], collections=[], now=now)
    peers.record_digest(con, body, now=now)
    assert peers.neighbours(con, now=now) == [{"key": PEER_KEY, "address": "http://peer.test"}]
    assert peers.neighbours(con, now=now + peers.NEIGHBOUR_WINDOW_S + 1) == []
    assert peers.digest_of(con, PEER_KEY)["address"] == "http://peer.test"
    peers.ban(con, PEER_KEY, now=now, why="a lie")
    assert peers.is_banned(con, PEER_KEY, now=now) and peers.neighbours(con, now=now) == []
    assert not peers.is_banned(con, PEER_KEY, now=now + peers.BAN_S + 1)
    eid = "sha256:" + "c" * 64
    assert not peers.seen(con, eid)
    peers.mark_seen(con, eid, now=now, time_text=messages.time_text(now), type_="snapshot",
                    hops=7, envelope={"body": {"key": "k"}, "sig": "s"})
    assert peers.seen(con, eid) and peers.to_forward(con)[0]["hops"] == 7
    peers.mark_forwarded(con, eid)
    assert peers.to_forward(con) == []
    assert peers.sweep(con, now=now + peers.EVENT_TTL_S + 1) == 1 and not peers.seen(con, eid)
    later = now + peers.BAN_S + 1
    assert peers.recipients(con, now=later) == [], "a day on, its digest is stale too"
    peers.record_digest(con, dict(body, time=messages.time_text(later)), now=later)
    assert peers.recipients(con, now=later) == ["http://peer.test"]
    monkeypatch.setattr(peers, "bootstrap_addresses", lambda: ("http://boot.test", "http://peer.test"))
    assert peers.recipients(con, now=later) == ["http://peer.test", "http://boot.test"], (
        "the peer list joins, each address once")
    con.close()
