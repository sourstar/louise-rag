"""tests/share/test_transport.py — members over the wire (04 §4.3; 09
§9.16), migration step 9d's second brick, on localhost: two peers, the
sharer's seeder and the fetcher's session, the members of the snapshot
the fetcher pulled arriving by the infohashes it computed itself, verified
and promoted; the ack gate; a stop that leaves staging for the next beat;
the peer hints read from digests."""
import sqlite3
import threading
import time

import pytest

lt = pytest.importorskip("libtorrent")

from fastapi.testclient import TestClient  # noqa: E402

from app import api, db  # noqa: E402
from app.acquire import seeding, transport_torrent as tt  # noqa: E402
from app.hold import collection as col, snapshot as sn  # noqa: E402
from app.hold.codex import canonical_json  # noqa: E402
from app.share import fetch, holdings, messages, peers, seeder, transport  # noqa: E402
from tests.deposit.test_deposit_identity import node  # noqa: F401, E402
from tests.hold.conftest import KEY, NOW  # noqa: E402
from tests.share.test_fetch import OTHER_KEY, SHARER, fetcher  # noqa: F401, E402
from tests.share.test_messages import PEER_KEY  # noqa: E402
from tests.share.test_seeder import SLICE, a_shared_collection, quiet_sessions  # noqa: E402
from tests.share.test_share_api import OURS, OUR_KEY  # noqa: E402


def leech_session():
    return lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
                       "enable_upnp": False, "enable_natpmp": False, "alert_mask": 0})


def manifest(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None)
    con.row_factory = sqlite3.Row
    return con


def test_two_peers_on_localhost_the_members_follow_the_snapshot(node, fetcher, monkeypatch):  # noqa: F811
    quiet_sessions(monkeypatch)
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    (d / "snapshots/2026-08.sig.json").write_bytes(
        canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    con = db.connect(c)
    assert seeder.sync([c], {c.name: con}, con, "running")["published"] == [c.name]
    seeding.tick([c], {c.name: con}, con, "running")
    seed_port = tt.get_session().listen_port()
    # the fetcher: the pre-ack set by pull, then the members by torrent
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    assert got["snapshots"]["promoted"] == ["2026-08"]
    fname = got["name"]
    fd = col.drag_of(fetcher, fname)
    fcon = manifest(fetcher / fname / "state" / "manifest.sqlite")
    said: list[str] = []
    gated = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                    ack_missing=True, session=leech_session(), con=fcon,
                                    hints=[("127.0.0.1", seed_port)], log=said.append)
    assert gated["promoted"] == [] and gated["waiting"].startswith("acknowledgement"), \
        "red: nothing but terms before the ack, and this snapshot lists no terms member"
    out = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=leech_session(), con=fcon,
                                  hints=[("127.0.0.1", seed_port)], log=said.append)
    assert out["promoted"] == [f"records/{SLICE}/00001.jsonl.zst", f"records/{SLICE}/00002.jsonl.zst"], out
    assert out["refused"] == [] and out["errors"] == [] and out["stopped"] is False
    for n, data in files.items():
        assert (fd / f"records/{SLICE}/{n:05d}.jsonl.zst").read_bytes() == data, "the sharer's bytes"
    assert set(holdings.marks(fcon)) == set(out["promoted"])
    assert holdings.summary(fd, snap) == {"slices": [SLICE], "kinds": ["records"], "members": 1}
    assert not any((fetcher / fname / "state" / holdings.STAGING).rglob("*.jsonl.zst")), "staging is empty"
    assert any("promoted" in s for s in said)
    again = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                    ack_missing=False, session=leech_session(), con=fcon)
    assert again["missing"] == 0 and again["promoted"] == [], "nothing twice"
    # a slice the file does not ask for is not fetched
    narrow = transport.fetch_members(fetcher, fname, codex, slices=("year=1999",), kinds=("records",),
                                     ack_missing=False, session=leech_session(), con=fcon)
    assert narrow["wanted"] == 0 and narrow["missing"] == 0


