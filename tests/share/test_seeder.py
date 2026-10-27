"""tests/share/test_seeder.py — the seeder (04 §4.3; 09 §9.16; migration step
9b): the worker serves one torrent per member file of a shared collection's
current snapshot, refuses a file that is not what the snapshot says, drops
what is no longer wanted, announces the keys; a peer that computed the
infohash itself fetches the member from our swarm; `share <collection>`
only writes the desired state."""
import json
import time

import pytest

lt = pytest.importorskip("libtorrent")

from app import collections as collections_mod, copy_state, db, licensing, louise  # noqa: E402
from app.acquire import seeding, transport_torrent as tt  # noqa: E402
from app.hold import collection as col, shard as shard_mod, snapshot as sn  # noqa: E402
from app.share import members as mem, seeder  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.hold.conftest import a_root, pubmed  # noqa: E402

SLICE = "year=2023"


def a_shared_collection(node, monkeypatch):  # noqa: F811
    """A held, public, in-use collection with two record shards in its SHARD
    and a snapshot naming them, `share = true` in its file."""
    codex = a_root(node, pubmed())
    name = col.name_of(node, codex.collection_id)
    d = col.shard_dir_of(node, name)
    files = []
    for n, size in ((1, 40_000), (2, 70_000)):
        data = bytes([n]) * size
        p = d / f"records/{SLICE}/{n:05d}.jsonl.zst"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files.append((n, data))
    shards = tuple(sn.Shard(slice=SLICE, n=n, root=shard_mod.pieces_root(data),
                            records_root="sha256:" + "11" * 32, first_id=f"{n}a",
                            last_id=f"{n}z", records=2, chunks=4,
                            chunks_root="sha256:" + "22" * 32, tokens=2048, bytes=len(data))
                   for n, data in files)
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08",
                       previous=None, shards=shards, delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots" / "2026-08.json").write_bytes(snap.canonical())
    louise._write_held_file(node, codex, "http://models:1/upstream/x")
    assert louise.main(["share", "collection", name, "--root", str(node)]) == 0
    c = collections_mod.by_name(name)
    assert c is not None and c.share is True
    monkeypatch.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
    monkeypatch.setattr(licensing, "state_for", lambda c, *a, **k: "acknowledged")
    return c, codex, snap, d, dict(files)


def quiet_sessions(monkeypatch):
    monkeypatch.setattr(tt, "_session", None)
    monkeypatch.setattr(tt, "_session_lock", __import__("threading").Lock())
    monkeypatch.setattr(tt, "session_settings", lambda: {
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False, "alert_mask": 0})
    monkeypatch.setattr(seeding, "_seeder", None)


# ---- publishing -----------------------------------------------------------------

