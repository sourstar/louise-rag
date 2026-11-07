"""tests/share/test_frozen.py — FREEZE, the share side (04 §4.3, §4.5,
amended 2026-11-07; operator: "you can still share (if you check the shared
box) the parts that are still aligned with the non frozen state";
docs/notes/collection-freeze.md §3).

A frozen collection takes nothing in from peers; shared, it offers only the
files whose torrent (09 §9.16: member, path, size, root) is that file's in
the newest snapshot it has read — its own, or a newer one a neighbour's
digest lists, read ASIDE and never into the copy — and the bundles of those
parts. With no newer snapshot read, everything held is aligned."""
import dataclasses
import time
from pathlib import Path

import pytest

lt = pytest.importorskip("libtorrent")

from fastapi.testclient import TestClient  # noqa: E402

from app import api, copy_state, db, licensing, node_state  # noqa: E402
from app.acquire import seeding  # noqa: E402
from app.hold import collection as col, shard as shard_mod, snapshot as sn  # noqa: E402
from app.hold.codex import canonical_json  # noqa: E402
from app.share import accept, attest, contribute, fetch, frozen, gossip, members as mem, peers, rendezvous, \
    seeder, transport  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.hold.conftest import KEY, NOW  # noqa: E402
from tests.share.test_accept import C  # noqa: E402
from tests.share.test_contribute import attested, ready  # noqa: F401, E402
from tests.share.test_fetch import OTHER_KEY, SHARER, digest, entry, fetcher  # noqa: F401, E402
from tests.share.test_messages import PEER, PEER_KEY, a_shard  # noqa: E402
from tests.share.test_seeder import SLICE, a_shared_collection  # noqa: E402
from tests.share.test_share_api import OURS, OUR_KEY  # noqa: E402


def changed(s: sn.Shard, data: bytes) -> sn.Shard:
    """The part re-issued upstream with other bytes, under its own number."""
    return dataclasses.replace(s, root=shard_mod.pieces_root(data), bytes=len(data))


def newer(first: sn.Snapshot, shards, sid: str = "2026-09") -> sn.Snapshot:
    return sn.Snapshot(collection_id=first.collection_id, snapshot_id=sid, previous=first.hash,
                       shards=tuple(shards), delta=sn.Delta((), (), ()), files=first.files)


def read_aside(root: Path, name: str, snap: sn.Snapshot) -> None:
    """What `frozen.track` keeps after a pull: the newer snapshot, aside."""
    d = frozen.aside_dir(root, name)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.json"):
        old.unlink()
    (d / f"{snap.snapshot_id}.json").write_bytes(snap.canonical())


def published_paths(con) -> list[str]:
    return sorted(e["path"] for e in seeding.read_published(con) if e.get("member"))


# ---- aligned, exactly ---------------------------------------------------------------------

def test_aligned_is_the_same_torrent_in_the_newest_snapshot_read(tmp_path):
    p1, p2 = (sn.Shard(slice=SLICE, n=n, root=shard_mod.pieces_root(bytes([n]) * 100), records_root="sha256:" + "1" * 64,
                       first_id=f"{n}a", last_id=f"{n}z", records=2, chunks=4, chunks_root="sha256:" + "2" * 64,
                       tokens=8, bytes=100) for n in (1, 2))
    files = {"validation.json": {"root": "sha256:" + "3" * 64, "bytes": 10}}
    cid = "sha256:" + "c" * 64
    own = sn.Snapshot(collection_id=cid, snapshot_id="2026-08", previous=None, shards=(p1, p2),
                      delta=sn.Delta((), (), ()), files=files)
    p3 = dataclasses.replace(p1, n=3)
    up = newer(own, (p1, changed(p2, b"x" * 120), p3))
    al = frozen.alignment(own, up)
    assert not al.everything and al.newest_id == "2026-09" and al.held == 3
    assert al.file(p1.path, p1.root, p1.bytes) and al.part(SLICE, 1)
    assert not al.file(p2.path, p2.root, p2.bytes) and not al.part(SLICE, 2), "re-issued upstream"
    assert not al.file(p1.path, p1.root, p1.bytes + 1), "the size is in the torrent too"
    assert al.file("validation.json", "sha256:" + "3" * 64, 10), "a whole-collection file, by the same rule"
    bundles = tmp_path / "state" / "bundles" / ("a" * 16)
    assert al.bundle_dir(bundles / SLICE / "00001" / ("ab" * 32)) is True
    assert al.bundle_dir(bundles / SLICE / "00002" / ("ab" * 32)) is False, "a bundle of a part changed upstream"
    member = mem.members_of(own)[0]
    assert member.name == f"records-{SLICE}" and al.member(member) is False, "listed only whole"
    assert frozen.offered(al, [member]) == [] and frozen.offered(None, [member]) == [member]
    # no newer snapshot read, an older one, another collection's: everything is aligned
    for other in (None, own, dataclasses.replace(up, snapshot_id="2026-07"),
                  dataclasses.replace(up, collection_id="sha256:" + "d" * 64)):
        everything = frozen.alignment(own, other)
        assert everything.everything and everything.member(member) and everything.bundle_dir(None)