def test_a_stop_leaves_staging_for_the_next_beat_and_nobody_serving_is_an_error_not_a_hang(node, fetcher, monkeypatch):  # noqa: F811
    quiet_sessions(monkeypatch)
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    (d / "snapshots/2026-08.sig.json").write_bytes(
        canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    got = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    fname = got["name"]
    fcon = manifest(fetcher / fname / "state" / "manifest.sqlite")
    stop = threading.Event()
    stop.set()
    out = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=leech_session(), con=fcon, stop=stop)
    assert out["stopped"] is True and out["promoted"] == [] and out["missing"] == 2
    assert holdings.marks(fcon) == {}
    # nobody serves it (the sharer never published): the metadata wait ends in an error, bounded
    monkeypatch.setattr(transport, "METADATA_TIMEOUT_S", 1.0)
    t0 = time.monotonic()
    out = transport.fetch_members(fetcher, fname, codex, slices="all", kinds=("records",),
                                  ack_missing=False, session=leech_session(), con=fcon)
    assert len(out["errors"]) == 1 and "no metadata" in out["errors"][0], "one error, then the rest waits"
    assert time.monotonic() - t0 < 10


def test_hints_come_from_the_digests_that_name_a_peer_port(node):  # noqa: F811
    now = time.time()
    cid = "sha256:" + "0" * 64
    entry = {"collection_id": cid, "lineage_id": cid, "title": "t", "snapshot_id": "2026-08",
             "snapshot_hash": "sha256:" + "1" * 64, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [], "coverage": []}
    with peers.connect() as con:
        body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                    benchmarks=[], collections=[entry], now=now)
        peers.record_digest(con, {**body, "peer_port": 6881}, now=now)
        assert transport.hints_for(con, cid, now=now) == [("10.0.0.7", 6881)]
        # a collection in no digest yet (shared after the last one, 2026-10-07):
        # every neighbour naming a swarm is asked
        assert transport.hints_for(con, "sha256:" + "f" * 64, now=now) == [("10.0.0.7", 6881)]
        other = messages.digest_body(key=OTHER_KEY, address="http://10.0.0.8:30042", neighbours=[],
                                     benchmarks=[], collections=[], now=now)
        peers.record_digest(con, {**other, "peer_port": 6999}, now=now)
        assert transport.hints_for(con, cid, now=now) == [("10.0.0.7", 6881)], "a digest that lists it comes first, alone"
        assert sorted(transport.hints_for(con, "sha256:" + "f" * 64, now=now)) == [("10.0.0.7", 6881), ("10.0.0.8", 6999)]
        peers.record_digest(con, body, now=now)                       # no port named
        peers.record_digest(con, other, now=now)
        assert transport.hints_for(con, cid, now=now) == []


def test_a_beat_on_a_complete_collection_costs_a_listing_not_a_stat_per_shard(node, monkeypatch):  # noqa: F811
    """The worker's beat over the three held corpora must not stat every
    shard every fifteen seconds: once complete at a snapshot and selection,
    a collection is skipped until the snapshot file or the selection
    changes, or an hour passes."""
    from app import collections as collections_mod, louise
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    louise._set_held_selection(collections_mod.config_toml_path(c.name), "all", ("records",))
    c = collections_mod.by_name(c.name)
    assert c.slices == "all" and transport.wants_fetching(c)
    monkeypatch.setattr(transport, "_complete", {})
    monkeypatch.setattr(transport, "_next_try", {})
    calls: list[int] = []
    real = holdings.missing

    def counted(drag_dir, members):
        calls.append(1)
        return real(drag_dir, members)
    monkeypatch.setattr(holdings, "missing", counted)
    con = db.connect(c)
    opened: list[int] = []

    def no_session():
        opened.append(1)
        raise AssertionError("nothing to fetch: no session may be opened")
    kw = dict(worker_state="running", session_factory=no_session, root=node)
    assert transport.beat([c], {c.name: con}, None, now=1000.0, **kw) == {}
    assert calls == [1], "one pass over the members"
    assert transport.beat([c], {c.name: con}, None, now=1010.0, **kw) == {}
    assert calls == [1], "complete: skipped without a stat per shard"
    assert transport.beat([c], {c.name: con}, None, now=1000.0 + transport.RECHECK_S + 1, **kw) == {}
    assert calls == [1, 1], "an hour on, looked at again"
    (d / "snapshots/2026-08.json").write_bytes(snap.canonical() + b" ")   # the snapshot file changed
    transport.beat([c], {c.name: con}, None, now=1020.0, **kw)
    assert len(calls) == 3, "a changed snapshot file is looked at"
    assert transport.beat([c], {c.name: con}, None, worker_state="paused", session_factory=no_session,
                          root=node, now=1030.0) == {}, "a mode that does not fetch: nothing"
    assert opened == []


