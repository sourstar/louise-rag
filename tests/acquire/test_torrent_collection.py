"""A TORRENT COLLECTION (STATUS 5l): one magnet for a whole published
corpus. The publisher's index torrent lists per-slice members; a peer's
collection is created with that one magnet, its profile picks members,
growing the profile adds members without a TOML edit, and the Network
page groups the members under their collection with its magnet."""
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app import collections as collections_mod, db
from app.acquire import engine, seeding, torrent_collection, torrent_publish
from app.acquire import transport_torrent as tt
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                               Transport)
from app.plugins.base import LicenseClass

lt = pytest.importorskip("libtorrent")


@pytest.fixture(autouse=True)
def _own_session(monkeypatch):
    tt.shutdown_session()
    monkeypatch.setenv("TORRENT_LISTEN", "127.0.0.1:0")
    monkeypatch.setenv("TORRENT_DHT", "0")
    monkeypatch.setenv("TORRENT_LSD", "0")
    yield
    tt.shutdown_session()


def _spec():
    return AcquisitionSpec(
        source_id="embedded", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="http://origin"),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"),
        profiles={"tr": Filter(include=("data/tr/*",)), "it": Filter(include=("data/it/*",)),
                  "en": Filter(include=("data/en/*",)), "all": Filter()},
        profile_mode="additive",
        estimated_bytes={"tr": 1, "it": 2, "en": 3, "all": 6}, license_class=LicenseClass.GREEN)


def _publish_members(tmp_path):
    corpus = tmp_path / "pub" / "wikivec"
    sizes = {"tr": 1, "it": 3, "en": 5}
    for lang, n in sizes.items():
        d = corpus / "data" / lang
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"{i:03d}.parquet").write_bytes(bytes([i + 1]) * 4096)
    members = []
    for lang in sizes:
        p = torrent_publish.publish_corpus(corpus / "data", tmp_path / f"wikivec-{lang}.torrent",
                                           name=f"wikivec-{lang}", include_patterns=(f"*data/{lang}/*",))
        members.append({**p.as_catalog_entry(), "torrent_path": str(p.torrent_path),
                        "root": str(corpus / "data"), "kind": "member", "slice": lang,
                        "include": [f"data/{lang}/*"]})
    return corpus, members


def test_index_roundtrip_and_members_by_profile(tmp_path):
    corpus, members = _publish_members(tmp_path)
    index_dir = tmp_path / "pub" / "collection"
    torrent_collection.write_index(index_dir, collection="wikivec", plugin="embedded",
                                   members=members, peer_hint="127.0.0.1:6881")
    index = json.loads((index_dir / torrent_collection.INDEX_FILE).read_text())
    assert index["kind"] == torrent_collection.KIND and len(index["members"]) == 3
    assert {m["slice"] for m in index["members"]} == {"tr", "it", "en"}
    assert sorted(p.name for p in index_dir.glob("*.torrent")) == \
        ["wikivec-en.torrent", "wikivec-it.torrent", "wikivec-tr.torrent"], "member metadata rides along"

    spec = _spec()
    pick = lambda prof: [m["slice"] for m in torrent_collection.members_for_profile(index, spec, prof)]
    assert pick("tr") == ["tr"]
    assert pick("it,tr") == ["it", "tr"], "a combined profile's order is the fetch order"
    assert pick("all") == ["tr", "it", "en"], "everything: smallest first, busy soon"
    assert pick("nope") == []
    assert [s["name"] for s in torrent_collection.offered_slices(index)] == ["tr", "it", "en"]
    assert torrent_collection.with_hint("magnet:?xt=a", "h:1") == "magnet:?xt=a&x.pe=h:1"
    assert torrent_collection.with_hint("/x.torrent", "h:1") == "/x.torrent"


