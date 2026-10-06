"""tests/share/test_accept.py — a bundle another peer announced, taken in
(04 §4.3, §4.5; 03 §3.5; 09 §9.7, §9.9, §9.16), on localhost: the sharer
contributes and serves; the fetcher holds the collection and its records,
hears the `bundle` events, fetches the four files by the torrents the
event's file list gives, judges the bundle, lands the vectors, records
the coverage and serves the bundle itself — the survivor of 04 §4.1."""
import shutil
import time

import pytest

lt = pytest.importorskip("libtorrent")

from app import copy_state, db, embeddings, licensing  # noqa: E402
from app.acquire import seeding, transport_torrent as tt  # noqa: E402
from app.hold import collection as col, drag as drag_mod, records as rec  # noqa: E402
from app.share import accept, attest, contribute, coverage as cov, holdings, members as mem, peers, \
    rendezvous, seeder, transport  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.share.test_contribute import attested, ready  # noqa: F401, E402
from tests.share.test_fetch import fetcher  # noqa: F401, E402
from tests.share.test_messages import PEER, PEER_KEY  # noqa: E402
from tests.share.test_seeder import quiet_sessions  # noqa: E402
from tests.share.test_share_api import OURS, OUR_KEY  # noqa: E402
from tests.share.test_transport import leech_session  # noqa: E402


class C:
    def __init__(self, name, codex):
        self.name, self.codex = name, codex
        self.share = True