def test_publish_serves_one_torrent_per_member_file_with_the_snapshots_infohash(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    state = node / c.name / "state"
    entries = seeder.publish(c, snap, d, state, con)
    assert len(entries) == 2 and {e["member"] for e in entries} == {f"records-{SLICE}"}
    expected = {t.path: t.infohash[7:] for m in mem.members_of(snap) for t in m.torrents()}
    for e in entries:
        assert e["info_hash"] == expected[e["path"]] and e["snapshot_id"] == "2026-08"
        assert (state / "share" / "2026-08" / e["member"] / e["path"]).read_bytes() == \
            (d / e["path"]).read_bytes()
        assert e["torrent_path"].endswith(f"{e['info_hash']}.torrent")
        ti = lt.torrent_info(e["torrent_path"])
        assert str(ti.info_hashes().v2) == e["info_hash"] and ti.name() == e["member"]
    assert seeder.served(con)["snapshot_id"] == "2026-08" and seeder.served(con)["refused"] == []
    assert len(seeding.read_published(con)) == 2


def test_a_republish_keeps_the_parts_record_lists_of_this_snapshot(node, monkeypatch):  # noqa: F811
    """possession.part_records reads a part's records once per part ROOT into
    state/share/part-uids. The republish swept that directory with the old
    snapshots', so every Library view decoded the whole SHARD again on the
    request path: 170 s every few minutes while gutenberg contributed
    (München, 2026-10-27). Kept now; a root no longer in the snapshot goes."""
    from app.share import possession
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    state = node / c.name / "state"
    parts = state / "share" / possession.PARTS
    parts.mkdir(parents=True)
    held = [parts / f"{str(s.root).split(':')[-1]}.json" for s in snap.shards]
    for p in held:
        p.write_text('[["a:1", "sha256:00"]]', encoding="utf-8")
    gone = parts / ("ab" * 32 + ".json")
    gone.write_text("[]", encoding="utf-8")
    (state / "share" / "2026-07").mkdir()
    seeder.publish(c, snap, d, state, con)
    seeder.publish(c, snap, d, state, con)
    assert all(p.read_text(encoding="utf-8") == '[["a:1", "sha256:00"]]' for p in held)
    assert not gone.exists(), "a part root the snapshot no longer has is pruned"
    assert not (state / "share" / "2026-07").exists(), "an old snapshot's files still go"


def test_a_file_that_is_not_what_the_snapshot_says_is_refused_not_served(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    with open(d / f"records/{SLICE}/00002.jsonl.zst", "ab") as f:
        f.write(b"!")
    con = db.connect(c)
    said: list[str] = []
    entries = seeder.publish(c, snap, d, node / c.name / "state", con, log=said.append)
    assert [e["path"] for e in entries] == [f"records/{SLICE}/00001.jsonl.zst"]
    assert seeder.served(con)["refused"] == [f"records-{SLICE}: records/{SLICE}/00002.jsonl.zst"]
    assert said and "refused" in said[0] and "not what the snapshot says" in said[0]


# ---- the swarm ---------------------------------------------------------------------

def test_a_peer_fetches_a_member_by_the_infohash_it_computed_itself(node, monkeypatch, tmp_path):  # noqa: F811
    """04 §4.3 end to end on localhost: the worker's seeder serves the
    members; a leecher that knows only the infohash (from the snapshot, not
    from us) gets the metadata, the piece layers and the bytes from our
    swarm, and ends with the SHARD's file."""
    quiet_sessions(monkeypatch)
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    got = seeder.sync([c], {c.name: con}, con, "running")
    assert got["published"] == [c.name]
    seeding.tick([c], {c.name: con}, con, "running")
    rows = seeding.get_seeder().table()
    assert len(rows) == 2, rows
    seed_port = tt.get_session().listen_port()
    member = f"records-{SLICE}"
    path = f"records/{SLICE}/00002.jsonl.zst"
    infohash = mem.infohash(mem.info_dict(member, path, len(files[2]),
                                         shard_mod.pieces_root(files[2])))
    leech = lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                        "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
                        "alert_mask": 0})
    params = lt.parse_magnet_uri(f"magnet:?xt=urn:btmh:1220{infohash[7:]}")
    params.save_path = str(tmp_path / "leech")
    h = leech.add_torrent(params)
    deadline = time.time() + 90
    while not h.status().is_seeding and time.time() < deadline:
        h.connect_peer(("127.0.0.1", seed_port))
        seeding.tick([c], {c.name: con}, con, "running")
        time.sleep(0.5)
    assert h.status().is_seeding, "the peer must complete from our seed"
    # `is_seeding` says every piece is verified, not that libtorrent has laid
    # the file out on disk yet: read right after it, the leech's file was
    # missing once in the node's staged pre-check (2026-10-20) — wait for it,
    # bounded, before comparing
    landed = tmp_path / "leech" / member / path
    deadline = time.time() + 30
    while time.time() < deadline and not (landed.is_file() and landed.stat().st_size == len(files[2])):
        time.sleep(0.1)
    assert landed.read_bytes() == files[2]
    # a second beat changes nothing; stopping drops both torrents
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == []
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--stop"]) == 0
    c2 = collections_mod.by_name(c.name)
    assert c2.share is False
    got = seeder.sync([c2], {c.name: con}, con, "running")
    assert got["unpublished"] == [c.name] and seeding.read_published(con) == []
    assert seeding.get_seeder().table() == []
    leech.remove_torrent(h)


# ---- announcing ----------------------------------------------------------------------

class FakeSession:
    def __init__(self):
        self.calls = []

    def dht_announce(self, h, port, flags):
        self.calls.append((h.to_bytes(), port, flags))


