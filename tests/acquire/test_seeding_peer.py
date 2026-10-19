"""The peer plane, both directions: a node SEEDS what it published and
accounts every uploaded byte (invariant 5), and a collection can FETCH its
corpus from a peer's torrent instead of the origin (`peer_source`).

Observed before this existed: the Network page's "seeding / up" column
could only ever read 0 — nothing reported upload bytes into the UP bucket
— and there was no way to point a collection at a peer at all (STATUS 5c).
"""
import json
import time
from pathlib import Path

import pytest

from app import collections as collections_mod, db
from app.acquire import engine, seeding
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, Endpoint, Filter,
                               Transport)
from app.transfer.bucket import Priority
from app.transfer.manager import Direction, get_manager

lt = pytest.importorskip("libtorrent")


def _collection(tmp_path, **over):
    d = dict(name="peer", plugin="gutenberg", profile=None, embed_mode_default=None,
             corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "manifest.db",
             fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
             acquire_enabled=True, license_ack=None)
    d.update(over)
    d["corpus_dir"].mkdir(parents=True, exist_ok=True)
    return collections_mod.Collection(**d)


# ---------------- effective_spec: the peer redirect ----------------

def test_peer_source_redirects_the_spec_to_the_torrent_transport(tmp_path):
    spec = AcquisitionSpec(
        source_id="gutenberg", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="https://origin"),
                   Endpoint(transport=Transport.HTTPS, url="https://mirror")),
        layout="one-dir-per-doc", probe_file="1/pg1.rdf",
        checksums=ChecksumSource(kind="sidecar"),
        profiles={"demo": Filter(max_files=3), "full": Filter()})
    plain = _collection(tmp_path)
    assert engine.effective_spec(plain, spec) is spec, "no peer_source: untouched"

    peer = _collection(tmp_path, peer_source="magnet:?xt=urn:btih:abc&x.pe=10.0.0.2:6881")
    eff = engine.effective_spec(peer, spec)
    assert eff.transport is Transport.TORRENT
    assert [e.url for e in eff.endpoints] == ["magnet:?xt=urn:btih:abc&x.pe=10.0.0.2:6881"]
    assert eff.probe_file is None, "mirrors/probing are origin concepts"
    assert eff.checksums.kind == "none", "torrent piece hashes replace sidecars"
    # everything that defines WHAT is fetched survives
    assert eff.profiles == spec.profiles and eff.layout == spec.layout
    assert eff.source_id == spec.source_id


# ---------------- seeding + upload accounting ----------------

def _quiet_session():
    return lt.session({"listen_interfaces": "127.0.0.1:0", "enable_dht": False,
                       "enable_lsd": False, "enable_upnp": False,
                       "enable_natpmp": False, "alert_mask": 0})


