"""Per-slice torrents over a pack (pack-format.md §10): member = slice,
every member rooted at the pack so a peer's files land in the pack
layout; the manifest member is tiny and separate."""
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery
from app.acquire import torrent_publish
from app.acquire.spec import Endpoint, Transport
from app.acquire.transport_torrent import TorrentTransport
from app.pack import build as pack_build, manifest as manifest_mod

pytest.importorskip("libtorrent")


def _pack(tmp_path, corpus_dir) -> Path:
    d = tmp_path / "g"
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus_dir, manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    pack_dir = tmp_path / "packs" / "gutenberg"
    pack_build.build(c, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    return pack_dir


def test_per_slice_members_are_rooted_at_the_pack(isolated_config, tmp_path, corpus_dir):
    pack_dir = _pack(tmp_path, corpus_dir)
    m = manifest_mod.load(pack_dir)
    assert sorted(m["slices"]) == ["lang=en", "lang=fr"]
    published = {}
    for slice_name in m["slices"]:
        safe = slice_name.replace("/", "+")
        published[slice_name] = torrent_publish.publish_corpus(
            pack_dir, tmp_path / f"gutenberg-{safe}.torrent", name=f"gutenberg-{safe}",
            include_patterns=(f"*records/{slice_name}/*",))
    meta = torrent_publish.publish_corpus(
        pack_dir, tmp_path / "gutenberg-manifest.torrent", name="gutenberg-manifest",
        include_patterns=("manifest.json", "validation.json", "TERMS.txt"))
    assert published["lang=en"].files == 2 and published["lang=fr"].files == 1
    assert meta.files == 2, "manifest + validation plan (green: no terms file)"
    for slice_name, pub in published.items():
        ep = Endpoint(transport=Transport.TORRENT, url=str(pub.torrent_path))
        listed = sorted(f.rel_path for f in TorrentTransport().list_files(ep, None))
        assert all(p.startswith(f"gutenberg/records/{slice_name}/") for p in listed), listed
        assert not any(".build" in p for p in listed), "the build state is hidden, never published"
    ep = Endpoint(transport=Transport.TORRENT, url=str(meta.torrent_path))
    assert sorted(f.rel_path for f in TorrentTransport().list_files(ep, None)) == [
        "gutenberg/manifest.json", "gutenberg/validation.json"]