def test_the_keys_are_announced_once_per_interval_and_only_when_the_mode_seeds(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    monkeypatch.setattr(seeder, "_last_announce", {})
    s = FakeSession()
    keys = seeder.keys_for(codex, snap, col.resolver(node))
    assert len(keys) == 2 and all(len(k) == 20 for k in keys)
    assert keys[0] == mem.collection_key(codex.collection_id, "2026-08")
    assert keys[1] == mem.lineage_key(codex.collection_id, "2026-08"), "a root is its own lineage"
    assert seeder.announce(s, keys, 30042, now=1000.0) == 1, "one key twice over is one announce"
    assert s.calls == [(keys[0], 30042, 0)]
    assert seeder.announce(s, keys, 30042, now=1001.0) == 0
    assert seeder.announce(s, keys, 30042, now=1000.0 + seeder.ANNOUNCE_EVERY_S) == 1
    monkeypatch.setattr(seeder, "_last_announce", {})
    assert seeder.sync([c], {c.name: con}, con, "paused", session=s, port=30042)["announced"] == 0
    assert seeder.sync([c], {c.name: con}, con, "seed-only", session=s, port=30042)["announced"] == 1


def test_the_swarm_key_is_announced_with_the_swarms_port_three_keys_per_snapshot(node, monkeypatch):  # noqa: F811
    """04 §4.2: the members' swarms are found under the snapshot's swarm key,
    announced with the swarm's port — not by each member's own infohash,
    which put 3,170 announcements on the DHT for one collection and filled a
    router's NAT table (docs/notes/dht-flood.md). The announcements a
    snapshot costs do not grow with its members."""
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    monkeypatch.setattr(seeder, "_last_announce", {})
    s = FakeSession()
    got = seeder.sync([c], {c.name: con}, con, "running", session=s, port=30042, peer_port=6881)
    swarm = mem.swarm_key(codex.collection_id, "2026-08")
    assert len(swarm) == 20 and swarm not in seeder.keys_for(codex, snap, col.resolver(node))
    assert got["announced"] == 1 and got["swarm"] == 1
    assert s.calls == [(mem.collection_key(codex.collection_id, "2026-08"), 30042, 0), (swarm, 6881, 0)],         "the peer messages' port under the collection key, the swarm's under the swarm key"
    assert seeder.sync([c], {c.name: con}, con, "running", session=s, port=30042, peer_port=6881)["swarm"] == 0,         "once per interval"
    assert seeder.swarm_port(s, None) is None, "a session that names no port announces no swarm"


# ---- who is served ------------------------------------------------------------------

def test_a_copy_in_quarantine_or_not_wanting_it_is_not_served(node, monkeypatch):  # noqa: F811
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    monkeypatch.setattr(copy_state, "of",
                        lambda collection: copy_state.CopyState("quarantine", ("the gate",),
                                                                "pending", True))
    assert seeder.wants_serving(c) is False
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == []
    assert seeder.served(con) is None


def test_a_never_share_codex_is_refused(node, monkeypatch, capsys):  # noqa: F811
    """(`--slices`/`--kinds` narrow serving since 2026-10-07: the narrowing test.)"""
    a_shared_collection(node, monkeypatch)
    from tests.deposit.test_deposit_identity import make
    make(node, "alice")                             # a never-share collection (02 §2.9)
    assert louise.main(["share", "collection", "alice", "--root", str(node)]) == 2
    assert "never-share" in capsys.readouterr().err


def test_the_spec_s_bare_form_share_collection_works(node, monkeypatch):  # noqa: F811
    """07 §7.3 writes `share <collection> [--stop]` with no noun between."""
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    assert louise.main(["share", c.name, "--root", str(node), "--stop"]) == 0
    assert collections_mod.by_name(c.name).share is False
    assert louise.main(["share", c.name, "--root", str(node)]) == 0
    assert collections_mod.by_name(c.name).share is True


def test_share_narrows_what_is_served_and_a_changed_narrowing_is_served_again(node, monkeypatch):  # noqa: F811
    """04 §4.3 / 07 §7.8, 2026-10-07: `share --slices … --kinds …` writes
    `share_slices` / `share_kinds`; the seeder serves only those, and a
    whole-collection member always."""
    from app import collections as collections_mod
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    assert seeder.serves(c, f"records-{SLICE}") and seeder.serves(c, "validation")
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--kinds", "vectors"]) == 0
    c = collections_mod.by_name(c.name)
    assert c.share_kinds == ("vectors",) and c.share is True
    assert not seeder.serves(c, f"records-{SLICE}") and seeder.serves(c, "validation"), \
        "a narrowed kind is not served; a whole-collection member always is"
    con = db.connect(c)
    seeder.sync([c], {c.name: con}, con, "running")
    assert [e for e in seeding.read_published(con) if e.get("member", "").startswith("records-")] == []
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--kinds", "all",
                        "--slices", SLICE]) == 0
    c = collections_mod.by_name(c.name)
    assert c.share_kinds is None and c.share_slices == (SLICE,)
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == [c.name], \
        "the snapshot did not change, the narrowing did: served again"
    assert {e["member"] for e in seeding.read_published(con)} >= {f"records-{SLICE}"}
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--kinds", "nonsense"]) == 2