# ---- the seeder serves only what is aligned ------------------------------------------------------

class FakeSession:
    def __init__(self):
        self.announced: list[bytes] = []

    def dht_announce(self, h, port, flags):
        self.announced.append(h.to_bytes())

    def listen_port(self):
        return 6881


def test_the_seeder_serves_only_the_aligned_files_and_follows_the_freeze(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    monkeypatch.setattr(seeder, "_last_announce", {})
    seeder.sync([c], {c.name: con}, con, "running")
    both = [f"records/{SLICE}/00001.jsonl.zst", f"records/{SLICE}/00002.jsonl.zst"]
    assert published_paths(con) == both and "aligned_with" not in seeder.served(con)

    node_state.set_frozen(c.name, True, by="test")
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == [c.name], "frozen: served again"
    assert published_paths(con) == both, "no newer snapshot read: everything held is aligned"
    assert seeder.served(con)["aligned_with"] == "frozen:own"
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == [], "nothing moved: not again"

    up = newer(snap, (snap.shards[0], changed(snap.shards[1], b"q" * 70_001)))
    read_aside(node, c.name, up)
    said: list[str] = []
    assert seeder.sync([c], {c.name: con}, con, "running", log=said.append)["published"] == [c.name]
    assert published_paths(con) == [both[0]], "the part re-issued upstream is not served"
    rec = seeder.served(con)
    assert rec["aligned_with"] == f"frozen:2026-09:{up.hash}" and rec["withheld"] == 1 and rec["torrents"] == 1
    assert any("frozen: 1 file(s) changed upstream" in s for s in said)
    assert frozen.summary(node, c, snap) == {"snapshot_id": "2026-08", "newest_read": "2026-09",
                                             "aligned_files": 1, "files": 2}, "what a display shows"
    gone = next(t for m in mem.members_of(snap) for t in m.torrents() if t.path == both[1])
    assert not (node / c.name / "state" / "share" / "torrents" / f"{gone.infohash[7:]}.torrent").exists(), \
        "its torrent file goes with it"

    # nothing aligned: nothing served, and nothing announced (04 §4.2)
    read_aside(node, c.name, newer(snap, [changed(s, b"z" * (s.bytes + 1)) for s in snap.shards], "2026-10"))
    session = FakeSession()
    seeder.sync([c], {c.name: con}, con, "running", session=session, port=8742)
    assert published_paths(con) == [] and session.announced == []

    # UNFROZEN: served whole again, and announced
    node_state.set_frozen(c.name, False, by="test")
    assert seeder.sync([c], {c.name: con}, con, "running", session=session, port=8742)["published"] == [c.name]
    assert published_paths(con) == both and "aligned_with" not in seeder.served(con)
    assert session.announced, "announced again"


# ---- the digest lists only what is aligned --------------------------------------------------------

def test_the_digest_lists_a_member_only_when_every_file_of_it_is_aligned(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)

    def members():
        got = gossip.entries(node, [c])
        return None if not got else [m["name"] for m in got[0]["members"]]
    assert members() == [f"records-{SLICE}"]
    node_state.set_frozen(c.name, True, by="test")
    assert members() == [f"records-{SLICE}"], "frozen, no newer snapshot read: as before"
    read_aside(node, c.name, newer(snap, (snap.shards[0], changed(snap.shards[1], b"q" * 70_001))))
    got = gossip.entries(node, [c])
    assert got and got[0]["members"] == [], "partly aligned: not listed (its aligned file is served by torrent)"
    assert got[0]["snapshot_id"] == "2026-08" and got[0]["snapshot_hash"] == snap.hash, \
        "the digest names the snapshot this peer holds"
    read_aside(node, c.name, newer(snap, [changed(s, b"z" * (s.bytes + 1)) for s in snap.shards], "2026-10"))
    assert members() is None, "nothing aligned: not announced at all"
    node_state.set_frozen(c.name, False, by="test")
    assert members() == [f"records-{SLICE}"]


def test_a_frozen_copy_leads_nothing(isolated_config):
    class Held:
        name, share, embed_mode_default = "gutenberg", True, "p2p"
    assert gossip.leads(Held(), frozen=set()) is True
    assert gossip.leads(Held(), frozen={"gutenberg"}) is False
    node_state.set_frozen("gutenberg", True)
    assert gossip.leads(Held()) is False, "read from the node's store when not given"


def test_the_publisher_bundles_only_the_parts_still_current_upstream(ready):  # noqa: F811
    """A frozen collection's publisher (`_publish_pass`) hands `contribute.once`
    the alignment's part test: no `bundle` event names a part of a version
    the network has left."""
    from app import embeddings
    from app.hold import land as landing, records as rec, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    first = snapshot.shards[0]
    vb.build(codex, shards=[first],
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    alone = [rendezvous.Peer(OUR_KEY, 1000.0)]
    held = dict(held_books=lambda ids: embeddings.books_with_vectors(
        ids, collection=rec.local_collection(root, "gutenberg", codex)))
    sid = f"{first.slice}/{first.n:05d}"
    out = contribute.once(root, "gutenberg", codex, live=alone, build=False,
                          parts=lambda s: (s.slice, s.n) != (first.slice, first.n), **held, **kw)
    assert out["held"] == [] and out["bundles"] == [] and out["events"] == 0
    assert sid in out["frozen_withheld"]
    out = contribute.once(root, "gutenberg", codex, live=alone, build=False, **held, **kw)
    assert out["held"] == [sid] and out["frozen_withheld"] == [], "not frozen: bundled as before"


# ---- nothing is taken in --------------------------------------------------------------------

def test_a_frozen_collection_takes_no_member_no_newer_snapshot_and_no_bundle(node, monkeypatch):  # noqa: F811
    from app import collections as collections_mod, louise
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    louise._set_held_selection(collections_mod.config_toml_path(c.name), "all", ("records",))
    c = collections_mod.by_name(c.name)
    for name in ("_complete", "_next_try", "_tried_hints"):
        monkeypatch.setattr(transport, name, {})
    monkeypatch.setattr(transport, "_frozen_said", set())
    fetched, newer_for, tracked, taken = [], [], [], []
    monkeypatch.setattr(transport, "fetch_members", lambda root, name, *a, **k: fetched.append(name) or
                        {"errors": [], "promoted": [], "missing": 0, "stopped": False, "deferred": []})
    monkeypatch.setattr(accept, "newer_from_digests", lambda root, cols, *a, **k: newer_for.append(
        [x.name for x in cols]) or {})
    monkeypatch.setattr(frozen, "track", lambda root, cols, *a, **k: tracked.append([x.name for x in cols]) or {})
    monkeypatch.setattr(accept, "beat", lambda cols, *a, **k: taken.append(set(k.get("frozen", ()))) or {})
    for p in (d / "records").rglob("*.jsonl.zst"):
        p.unlink()                                          # a member to fetch, were it not frozen
    con = db.connect(c)
    ncon = peers.connect()
    node_state.set_frozen(c.name, True, by="test")
    said: list[str] = []
    kw = dict(worker_state="running", session_factory=lambda: None, root=node)
    transport.beat([c], {c.name: con}, ncon, now=1000.0, log=said.append, **kw)
    assert fetched == [], "frozen: no member fetched"
    assert newer_for == [[]], "no newer snapshot taken into the copy"
    assert tracked == [[c.name]], "the newer snapshot is read aside instead"
    assert taken == [{c.name}], "and the bundle intake is told which collections are frozen"
    assert sum("frozen" in s and "no member, no newer snapshot, no bundle" in s for s in said) == 1
    transport.beat([c], {c.name: con}, ncon, now=1100.0, log=said.append, **kw)
    assert sum("no member, no newer snapshot" in s for s in said) == 1, "said once per freeze"
    # UNFROZEN: the members follow, the snapshot is taken in, the aside copy goes
    read_aside(node, c.name, newer(snap, snap.shards))
    node_state.set_frozen(c.name, False, by="test")
    transport.beat([c], {c.name: con}, ncon, now=2000.0, log=said.append, **kw)
    assert fetched == [c.name] and newer_for[-1] == [c.name]
    assert len(tracked) == 2, "read aside on each frozen beat, never once unfrozen"
    assert taken[-1] == set()
    assert not (node / c.name / "state" / frozen.DIR).exists(), "what was read aside is dropped"
    ncon.close()


def test_bundle_and_snapshot_events_wait_unacted_for_the_unfreeze(node, monkeypatch):  # noqa: F811
    codex, name, d, first = a_shard(node)
    c = C(name, codex)
    for fn in ("codexes", "flags", "reproduce_forgeries", "reproduce_claims"):
        monkeypatch.setattr(accept, fn, lambda *a, **k: {})
    from app.share import deprecate, withdraw
    monkeypatch.setattr(withdraw, "drop_withdrawn", lambda *a, **k: {"dropped": [], "not_the_producer": []})
    monkeypatch.setattr(deprecate, "expire", lambda *a, **k: {"expired": []})
    monkeypatch.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
    monkeypatch.setattr(licensing, "state_for", lambda c, *a, **k: "acknowledged")
    monkeypatch.setattr(transport, "hints_for", lambda *a, **k: [])
    bundle_ev = {"id": "b1", "envelope": {"body": {"payload": {"collection": codex.collection_id,
                                                                "bundle_id": "sha256:" + "e" * 64}}}}
    snap_ev = {"id": "s1", "envelope": {"body": {"key": PEER_KEY, "payload": {
        "collection_id": codex.collection_id, "snapshot_id": "2026-09", "snapshot_hash": "sha256:" + "9" * 64}}}}
    monkeypatch.setattr(accept, "pending", lambda con, type_="bundle": [bundle_ev] if type_ == "bundle" else [snap_ev])
    acted, took, offered = [], [], []
    monkeypatch.setattr(attest, "mark_acted", lambda con, ev_id: acted.append(ev_id))
    monkeypatch.setattr(accept, "take", lambda *a, **k: took.append(1) or {"outcome": "accepted"})
    monkeypatch.setattr(accept, "from_digests", lambda cols, *a, **k: offered.append([x.name for x in cols]) or
                        {"accepted": 0, "refused": 0, "held": 0, "offered": 0})
    ncon = peers.connect()
    kw = dict(session_factory=lambda: None, root=node, now=time.time())
    out = accept.beat([c], {name: object()}, ncon, frozen={name}, **kw)
    assert took == [] and acted == [], "frozen: nothing taken, and nothing marked acted — it waits"
    assert out["deferred"] == 1 and out["snapshots"]["waiting"] == 1 and out["snapshots"]["pulled"] == 0
    assert offered == [[]], "the digests' offers are not read for it"
    out = accept.beat([c], {name: object()}, ncon, **kw)
    assert took == [1] and "b1" in acted and out["accepted"] == 1, "unfrozen: the waiting bundle is taken in"
    assert offered[-1] == [name]
    ncon.close()


# ---- reading the newer snapshot, aside ---------------------------------------------------------------

def test_the_newer_snapshot_a_digest_lists_is_read_aside_and_the_copy_does_not_move(node, fetcher, monkeypatch):  # noqa: F811
    monkeypatch.setattr(frozen, "_tried", {})
    codex, name, d, first = a_shard(node)
    client = TestClient(api.app)
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client, key=OURS, own_key=OUR_KEY)
    fc = C(got["name"], codex)
    up = newer(first, [changed(first.shards[0], b"s" * 20_001)])
    (d / "snapshots/2026-09.json").write_bytes(up.canonical())
    (d / "snapshots/2026-09.sig.json").write_bytes(canonical_json(sn.sidecar_add(None, up.hash, KEY, now=NOW)))
    now = time.time()
    ncon = peers.connect()
    kw = dict(client=client, key=OURS, own_key=OUR_KEY, now=now)
    cid = codex.collection_id
    peers.record_digest(ncon, digest(PEER_KEY, SHARER, [entry(cid, cid, "2026-08", snapshot_hash=first.hash)], now),
                        now=now)
    assert frozen.track(fetcher, [fc], ncon, **kw) == {"read": 0, "failed": 0, "waiting": 0}, \
        "the snapshot it lists is the one held: nothing newer to read"
    peers.record_digest(ncon, digest(PEER_KEY, SHARER, [entry(cid, cid, "2026-09", snapshot_hash=up.hash)], now),
                        now=now)
    said: list[str] = []
    assert frozen.track(fetcher, [fc], ncon, log=said.append, **kw)["read"] == 1, said
    assert (frozen.aside_dir(fetcher, fc.name) / "2026-09.json").read_bytes() == up.canonical()
    fshard = col.shard_dir_of(fetcher, fc.name)
    assert not (fshard / "snapshots/2026-09.json").exists(), "never into the copy"
    assert seeder.current_snapshot(fshard).snapshot_id == "2026-08"
    assert any("kept aside" in s and "0 of 1 file(s) held are still current" in s for s in said)
    al = frozen.alignment_for(fetcher, fc, first, frozen={fc.name})
    assert al is not None and not al.everything and not al.part("year=2023", 1)
    assert frozen.alignment_for(fetcher, fc, first, frozen=set()) is None, "not frozen: nothing filters"
    assert frozen.track(fetcher, [fc], ncon, **kw)["read"] == 0, "read already"
    # a digest naming a snapshot its sender will not hand over changes nothing,
    # and is asked for again later, not every beat
    peers.record_digest(ncon, digest(PEER_KEY, SHARER, [entry(cid, cid, "2026-10")], now), now=now)
    assert frozen.track(fetcher, [fc], ncon, **kw)["failed"] == 1
    assert frozen.track(fetcher, [fc], ncon, **kw) == {"read": 0, "failed": 0, "waiting": 1}
    assert frozen.reference(fetcher, fc.name).snapshot_id == "2026-09", "the last one read still applies"
    assert frozen.clear(fetcher, fc.name) and frozen.reference(fetcher, fc.name) is None
    ncon.close()


def test_a_snapshot_whose_hash_is_not_the_one_advertised_is_not_kept(node, fetcher, monkeypatch):  # noqa: F811
    monkeypatch.setattr(frozen, "_tried", {})
    codex, name, d, first = a_shard(node)
    client = TestClient(api.app)
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client, key=OURS, own_key=OUR_KEY)
    fc = C(got["name"], codex)
    up = newer(first, first.shards)
    (d / "snapshots/2026-09.json").write_bytes(up.canonical())
    (d / "snapshots/2026-09.sig.json").write_bytes(canonical_json(sn.sidecar_add(None, up.hash, KEY, now=NOW)))
    now = time.time()
    ncon = peers.connect()
    cid = codex.collection_id
    peers.record_digest(ncon, digest(PEER_KEY, SHARER, [entry(cid, cid, "2026-09")], now), now=now)  # a wrong hash
    out = frozen.track(fetcher, [fc], ncon, client=client, key=OURS, own_key=OUR_KEY, now=now)
    assert out["failed"] == 1 and frozen.reference(fetcher, fc.name) is None
    assert peers.is_banned(ncon, PEER_KEY, now=now), "a lie (04 §4.2)"
    # the right hash and no signature on it: pulled, and not kept either
    monkeypatch.setattr(frozen, "_tried", {})
    (d / "snapshots/2026-09.sig.json").unlink()
    peers.record_digest(ncon, digest(OTHER_KEY, SHARER, [entry(cid, cid, "2026-09", snapshot_hash=up.hash)], now),
                        now=now)
    said: list[str] = []
    out = frozen.track(fetcher, [fc], ncon, client=client, key=OURS, own_key=OUR_KEY, now=now, log=said.append)
    assert out["failed"] == 1 and frozen.reference(fetcher, fc.name) is None
    assert any("no verified signature" in s for s in said)
    ncon.close()