def test_seeder_serves_published_torrents_and_credits_the_up_bucket(
        tmp_path, isolated_config, monkeypatch):
    """publish -> the worker's seeder serves it -> a peer downloads -> the
    uploaded bytes land in the UP bucket under SEEDING and the seeding
    table shows them. The one test that makes 'seeding / up' non-fiction."""
    from app.acquire import torrent_publish, transport_torrent as tt
    from app.transfer import manager as tm

    # a tiny corpus, published the way `torrent publish --subdir data` does
    c = _collection(tmp_path)
    root = c.corpus_dir / "data"
    (root / "tr").mkdir(parents=True)
    (root / "tr" / "000.parquet").write_bytes(b"x" * 300_000)
    (root / "tr" / "001.parquet").write_bytes(b"y" * 200_000)
    out = tmp_path / "peer.torrent"
    published = torrent_publish.publish_corpus(root, out, piece_size=16 * 1024)
    con = db.connect(c)
    seeding.record_published(con, {**published.as_catalog_entry(),
                                   "torrent_path": str(out), "root": str(root)})

    # isolate the process-global session and the transfer manager
    monkeypatch.setattr(tt, "_session", None)
    monkeypatch.setattr(tt, "_session_lock", __import__("threading").Lock())
    monkeypatch.setattr(tt, "session_settings", lambda: {
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False, "alert_mask": 0})
    monkeypatch.setattr(seeding, "_seeder", None)
    monkeypatch.setattr(tm, "_manager", None, raising=False)
    manager = get_manager()
    before = manager.bucket(Direction.UP).core.granted[Priority.SEEDING]

    control = db.connect(c)
    seeding.tick([c], {c.name: con}, control, "running")
    seeder = seeding.get_seeder()
    assert len(seeder.table()) == 1, "the published torrent is being served"
    seed_port = tt.get_session().listen_port()

    # a peer downloads the whole thing straight from us: no tracker, no DHT
    leech = _quiet_session()
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(out))
    params.save_path = str(tmp_path / "leech")
    h = leech.add_torrent(params)
    h.connect_peer(("127.0.0.1", seed_port))
    deadline = time.time() + 60
    while not h.status().is_seeding and time.time() < deadline:
        seeding.tick([c], {c.name: con}, control, "running")   # accounts as it goes
        time.sleep(0.5)
    assert h.status().is_seeding, "the peer must complete from our seed"
    seeding.tick([c], {c.name: con}, control, "running")

    after = manager.bucket(Direction.UP).core.granted[Priority.SEEDING]
    assert after - before >= 500_000, \
        f"uploaded bytes must be credited to the UP bucket under SEEDING ({after - before})"
    rows, age = seeding.read_table(control)
    assert rows and rows[0]["uploaded"] >= 500_000 and rows[0]["collection"] == "peer"
    assert age is not None and age < 10

    # paused: the seeder stands down; seed-only / acquire-only keep serving
    seeding.tick([c], {c.name: con}, control, "paused")
    assert seeding.read_table(control)[0][0]["paused"] is True
    seeding.tick([c], {c.name: con}, control, "seed-only")
    assert seeding.read_table(control)[0][0]["paused"] is False

    # deleting the .torrent is how seeding is stopped: a fresh seeder
    # refuses to serve it
    out.unlink()
    assert seeding.Seeder().ensure(c, seeding.read_published(con)[0]) is False
    leech.remove_torrent(h)


def test_network_page_has_a_real_seeding_table(isolated_config):
    """The seeding panel is no longer a placeholder: it renders the rows the
    worker's seeder publishes (the SSE payload's `seeding`)."""
    from app import web
    html = web.TEMPLATES.env.get_template("network.html").render(request=None, active="network")
    assert "seed-table" in html and "seed-scope" in html
    assert "d.seeding" in html, "the page consumes the seeding rows from the stream"


def test_a_shard_member_is_seeded_off_the_dht_and_a_published_corpus_on_it(
        tmp_path, isolated_config, monkeypatch):
    """libtorrent announces every torrent it holds by its own infohash, and a
    not-auto-managed seed is outside `active_dht_limit`: 3,170 gutenberg
    members kept ~10k UDP flows open and filled the router's NAT table
    (docs/notes/dht-flood.md). A SHARD member is found under its snapshot's
    keys (04 §4.2), so it is seeded with the DHT and local discovery off; a
    published corpus, which strangers fetch by magnet, keeps both."""
    from app.acquire import torrent_publish, transport_torrent as tt

    c = _collection(tmp_path)
    monkeypatch.setattr(tt, "_session", None)
    monkeypatch.setattr(tt, "_session_lock", __import__("threading").Lock())
    monkeypatch.setattr(tt, "session_settings", lambda: {
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False, "alert_mask": 0})
    entries = []
    for name in ("corpus", "member"):
        root = tmp_path / name / "data"
        root.mkdir(parents=True)
        (root / "000.parquet").write_bytes(name.encode() * 40_000)
        out = tmp_path / f"{name}.torrent"
        published = torrent_publish.publish_corpus(root, out, piece_size=16 * 1024)
        entry = {**published.as_catalog_entry(), "torrent_path": str(out), "root": str(root)}
        entries.append({**entry, "member": "records", "path": "000.parquet"} if name == "member" else entry)
    seeder = seeding.Seeder()
    quiet = int(lt.torrent_flags.disable_dht) | int(lt.torrent_flags.disable_lsd)
    flags = {}
    for e in entries:
        assert seeder.ensure(c, e)
        flags["member" in e] = int(seeder._handles[e.get("info_hash") or e["torrent_path"]].flags())
    assert flags[True] & quiet == quiet, "a SHARD member is never announced by its own infohash"
    assert flags[False] & quiet == 0, "a published corpus stays findable by magnet"