def test_a_peer_resolves_the_collection_and_its_profile_picks_the_members(isolated_config, tmp_path):
    """End to end over a local swarm: the publisher seeds the index; the
    peer's collection holds ONE magnet; effective_spec expands it to the
    members its profile selects; growing the profile adds a member with
    no TOML change; the grow dialog offers what the publisher offers."""
    corpus, members = _publish_members(tmp_path)
    index_dir = tmp_path / "pub" / "collection"
    torrent_collection.write_index(index_dir, collection="wikivec", plugin="embedded",
                                   members=members, peer_hint="")
    index_pub = torrent_publish.publish_corpus(index_dir, tmp_path / "wikivec.collection.torrent",
                                               name="wikivec-collection")
    # a seeder for the index (the publisher's worker does this live)
    seeder = lt.session({"listen_interfaces": "127.0.0.1:0", "alert_mask": 0, "enable_dht": False,
                         "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False})
    params = lt.add_torrent_params()
    params.ti = lt.torrent_info(str(index_pub.torrent_path))
    params.save_path = str(index_dir.parent)
    handle = seeder.add_torrent(params)
    deadline = time.time() + 30
    while not handle.status().is_seeding and time.time() < deadline:
        time.sleep(0.1)
    assert handle.status().is_seeding
    port = seeder.listen_port()

    peer = replace(collections_mod.default_collection(), name="wv", plugin="embedded",
                   profile="tr", acquire_enabled=True,
                   manifest_path=tmp_path / "peer" / "manifest.db",
                   corpus_dir=tmp_path / "peer" / "corpus",
                   peer_source=f"{index_pub.magnet}&x.pe=127.0.0.1:{port}")
    peer.corpus_dir.mkdir(parents=True)
    db.connect(peer)
    try:
        source = engine.peer_sources(peer)[0]
        index = torrent_collection.resolve(peer, source)
        assert index is not None and len(index["members"]) == 3
        assert (peer.manifest_path.parent / "peer-index").is_dir(), "index content lands beside the manifest, not in the corpus"
        assert torrent_collection.cached_index(peer, source)["collection"] == "wikivec"

        spec = _spec()
        eff = engine.effective_spec(peer, spec, "tr")
        assert eff.transport is Transport.TORRENT
        assert [e.url for e in eff.endpoints] == \
            [m["magnet"] for m in members if m["slice"] == "tr"], "no hint: the index had none"
        grown = engine.effective_spec(peer, spec, "tr,it")
        assert len(grown.endpoints) == 2, "growing the profile adds a member — no TOML edit"
        everything = engine.effective_spec(peer, spec, "all")
        assert len(everything.endpoints) == 3

        from app import web
        offered = web._offered_by_torrent_collection(peer)
        assert [o["name"] for o in offered] == ["tr", "it", "en"]
        assert web._grow_options(peer) == offered
        # a plain-torrent peer keeps the origin's grow options
        plain = replace(peer, peer_source=str(members[0]["torrent_path"]))
        assert web._offered_by_torrent_collection(plain) is None
    finally:
        seeder.remove_torrent(handle)
        tt.shutdown_session()


def test_seeding_rows_carry_kind_slice_and_collection_magnet(isolated_config, tmp_path):
    c = collections_mod.default_collection()
    con = db.connect(c)
    seeding.record_published(con, {"name": "wikivec-tr", "info_hash": "aaa", "kind": "member",
                                   "slice": "tr", "magnet": "magnet:?xt=urn:btih:aaa",
                                   "torrent_path": str(tmp_path / "a.torrent"), "root": str(tmp_path),
                                   "files": 1, "bytes": 10})
    seeding.record_published(con, {"name": "wikivec-collection", "info_hash": "ccc", "kind": "collection",
                                   "magnet": "magnet:?xt=urn:btih:ccc", "peer_hint": "h:1",
                                   "members": ["aaa"], "torrent_path": str(tmp_path / "c.torrent"),
                                   "root": str(tmp_path), "files": 2, "bytes": 1})
    entries = seeding.read_published(con)
    assert [(e["kind"], e.get("slice")) for e in entries] == [("member", "tr"), ("collection", None)]
    s = seeding.Seeder()

    class _Status:
        total_upload = 5; num_peers = 1; is_seeding = True; state = 5
    class _Handle:
        def status(self): return _Status()
    for e in entries:
        s._handles[e["info_hash"]] = _Handle()
        s._meta[e["info_hash"]] = {**e, "collection": c.name}
    rows = {r["info_hash"]: r for r in s.table()}
    assert rows["aaa"]["kind"] == "member" and rows["aaa"]["slice"] == "tr"
    assert rows["ccc"]["kind"] == "collection" and rows["ccc"]["members"] == ["aaa"]
    assert rows["ccc"]["magnet"] == "magnet:?xt=urn:btih:ccc" and rows["ccc"]["peer_hint"] == "h:1"


def test_collection_only_rebuilds_the_index_from_recorded_members_without_hashing(
        isolated_config, tmp_path, monkeypatch, capsys):
    """The NAS case: eleven members already hashed and recorded; the index
    is (re)built from the records alone and recorded as kind=collection."""
    from app import cli
    corpus, members = _publish_members(tmp_path)
    c = replace(collections_mod.default_collection(), corpus_dir=corpus,
                manifest_path=tmp_path / "m" / "manifest.db")
    c.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    con = db.connect(c)
    for m in members:
        seeding.record_published(con, {**m, "peer_hint": "127.0.0.1:6881"})
    monkeypatch.setattr(cli, "_collection_by_name", lambda name: c)
    hashed = []
    real = torrent_publish.publish_corpus

    def counting(root, out, **kw):
        hashed.append(Path(out).name)
        return real(root, out, **kw)
    monkeypatch.setattr(torrent_publish, "publish_corpus", counting)
    assert cli.main(["torrent", "publish", c.name, "--collection-only"]) == 0
    assert hashed == [f"{c.name}.collection.torrent"], "only the index was hashed"
    out = capsys.readouterr().out
    assert "TORRENT COLLECTION" in out and "x.pe=127.0.0.1:6881" in out, "the recorded hint rides the magnet"
    entries = seeding.read_published(con)
    index_entry = next(e for e in entries if e.get("kind") == "collection")
    assert sorted(index_entry["members"]) == sorted(m["info_hash"] for m in members)
    index = json.loads((c.manifest_path.parent / "collection" / torrent_collection.INDEX_FILE).read_text())
    assert [m["slice"] for m in index["members"]] == ["tr", "it", "en"] and index["peer_hint"] == "127.0.0.1:6881"
