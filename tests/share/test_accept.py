"""tests/share/test_accept.py — a bundle another peer announced, taken in
(04 §4.3, §4.5; 03 §3.5; 09 §9.7, §9.9, §9.16), on localhost: the sharer
contributes and serves; the fetcher holds the collection and its records,
hears the `bundle` events, fetches the four files by the torrents the
event's file list gives, judges the bundle, lands the vectors, records
the coverage and serves the bundle itself — the survivor of 04 §4.1."""
import json
import shutil
import time

import pytest

lt = pytest.importorskip("libtorrent")

from app import copy_state, db, embeddings, licensing  # noqa: E402
from app.acquire import seeding, transport_torrent as tt  # noqa: E402
from app.hold import collection as col, shard as shard_mod, records as rec  # noqa: E402
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


def test_a_staged_file_at_full_length_with_holes_is_fetched_on_not_refused_as_the_producer_s(tmp_path, monkeypatch):
    """A stop or a lagging disk leaves a bundle file at its full length with
    blocks still unwritten (libtorrent sizes files first). Taken as landed
    by its length, it failed the root check and the bundle was REFUSED, its
    event acted on for good — a right bundle lost to a local race
    (2026-10-08). Only a file that hashes to its root is skipped."""
    import types
    d = tmp_path / "b"
    d.mkdir()
    for f in mem.BUNDLE_FILES:
        (d / f).write_bytes(f.encode() * 10)
    member = mem.bundle_member("a" * 16, d)
    payload = {"bundle_id": "sha256:" + "b" * 64, "root": member.root, "files": member.to_dict()["files"]}
    staged = tmp_path / "c" / "g" / "state" / holdings.STAGING / accept.member_of(payload, "a" * 16).name
    staged.mkdir(parents=True)
    for f in member.files:
        (staged / f.path).write_bytes(b"\0" * f.bytes)            # full length, nothing written
    fetched: list[str] = []

    def fetch_file(session, *, torrent, member, staging, **_):
        fetched.append(torrent.path)
        (staging / member / torrent.path).write_bytes((d / torrent.path).read_bytes())
        return staging / member / torrent.path

    class Judged(Exception):
        pass

    def past_the_root_check(*a, **k):
        raise Judged
    monkeypatch.setattr(transport, "fetch_file", fetch_file)
    monkeypatch.setattr(cov, "row", lambda con, bundle_id: None)
    monkeypatch.setattr(contribute, "calibration_tensors", past_the_root_check)
    codex = types.SimpleNamespace(space_segment="a" * 16)
    with pytest.raises(Judged):
        accept.take(tmp_path / "c", "g", codex, payload, session=None, con=None, node_con=None)
    assert sorted(fetched) == sorted(f.path for f in member.files), "every holed file fetched on"
    fetched.clear()
    with pytest.raises(Judged):
        accept.take(tmp_path / "c", "g", codex, payload, session=None, con=None, node_con=None)
    assert fetched == [], "whole files from an earlier beat are not fetched again"