def test_wants_fetching_needs_a_codex_file_that_asks_for_slices():
    class C:
        def __init__(self, codex, slices):
            self.codex, self.slices = codex, slices
    from tests.hold.conftest import pubmed
    assert transport.wants_fetching(C(None, "all")) is False
    assert transport.wants_fetching(C(pubmed(), None)) is False
    assert transport.wants_fetching(C(pubmed(), ())) is False
    assert transport.wants_fetching(C(pubmed(), "all")) is True
    assert transport.wants_fetching(C(pubmed(), ("year=2023",))) is True


def test_a_swarm_hint_not_yet_tried_ends_the_backoff(node, monkeypatch):  # noqa: F811
    """Live, 2026-10-07: the second peer's first attempt ran before the
    node's digest named its swarm, found nobody, and then waited fifteen
    minutes beside a swarm that answered in one second."""
    from app import collections as collections_mod, louise
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    louise._set_held_selection(collections_mod.config_toml_path(c.name), "all", ("records",))
    c = collections_mod.by_name(c.name)
    for p in (d / "records").rglob("*.jsonl.zst"):     # the members are missing here
        p.unlink()
    monkeypatch.setattr(transport, "_complete", {})
    monkeypatch.setattr(transport, "_next_try", {})
    monkeypatch.setattr(transport, "_tried_hints", {})
    calls = []

    def fake_fetch(root, name, codex_, *, hints=(), **kw):
        calls.append(list(hints))
        return {"errors": ["records: no metadata after 120s — no peer holding it answered"],
                "promoted": [], "missing": 1}
    monkeypatch.setattr(transport, "fetch_members", fake_fetch)
    monkeypatch.setattr("app.share.accept.beat", lambda *a, **k: {})
    con = db.connect(c)
    kw = dict(worker_state="running", session_factory=lambda: None, root=node)
    t = time.time()
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "t",
             "snapshot_id": snap.snapshot_id, "snapshot_hash": snap.hash, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [], "coverage": []}

    def heard_at(when):
        body = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                    benchmarks=[], collections=[entry], now=when)
        peers.record_digest(ncon, {**body, "peer_port": 6881}, now=when)
    with peers.connect() as ncon:
        transport.beat([c], {c.name: con}, ncon, now=t, **kw)
        transport.beat([c], {c.name: con}, ncon, now=t + 60, **kw)
        assert calls == [[]], "nobody answered and nothing new is known: the backoff holds"
        heard_at(t + 90)
        transport.beat([c], {c.name: con}, ncon, now=t + 120, **kw)
        assert calls == [[], [("10.0.0.7", 6881)]], "a digest naming the swarm ends the backoff at once"
        transport.beat([c], {c.name: con}, ncon, now=t + 180, **kw)
        assert len(calls) == 2, "the same swarm, nothing heard since: the backoff holds"
        heard_at(t + 200)                       # the peer restarted, and says so
        transport.beat([c], {c.name: con}, ncon, now=t + 240, **kw)
        assert len(calls) == 3, "a digest after the failed attempt is news, even from the same peer"