def test_member_of_reads_the_event_s_file_list_and_refuses_what_does_not_hash(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    for f in mem.BUNDLE_FILES:
        (d / f).write_bytes(f.encode() * 10)
    member = mem.bundle_member("a" * 16, d)
    payload = {"bundle_id": "sha256:" + "b" * 64, "root": member.root, "files": member.to_dict()["files"]}
    got = accept.member_of(payload, "a" * 16)
    assert got is not None and got.root == member.root and got.name == f"bundle-{'a' * 16}-{'b' * 64}"
    assert accept.member_of({**payload, "root": "sha256:" + "0" * 64}, "a" * 16) is None, "the list must hash to the root"
    assert accept.member_of({k: v for k, v in payload.items() if k != "files"}, "a" * 16) is None, "an older announcer"
    short = dict(payload, files=payload["files"][:3])
    assert accept.member_of(short, "a" * 16) is None


def test_the_survivor_takes_the_announced_bundle_in_and_serves_it(ready, tmp_path, monkeypatch):
    quiet_sessions(monkeypatch)
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    sdrag = col.drag_of(root, "gutenberg")
    member_path = drag_mod.calibration_paths(codex.space.calibration.set)[0]
    assert drag_mod.pieces_root_of(sdrag / member_path) == snapshot.files[member_path]["root"], \
        "the fixture's snapshot names the member as it lies"
    # the sharer contributes, then serves records and bundles
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)
    assert out["accepted"], "the sharer's own bundles"
    assert drag_mod.pieces_root_of(sdrag / member_path) == snapshot.files[member_path]["root"], \
        "contributing rewrote the calibration member"
    c = C("gutenberg", codex)
    monkeypatch.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
    monkeypatch.setattr(licensing, "state_for", lambda collection, *a, **k: licensing.NOT_REQUIRED)
    entries = seeder.publish(c, snapshot, col.drag_of(root, "gutenberg"), root / "gutenberg" / "state", kw["con"])
    assert any(e["member"].startswith("bundle-") for e in entries)
    seeding.tick([c], {c.name: kw["con"]}, kw["con"], "running")
    seed_port = tt.get_session().listen_port()
    # the fetcher: the pre-ack set in place, then the members by torrent
    fetcher = tmp_path / "fetcher" / "collections"
    fetcher.mkdir(parents=True)
    col.write(fetcher, codex)
    fdrag = col.drag_of(fetcher, "gutenberg")
    (fdrag / "snapshots").mkdir(exist_ok=True)
    for p in (col.drag_of(root, "gutenberg") / "snapshots").glob("*.json"):
        shutil.copy(p, fdrag / "snapshots" / p.name)
    flocal = rec.local_collection(fetcher, "gutenberg", codex)
    fcon = db.connect(flocal)
    leech = leech_session()
    got = transport.fetch_members(fetcher, "gutenberg", codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=leech, con=fcon,
                                  hints=[("127.0.0.1", seed_port)])
    assert got["errors"] == [] and got["refused"] == [] and got["promoted"], got
    assert all(holdings.held(fdrag, m) for m in holdings.wanted(snapshot, slices="all", kinds=("records",)))
    # the events the sharer announced reach the fetcher (one node.sqlite here: re-opened as received),
    # and the sharer's digest names the port its swarm listens on (09 §9.9 peer_port)
    node_con = kw["node_con"]
    from app.share import messages
    now = time.time()
    peers.record_digest(node_con, {**messages.digest_body(
        key=PEER_KEY, address="http://127.0.0.1:1", neighbours=[], benchmarks=[],
        collections=[{"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "g",
                      "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash,
                      "codex_hash": "sha256:" + "0" * 64, "license": "green", "privacy": "public",
                      "members": [], "coverage": []}], now=now), "peer_port": seed_port}, now=now)
    assert transport.hints_for(node_con, codex.collection_id, now=now) == [("127.0.0.1", seed_port)]
    monkeypatch.setattr(transport, "METADATA_TIMEOUT_S", 30.0)
    ids = [ev["id"] for ev in peers.to_forward(node_con)]
    assert accept.pending(node_con) == [], "our own announcements are not ours to take in"
    for eid in ids:
        node_con.execute("UPDATE events_seen SET acted=0 WHERE id=?", (eid,))
    assert len(accept.pending(node_con)) == len(out["accepted"])
    said: list[str] = []
    fc = C("gutenberg", codex)
    taken = accept.beat([fc], {"gutenberg": fcon}, node_con, session_factory=lambda: leech, root=fetcher,
                        log=said.append)
    assert taken["accepted"] == len(out["accepted"]) and taken["refused"] == 0, (taken, said)
    assert accept.pending(node_con) == [], "acted on once"
    rows = cov.rows(fcon)
    assert len(rows) == len(out["accepted"])
    assert all(r["accepted_at"] and r["source"] == "fetched" and r["producer"] == OUR_KEY for r in rows)
    assert {r["bundle_id"] for r in rows} == set(out["accepted"])
    shown = cov.show(codex, snapshot, fcon)
    assert shown["covered"] == shown["chunks"] > 0, "the fetcher's map is covered by what it accepted"
    stored = embeddings.vectors_by_chunk_id([r for b in out["bundles"] for r in []], collection=flocal)  # noqa: F841
    for b in out["bundles"]:
        d = contribute.bundle_dir(fetcher, "gutenberg", codex, b["shard"], b["bundle_id"])
        assert d.is_dir() and sorted(p.name for p in d.iterdir()) == sorted(mem.BUNDLE_FILES)
    assert not any((fetcher / "gutenberg" / "state" / holdings.STAGING).rglob("header.json")), "staging is empty"
    assert any("accepted" in s and "served from here now" in s for s in said)
    # the survivor serves the bundles it took in
    served = seeder.publish(fc, snapshot, fdrag, fetcher / "gutenberg" / "state", fcon)
    assert {e["member"] for e in served if e["member"].startswith("bundle-")} == {b["member"] for b in out["bundles"]}
    # a second beat has nothing to do; the same event again is held, not fetched twice
    for eid in ids:
        node_con.execute("UPDATE events_seen SET acted=0 WHERE id=?", (eid,))
    again = accept.beat([fc], {"gutenberg": fcon}, node_con, session_factory=lambda: leech, root=fetcher)
    assert again["held"] == len(ids) and again["accepted"] == 0
    fcon.close()