def test_the_survivor_takes_the_announced_bundle_in_and_serves_it(ready, tmp_path, monkeypatch):
    quiet_sessions(monkeypatch)
    root, snapshot, codex, kw = ready
    # the fetcher's vector store is this test's tmp_path: the disk floor
    # (app/disk_guard.py) is the host's business, not what is tested here —
    # a runner with 29 GiB free deferred every bundle (2026-10-19)
    from app import config
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_GB", 0.0)
    monkeypatch.setattr(config, "VECTORS_MIN_FREE_FRACTION", 0.0)
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    sshard = col.shard_dir_of(root, "gutenberg")
    member_path = shard_mod.calibration_paths(codex.space.calibration.set)[0]
    assert shard_mod.pieces_root_of(sshard / member_path) == snapshot.files[member_path]["root"], \
        "the fixture's snapshot names the member as it lies"
    # the sharer contributes, then serves records and bundles
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)
    assert out["accepted"], "the sharer's own bundles"
    assert shard_mod.pieces_root_of(sshard / member_path) == snapshot.files[member_path]["root"], \
        "contributing rewrote the calibration member"
    c = C("gutenberg", codex)
    monkeypatch.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
    monkeypatch.setattr(licensing, "state_for", lambda collection, *a, **k: licensing.NOT_REQUIRED)
    entries = seeder.publish(c, snapshot, col.shard_dir_of(root, "gutenberg"), root / "gutenberg" / "state", kw["con"])
    assert any(e["member"].startswith("bundle-") for e in entries)
    seeding.tick([c], {c.name: kw["con"]}, kw["con"], "running")
    seed_port = tt.get_session().listen_port()
    # the fetcher: the pre-ack set in place, then the members by torrent
    fetcher = tmp_path / "fetcher" / "collections"
    fetcher.mkdir(parents=True)
    col.write(fetcher, codex)
    fshard = col.shard_dir_of(fetcher, "gutenberg")
    (fshard / "snapshots").mkdir(exist_ok=True)
    for p in (col.shard_dir_of(root, "gutenberg") / "snapshots").glob("*.json"):
        shutil.copy(p, fshard / "snapshots" / p.name)
    flocal = rec.local_collection(fetcher, "gutenberg", codex)
    fcon = db.connect(flocal)
    leech = leech_session()
    got = transport.fetch_members(fetcher, "gutenberg", codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=leech, con=fcon,
                                  hints=[("127.0.0.1", seed_port)])
    assert got["errors"] == [] and got["refused"] == [] and got["promoted"], got
    assert all(holdings.held(fshard, m) for m in holdings.wanted(snapshot, slices="all", kinds=("records",)))
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
    served = seeder.publish(fc, snapshot, fshard, fetcher / "gutenberg" / "state", fcon)
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
    from tests.share.test_messages import a_shard
    codex, name, d, first = a_shard(node)
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
    fshard = col.shard_dir_of(fetcher, fc.name)
    assert (fshard / "snapshots/2026-09.json").read_bytes() == second.canonical()
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


def test_a_codex_announced_enters_the_catalogue_once_and_a_chain_that_does_not_read_is_dropped(node):  # noqa: F811
    from app.hold import envelope as env
    from app.share import catalogue, messages
    from tests.hold.conftest import pubmed
    codex = pubmed()
    now = time.time()
    node_con = peers.connect()

    def event(payload, when=now):
        body = messages.event_body(key=PEER_KEY, type_="codex", payload=payload, now=when)
        peers.mark_seen(node_con, messages.event_id(body), now=now, time_text=body["time"], type_="codex",
                        hops=7, envelope=env.sign(env.EVENT, body, PEER))
        return body
    event({"collection_id": codex.collection_id, "chain": [codex.to_dict()]})
    said: list[str] = []
    assert accept.codexes(node_con, log=said.append) == {"recorded": 1, "known": 0, "dropped": 0}
    (entry,) = catalogue.entries(node_con)
    assert (entry["collection_id"], entry["lineage_id"], entry["relation"], entry["title"], entry["announced_by"]) == \
        (codex.collection_id, codex.collection_id, "root", "PubMed", PEER_KEY)
    assert catalogue.codex_of(node_con, codex.collection_id).collection_id == codex.collection_id
    assert any("in the catalogue" in s for s in said)
    event({"collection_id": codex.collection_id, "chain": [codex.to_dict()]}, when=now + 60)   # announced again later
    assert accept.codexes(node_con)["known"] == 1 and len(catalogue.entries(node_con)) == 1
    event({"collection_id": "sha256:" + "f" * 64, "chain": [codex.to_dict()]})     # names another id
    event({"collection_id": codex.collection_id, "chain": [{"x": 1}]})
    assert accept.codexes(node_con, log=said.append)["dropped"] == 2
    assert accept.pending(node_con, "codex") == []
    assert peers.show(node_con, now=now)["catalogue"][0]["collection_id"] == codex.collection_id
    node_con.close()