def test_a_slice_is_named_as_the_snapshot_lists_it(node, monkeypatch, capsys):  # noqa: F811
    """`--slices la` matched no member (`records-lang=la`) and served no part,
    silently (2026-10-12): a name the snapshot does not list is refused and
    the right spelling named."""
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    value = SLICE.split("=", 1)[1]
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--slices", value]) == 2
    assert SLICE in capsys.readouterr().err


def test_the_digest_offers_what_is_served_not_every_member_held(node, monkeypatch):  # noqa: F811
    """2026-10-18: the node served two slices (`share_slices`) and its digest
    listed every member it held; a peer wanting every slice hung on the first
    part nobody served. The digest's `members` is what the seeder serves
    (04 §4.2: announcing what you do not serve would be a lie)."""
    from app import collections as collections_mod
    from app.share import gossip
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    (entry,) = gossip.entries(node, [c])
    assert [m["name"] for m in entry["members"]] == [f"records-{SLICE}"]
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--kinds", "vectors"]) == 0
    c = collections_mod.by_name(c.name)
    (entry,) = gossip.entries(node, [c])
    assert entry["members"] == [], "a kind narrowed out is not offered"
    assert louise.main(["share", "collection", c.name, "--root", str(node), "--kinds", "all",
                        "--slices", SLICE]) == 0
    c = collections_mod.by_name(c.name)
    (entry,) = gossip.entries(node, [c])
    assert [m["name"] for m in entry["members"]] == [f"records-{SLICE}"]


def test_a_bundle_that_lands_after_the_publish_is_served_without_rehashing_the_rest(node, monkeypatch):  # noqa: F811
    """Live on the node, 2026-10-19 (p2p-review-open-issues.md §8): 331
    bundles made after the seeder's publish were offered in the digest and
    announced, and none was seeded — `sync` published only on a snapshot or
    selection change. Now the held bundles are listed at most once a minute,
    a change republishes, and a publish within a snapshot hashes only what
    is new."""
    from app.share import gossip
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    assert codex.space is not None
    con = db.connect(c)
    state = node / c.name / "state"
    monkeypatch.setattr(seeder, "_last_bundle_scan", {})
    built: list[str] = []
    real = seeder.build_torrent

    def counted(member_dir, torrent, out_dir):
        built.append(torrent.path)
        return real(member_dir, torrent, out_dir)
    monkeypatch.setattr(seeder, "build_torrent", counted)
    t0 = 1_800_000_000.0
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0)["published"] == [c.name]
    assert len(seeding.read_published(con)) == 2 and len(built) == 2
    first = seeder.served(con)["bundles"]
    assert first == seeder.bundles_fingerprint(state, codex.space_segment)
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0 + 10)["published"] == [], "nothing changed"
    # a bundle lands (share contribute, or accept) after the publish
    bdir = state / "bundles" / codex.space_segment / SLICE / "00001" / ("b" * 64)
    bdir.mkdir(parents=True)
    for f in mem.BUNDLE_FILES:
        (bdir / f).write_bytes(f.encode() * 50)
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0 + 20)["published"] == [], \
        "within the scan interval: not listed yet"
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0 + 70)["published"] == [c.name], \
        "a minute on: the held set changed, served again"
    published = seeding.read_published(con)
    assert len(published) == 6 and len(built) == 6, "the bundle's four files hashed, the two records not again"
    assert {e["member"] for e in published} == {f"records-{SLICE}", f"bundle-{codex.space_segment}-{'b' * 64}"}
    assert seeder.served(con)["bundles"] != first
    (entry,) = gossip.entries(node, [c])
    assert {m["name"] for m in entry["members"]} == {e["member"] for e in published}, \
        "the digest offers exactly what is served"
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0 + 140)["published"] == [], "the same set: nothing"
    # the bundle goes (an operator's hand): served again without it, its torrents gone
    import shutil
    shutil.rmtree(bdir)
    assert seeder.sync([c], {c.name: con}, con, "running", now=t0 + 210)["published"] == [c.name]
    published = seeding.read_published(con)
    assert len(published) == 2 and len(built) == 6, "nothing hashed again"
    assert not any("b" * 64 in p.name for p in (state / "share" / "torrents").glob("*.torrent")), \
        "no torrent of a member no longer held"
    # the seeding thread picks the published list up as it is
    quiet_sessions(monkeypatch)
    seeding.tick([c], {c.name: con}, con, "running")
    assert len(seeding._seeder._handles) == 2