def test_a_newer_snapshot_announced_is_pulled_from_its_announcer(node, fetcher, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    from app import api
    from app.hold import envelope as env, snapshot as sn
    from app.hold.codex import canonical_json
    from app.share import fetch, messages
    from tests.hold.conftest import KEY, NOW
    from tests.share.test_fetch import SHARER, second_snapshot
    from tests.share.test_messages import a_drag
    codex, name, d, first = a_drag(node)
    client = TestClient(api.app)
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client, key=OURS, own_key=OUR_KEY)
    assert got["snapshots"]["promoted"] == ["2026-08"]
    second = second_snapshot(d, first, signed=True)
    now = time.time()
    node_con = peers.connect()
    announce = messages.event_body(key=PEER_KEY, type_="snapshot", payload={
        "collection_id": codex.collection_id, "snapshot_id": "2026-09", "snapshot_hash": second.hash,
        "sig": {"key": env.key_text(KEY.verify_key), "time": NOW, "sig": "…"}}, now=now)
    peers.mark_seen(node_con, messages.event_id(announce), now=now, time_text=announce["time"], type_="snapshot",
                    hops=7, envelope=env.sign(env.EVENT, announce, PEER))
    fc = C(got["name"], codex)
    kw = dict(session_factory=lambda: None, root=fetcher, client=client, key=OURS, own_key=OUR_KEY)
    out = accept.beat([fc], {fc.name: None}, node_con, **kw)
    assert out["snapshots"] == {"pulled": 0, "known": 0, "ignored": 0, "waiting": 1, "failed": 0}, \
        "the announcer's address is unknown until its digest arrives"
    peers.record_digest(node_con, messages.digest_body(key=PEER_KEY, address=SHARER, neighbours=[], benchmarks=[],
                                                       collections=[], now=now), now=now)
    said: list[str] = []
    out = accept.beat([fc], {fc.name: None}, node_con, log=said.append, **kw)
    assert out["snapshots"]["pulled"] == 1 and accept.pending(node_con, "snapshot") == []
    fdrag = col.drag_of(fetcher, fc.name)
    assert (fdrag / "snapshots/2026-09.json").read_bytes() == second.canonical()
    assert any("snapshot 2026-09 pulled" in s for s in said)
    # announced again, or an older one: known, nothing pulled
    for sid, h in (("2026-09", second.hash), ("2026-08", first.hash)):
        body = messages.event_body(key=PEER_KEY, type_="snapshot", payload={
            "collection_id": codex.collection_id, "snapshot_id": sid, "snapshot_hash": h, "sig": {}}, now=now + 1)
        peers.mark_seen(node_con, messages.event_id(body), now=now, time_text=body["time"], type_="snapshot",
                        hops=7, envelope=env.sign(env.EVENT, body, PEER))
    out = accept.beat([fc], {fc.name: None}, node_con, **kw)
    assert out["snapshots"]["known"] == 2 and out["snapshots"]["pulled"] == 0
    node_con.close()


def test_an_event_about_a_collection_not_held_is_left_alone_and_a_gated_one_waits(ready, monkeypatch):
    root, snapshot, codex, kw = ready
    node_con = kw["node_con"]
    from app.hold import envelope as env
    from app.share import messages
    now = time.time()
    foreign = messages.event_body(key=PEER_KEY, type_="bundle", payload={"collection": "sha256:" + "f" * 64,
                                                                           "bundle_id": "sha256:" + "a" * 64}, now=now)
    peers.mark_seen(node_con, messages.event_id(foreign), now=now, time_text=foreign["time"], type_="bundle",
                    hops=7, envelope=env.sign(env.EVENT, foreign, PEER))
    ours = messages.event_body(key=PEER_KEY, type_="bundle", payload={"collection": codex.collection_id,
                                                                        "bundle_id": "sha256:" + "a" * 64}, now=now)
    peers.mark_seen(node_con, messages.event_id(ours), now=now, time_text=ours["time"], type_="bundle",
                    hops=7, envelope=env.sign(env.EVENT, ours, PEER))
    c = C("gutenberg", codex)
    monkeypatch.setattr(licensing, "state_for", lambda collection, *a, **k: licensing.MISSING)
    out = accept.beat([c], {"gutenberg": kw["con"]}, node_con, session_factory=lambda: None, root=root)
    assert out["ignored"] == 1 and out["deferred"] == 1, "not held: acted on; gated: left for the gate to clear"
    assert len(accept.pending(node_con)) == 1
    monkeypatch.setattr(licensing, "state_for", lambda collection, *a, **k: licensing.NOT_REQUIRED)
    monkeypatch.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
    out = accept.beat([c], {"gutenberg": kw["con"]}, node_con, session_factory=lambda: None, root=root)
    assert out["ignored"] == 1, "no file list the root agrees with: acted on by being left alone"
    assert accept.pending(node_con) == []