def test_a_flag_received_has_its_attestations_pulled_and_is_a_claim_until_seconded(node, fetcher, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    from app import api, copy_state
    from app.hold import envelope as env
    from app.share import fetch, levels, messages
    from tests.hold.conftest import PLATFORM
    from tests.share.test_fetch import SHARER
    from tests.share.test_messages import a_shard
    codex, name, d, snap = a_shard(node)
    client = TestClient(api.app)
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client, key=OURS, own_key=OUR_KEY)
    now = time.time()
    shard = f"{codex.collection_id}/year=2023/00001"
    failing = {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
               "snapshot_id": snap.snapshot_id, "snapshot_hash": snap.hash, "check": "chunks", "seed": None,
               "shards": [shard], "result": "fail", "detail": {"records": 2, "failed": ["x"]},
               "platform_image": PLATFORM, "encoder_engine": None, "encoder_weights": None,
               "key": PEER_KEY, "time": messages.time_text(now - 60)}
    envelope = env.sign(env.ATTESTATION, failing, PEER)
    aid = env.body_id(failing)
    attest.attestation_path(node, name, codex, aid).write_bytes(json.dumps(envelope).encode())   # on the sharer
    node_con = peers.connect()
    flag = messages.event_body(key=PEER_KEY, type_="flag", payload={
        "collection_id": codex.collection_id, "snapshot_hash": snap.hash, "check": "chunks",
        "attestations": [aid]}, now=now)
    peers.mark_seen(node_con, messages.event_id(flag), now=now, time_text=flag["time"], type_="flag", hops=7,
                    envelope=env.sign(env.EVENT, flag, PEER))
    fc = C(got["name"], codex)
    kw = dict(client=client, key=OURS, own_key=OUR_KEY, now=now)
    assert accept.flags(fetcher, [fc], node_con, **kw) == {"kept": 0, "ignored": 0, "waiting": 1, "failed": 0}
    peers.record_digest(node_con, messages.digest_body(key=PEER_KEY, address=SHARER, neighbours=[], benchmarks=[],
                                                       collections=[], now=now), now=now)
    said: list[str] = []
    assert accept.flags(fetcher, [fc], node_con, log=said.append, **kw)["kept"] == 1
    assert attest.attestation_path(fetcher, fc.name, codex, aid).is_file()
    assert accept.pending(node_con, "flag") == [] and any("a claim until" in s for s in said)
    bodies = copy_state.attestations(fetcher, fc.name, codex)
    out = levels.level(codex, snap, bodies, now=now, own_key=OUR_KEY, reference_images={PLATFORM})
    assert out["level"] == levels.UNVERIFIED and out["claims"] == [
        {"check": "chunks", "shard": shard, "key": PEER_KEY, "time": failing["time"]}]
    second = {**failing, "key": OTHER_KEY_FLAG, "time": messages.time_text(now - 30)}
    out = levels.level(codex, snap, list(bodies) + [second], now=now, own_key=OUR_KEY, reference_images={PLATFORM})
    assert out["level"] == levels.FLAGGED
    node_con.close()


OTHER_KEY_FLAG = "ed25519:" + "7" * 64


