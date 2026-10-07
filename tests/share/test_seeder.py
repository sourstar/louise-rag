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
from app.hold import collection as col, drag, snapshot as sn  # noqa: E402
from app.share import members as mem, seeder  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.hold.conftest import a_root, pubmed  # noqa: E402

SLICE = "year=2023"


def a_shared_collection(node, monkeypatch):  # noqa: F811
    """A held, public, in-use collection with two record shards in its drag
    and a snapshot naming them, `share = true` in its file."""
    codex = a_root(node, pubmed())
    name = col.name_of(node, codex.collection_id)
    d = col.drag_of(node, name)
    files = []
    for n, size in ((1, 40_000), (2, 70_000)):
        data = bytes([n]) * size
        p = d / f"records/{SLICE}/{n:05d}.jsonl.zst"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files.append((n, data))
    shards = tuple(sn.Shard(slice=SLICE, n=n, root=drag.pieces_root(data),
                            records_root="sha256:" + "11" * 32, first_id=f"{n}a",
                            last_id=f"{n}z", records=2, chunks=4,
                            chunks_root="sha256:" + "22" * 32, tokens=2048, bytes=len(data))
                   for n, data in files)
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08",
                       previous=None, shards=shards, delta=sn.Delta((), (), ()), files={})
    (d / "snapshots").mkdir(exist_ok=True)
    (d / "snapshots" / "2026-08.json").write_bytes(snap.canonical())
    louise._write_held_file(node, codex, "http://rage:1/upstream/x")
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
    swarm, and ends with the drag's file."""
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
                                         drag.pieces_root(files[2])))
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
    assert (tmp_path / "leech" / member / path).read_bytes() == files[2]
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