def test_record_shards_promoted_ask_for_a_scan_at_once(node, monkeypatch):  # noqa: F811
    """9f, live 2026-10-07: the second peer held its records with an empty
    manifest — discovery runs at start, on add and every six hours."""
    from app import collections as collections_mod, louise
    c, codex, snap, d, files = a_shared_collection(node, monkeypatch)
    louise._set_held_selection(collections_mod.config_toml_path(c.name), "all", ("records",))
    c = collections_mod.by_name(c.name)
    for p in (d / "records").rglob("*.jsonl.zst"):
        p.unlink()
    for name in ("_complete", "_next_try", "_tried_hints"):
        monkeypatch.setattr(transport, name, {})
    monkeypatch.setattr("app.share.accept.beat", lambda *a, **k: {})
    landed = [["calibration/x.safetensors"], ["records/lang=en/00001.jsonl.zst"]]
    monkeypatch.setattr(transport, "fetch_members",
                        lambda *a, **k: {"errors": [], "promoted": landed.pop(0), "missing": 0})
    asked = []
    con = db.connect(c)
    kw = dict(worker_state="running", session_factory=lambda: None, root=node, on_promoted=asked.append)
    transport.beat([c], {c.name: con}, None, now=1000.0, **kw)
    assert asked == [], "no record shard landed: nothing to scan"
    transport.beat([c], {c.name: con}, None, now=1015.0, **kw)
    assert asked == [c.name]


class _LaggingDisk:
    """A session whose disk thread is behind `file_progress`, as
    libtorrent's is: the file stands at its full length with its last block
    still zeros when the progress already says whole, and the block lands
    only on a later poll — the interleaving that refused the survivor
    test's calibration member (2026-10-08)."""

    def __init__(self, torrent, member, staging, data):
        from app.share import members as mem
        self.info = lt.torrent_info(lt.bdecode(mem.bencode({"info": torrent.info})))
        self.landed = staging / member / torrent.path
        self.data, self.polls_left = data, 2
        self.landed.parent.mkdir(parents=True, exist_ok=True)
        self.landed.write_bytes(data[:-1] + b"\0")      # sized, one byte not yet written

    def poll(self):
        self.polls_left -= 1
        if self.polls_left == 0:
            self.landed.write_bytes(self.data)          # the disk thread catches up

    # the session
    def add_torrent(self, params):
        return self

    def remove_torrent(self, handle):
        pass

    # the handle
    def status(self):
        return type("S", (), {"has_metadata": True, "total_download": len(self.data), "errc": None})()

    def torrent_file(self):
        return self.info

    def file_progress(self):
        return [len(self.data)]

    def connect_peer(self, h):
        pass

    def prioritize_files(self, p):
        pass

    def resume(self):
        pass

    def flush_cache(self):
        pass

    def set_download_limit(self, n):
        pass


def test_a_file_received_whole_is_returned_only_once_the_disk_holds_what_was_verified(tmp_path, monkeypatch):
    from app.hold import drag as drag_mod
    from app.share import members as mem
    data = bytes(range(256)) * 300
    root = drag_mod.pieces_root(data)
    member = mem.Member("calibration-x", (drag_mod.MemberFile("calibration/x.safetensors", root, len(data)),))
    t = member.torrents()[0]
    disk = _LaggingDisk(t, member.name, tmp_path / "staging", data)
    real_sleep = time.sleep
    monkeypatch.setattr(transport.time, "sleep", lambda s: (disk.poll(), real_sleep(0)))
    landed = transport.fetch_file(disk, torrent=t, member=member.name, staging=tmp_path / "staging",
                                  poll_s=0.0)
    assert landed == disk.landed
    assert drag_mod.pieces_root_of(landed) == root, "returned while the last block was still due"
    # and one that never comes right is an error, bounded — never a refusal of the bytes the swarm served
    disk = _LaggingDisk(t, member.name, tmp_path / "staging2", data)
    disk.polls_left = -1
    monkeypatch.setattr(transport, "LANDED_TIMEOUT_S", 0.05)
    with pytest.raises(transport.TransportError, match="not on disk as received"):
        transport.fetch_file(disk, torrent=t, member=member.name, staging=tmp_path / "staging2", poll_s=0.0)


def test_landed_whole_is_the_root_not_the_length(tmp_path):
    from app.hold import drag as drag_mod
    data = b"x" * 40000
    p = tmp_path / "f"
    p.write_bytes(b"\0" * len(data))
    assert not transport.landed_whole(p, len(data), drag_mod.pieces_root(data)), "full length, holes inside"
    p.write_bytes(data)
    assert transport.landed_whole(p, len(data), drag_mod.pieces_root(data))
    assert not transport.landed_whole(tmp_path / "absent", len(data), drag_mod.pieces_root(data))