def test_a_second_bundle_over_held_chunks_is_compared_attested_and_not_stored(ready, monkeypatch):
    from app.hold import export as exp
    from tests.share.test_messages import PEER
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "1f" * 32)
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)
    first = out["bundles"][0]
    sid = first["shard"]
    # another producer's bundle over the same chunks, from the same honest vectors
    second = exp.export(root, "gutenberg", shard=sid, dest=root.parent / "second", vectors_for=kw["vectors_for"],
                        embed=kw["embed"], key=PEER, snapshot_id=snapshot.snapshot_id)
    b2 = second["bundles"][0]
    named = root.parent / shard_mod._hex(b2["bundle_id"])          # 09 §9.7: the directory is the id
    shutil.move(b2["path"], named)
    member = mem.bundle_member(codex.space_segment, named)
    staged = root / "gutenberg" / "state" / holdings.STAGING / member.name
    shutil.copytree(named, staged)                                  # as a fetch would have left it
    payload = {"collection": codex.collection_id, "space_id": codex.space_id, "snapshot_id": snapshot.snapshot_id,
               "shard": sid, "uids": [], "bundle_id": b2["bundle_id"], "root": member.root, "bytes": member.bytes,
               "files": member.to_dict()["files"]}
    events_before = len(peers.to_forward(kw["node_con"]))
    said: list[str] = []
    taken = accept.take(root, "gutenberg", codex, payload, session=None, con=kw["con"], node_con=kw["node_con"],
                        vectors_for=kw["vectors_for"], write=kw["write"], key=OURS, own_key=OUR_KEY,
                        log=said.append)
    assert taken["outcome"] == "accepted" and taken["written"] == 0, "compared, not stored"
    dup = taken["duplicate"]
    assert dup["result"] == "pass" and dup["failed"] == [] and len(dup["compared"]) == b2["chunks"]
    assert dup["attestation"] is not None
    doc = json.loads(attest.attestation_path(root, "gutenberg", codex, dup["attestation"]).read_bytes())
    body = doc["body"]
    assert body["check"] == "vectors-sample" and body["result"] == "pass" and body["seed"] is None
    assert sorted(body["detail"]["producers"]) == sorted([OUR_KEY, PEER_KEY]), "both producers named (04 §4.5)"
    assert body["shards"] == [cov.shard_id(codex, sid)] and body["detail"]["chunks"] == b2["chunks"]
    assert len(peers.to_forward(kw["node_con"])) == events_before + 1, "published as an event"
    row = cov.row(kw["con"], b2["bundle_id"])
    assert row["accepted_at"] and row["producer"] == PEER_KEY and row["source"] == "fetched"
    assert any("compared with what was held: pass" in s for s in said)


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