def test_a_held_bundle_is_hashed_once_not_at_every_digest_and_publish(node, monkeypatch):  # noqa: F811
    """Measured on the node, 2026-10-19: `bundle_members` hashed all four
    files of every held bundle at every digest and every publish — 385
    bundles, 10.7 GiB, 8 s a call, growing with what is held. The roots are
    cached on each file's physical identity (path, size, mtime_ns); a file
    is hashed once per bundle, again only when it moved."""
    import json
    from app.share import gossip
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    con = db.connect(c)
    state = node / c.name / "state"
    space = codex.space_segment
    hashed: list[str] = []
    real = mem.shard_mod.pieces_root_of

    def counted(path):
        hashed.append(str(path))
        return real(path)
    monkeypatch.setattr(mem.shard_mod, "pieces_root_of", counted)

    def land(hex_name: str, salt: bytes = b""):
        bdir = state / "bundles" / space / SLICE / "00001" / hex_name
        bdir.mkdir(parents=True)
        for f in mem.BUNDLE_FILES:
            (bdir / f).write_bytes(f.encode() * 50 + salt)
        return bdir
    land("a" * 64)
    land("b" * 64)
    first = mem.bundle_members(state, space)
    assert len(first) == 2 and len(hashed) == 8, "two new bundles: eight files hashed"
    assert mem.bundle_members(state, space) == first and len(hashed) == 8, "the same set: nothing hashed"
    cache = json.loads((state / "share" / mem.ROOTS_CACHE).read_bytes())
    assert set(cache) == {f"{space}/{SLICE}/00001/{'a' * 64}", f"{space}/{SLICE}/00001/{'b' * 64}"}
    monkeypatch.setattr(seeder, "_last_bundle_scan", {})
    seeder.sync([c], {c.name: con}, con, "running", now=1_800_000_000.0)
    (entry,) = gossip.entries(node, [c])
    assert len([m for m in entry["members"] if m["name"].startswith("bundle-")]) == 2
    assert len(hashed) == 8, "a publish and a digest: nothing hashed again"
    third = land("c" * 64)
    seeder.sync([c], {c.name: con}, con, "running", now=1_800_000_000.0 + 61)
    assert len(hashed) == 12 and len(seeding.read_published(con)) == 2 + 12, "the new bundle's four files, once"
    gossip.entries(node, [c])
    assert len(hashed) == 12
    # a file that moved is hashed again; a bundle gone leaves the cache
    import os
    os.utime(third / mem.BUNDLE_FILES[0], ns=(1, 1))
    mem.bundle_members(state, space)
    assert len(hashed) == 16, "a file that moved: its bundle hashed again, whole"
    import shutil
    shutil.rmtree(third)
    mem.bundle_members(state, space)
    cache = json.loads((state / "share" / mem.ROOTS_CACHE).read_bytes())
    assert len(cache) == 2 and not any("c" * 64 in k for k in cache)
    # a cache that is not one is recomputed, not trusted
    (state / "share" / mem.ROOTS_CACHE).write_bytes(b"{not json")
    assert len(mem.bundle_members(state, space)) == 2 and len(hashed) == 16 + 8