def test_a_bundle_a_digest_offers_is_taken_in_without_its_event(node, monkeypatch, tmp_path):  # noqa: F811
    """04 §4.5, amended 2026-10-19: an event is forwarded once and kept seven
    days; a peer that missed it finds the bundle under the neighbour's
    digest `members` and takes it in the same way — once per digest
    arrival, with a backoff for a bundle nobody served."""
    import types
    from app.share import messages
    cid = "sha256:" + "0" * 64
    codex = types.SimpleNamespace(collection_id=cid, space_id="sha256:" + "5" * 64, space_segment="a" * 16,
                                  privacy="public")
    d = tmp_path / ("b" * 64)                       # a bundle directory is named by its id's hex (09 §9.7)
    d.mkdir()
    for f in mem.BUNDLE_FILES:
        (d / f).write_bytes(f.encode() * 10)
    member = mem.bundle_member("a" * 16, d)
    bid = "sha256:" + d.name
    entry = {"collection_id": cid, "lineage_id": cid, "title": "g", "snapshot_id": "2026-09",
             "snapshot_hash": "sha256:" + "1" * 64, "codex_hash": "sha256:" + "2" * 64, "license": "green",
             "privacy": "public", "coverage": [{"shard": f"{cid}/{'a' * 16}/lang=af/00001", "bundles": [bid]}],
             "members": [member.to_dict(), {"name": "records-lang=af", "root": "sha256:" + "3" * 64, "bytes": 5}]}
    now = time.time()
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    cov.ensure_schema(con)
    with peers.connect() as ncon:
        body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[], benchmarks=[],
                                    collections=[entry], now=now)
        peers.record_digest(ncon, {**body, "peer_port": 6881}, now=now)
        offered = accept.listed_bundles(ncon, codex, con, now=now)
        assert len(offered) == 1
        payload, hints = offered[0]
        assert payload["bundle_id"] == bid and payload["root"] == member.root and payload["files"] == member.to_dict()["files"]
        assert payload["collection"] == cid and payload["snapshot_id"] == "2026-09" and hints == [("10.0.0.7", 6881)]
        assert accept.member_of(payload, "a" * 16) is not None, "the event's shape: the same reader takes it"
        cov.record(con, bundle_id=bid, shard=f"{cid}/{'a' * 16}/lang=af/00001", producer=PEER_KEY, uids=[],
                   chunks=3, tiers={}, source=cov.FETCHED, accepted_at=messages.time_text(now))
        assert accept.listed_bundles(ncon, codex, con, now=now) == [], "accepted here: not asked for"
        cov.record(con, bundle_id=bid, shard=f"{cid}/{'a' * 16}/lang=af/00001", producer=PEER_KEY, uids=[],
                   chunks=0, tiers={"T0": "pass", "T1": "fail"}, source=cov.FETCHED, accepted_at=None)
        assert accept.listed_bundles(ncon, codex, con, now=now) == [], "refused by this peer's ladder: not asked for again"
        con.execute("DELETE FROM coverage")
        # the beat: taken in through `take`, read once per digest arrival, backed off when nobody serves
        taken: list[dict] = []
        verdict = {"outcome": "accepted"}

        def fake_take(root, name, codex_, payload_, **kw):
            taken.append(payload_)
            if isinstance(verdict.get("raise"), Exception):
                raise verdict["raise"]
            return dict(verdict)
        monkeypatch.setattr(accept, "take", fake_take)
        monkeypatch.setattr(accept, "_digests_read", None)
        monkeypatch.setattr(accept, "_digest_backoff", {})
        c = C("g", codex)
        kw = dict(session_factory=lambda: None, root=tmp_path, usable=lambda c_: True, now=now)
        got = accept.from_digests([c], {"g": con}, ncon, **kw)
        assert got["offered"] == 1 and got["accepted"] == 1 and [p["bundle_id"] for p in taken] == [bid]
        assert accept.from_digests([c], {"g": con}, ncon, **dict(kw, now=now + 60)) == \
            {"accepted": 0, "refused": 0, "ignored": 0, "held": 0, "deferred": 0, "offered": 0}, \
            "the same digests: not read again until one arrives or the backoff passes"
        peers.record_digest(ncon, {**body, "peer_port": 6881}, now=now + 100)      # a digest arrived
        verdict.update({"raise": transport.TransportError("MANIFEST.sig: no metadata after 120s — nobody")})
        got = accept.from_digests([c], {"g": con}, ncon, **dict(kw, now=now + 120))
        assert got["offered"] == 1 and got["deferred"] == 1 and len(taken) == 2
        peers.record_digest(ncon, {**body, "peer_port": 6881}, now=now + 200)
        got = accept.from_digests([c], {"g": con}, ncon, **dict(kw, now=now + 240))
        assert got["deferred"] == 1 and len(taken) == 2, "nobody served it: not asked for again within the backoff"
        got = accept.from_digests([c], {"g": con}, ncon, **dict(kw, now=now + transport.BACKOFF_S + 300, force=True))
        assert len(taken) == 3, "asked for again once the backoff has passed"
        assert accept.listed_bundles(ncon, types.SimpleNamespace(collection_id="sha256:" + "f" * 64,
                                                                 space_id="x", space_segment="a" * 16), con, now=now) == []


def test_a_bundle_lands_whole_across_filesystems_when_state_bundles_is_a_link(tmp_path, monkeypatch):
    """BUNDLES_ROOT (07 §7.8, 2026-10-19): staging on one filesystem, the
    bundles on another — a rename fails with EXDEV; the bundle is copied
    beside its place and renamed in, whole or absent (invariant 2)."""
    import errno
    from pathlib import Path
    src = tmp_path / "staging" / "bundle-x"
    src.mkdir(parents=True)
    for f in mem.BUNDLE_FILES:
        (src / f).write_bytes(f.encode())
    dst = tmp_path / "bundles" / "x" / "y" / ("d" * 64)
    dst.parent.mkdir(parents=True)
    real = Path.replace

    def across(self, target):
        if self == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real(self, target)
    monkeypatch.setattr(Path, "replace", across)
    accept._move_dir(src, dst)
    assert sorted(p.name for p in dst.iterdir()) == sorted(mem.BUNDLE_FILES) and not src.exists()
    assert not dst.with_name(f".{dst.name}.landing").exists()
    # on one filesystem: the one rename
    src2 = tmp_path / "staging" / "bundle-y"
    src2.mkdir()
    (src2 / "a").write_bytes(b"a")
    accept._move_dir(src2, tmp_path / "bundles" / "z")
    assert (tmp_path / "bundles" / "z" / "a").is_file() and not src2.exists()
