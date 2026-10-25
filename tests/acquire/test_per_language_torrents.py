"""Per-language torrents for a pre-embedded corpus (STATUS 5h, amendment
37's "active while fetching"): one torrent per data/<lang>, all rooted at
`data`, several recorded per collection, several listed per peer and
fetched one after the other."""
from dataclasses import replace
from pathlib import Path

import pytest

from app import collections as collections_mod, db
from app.acquire import engine, seeding, torrent_publish
from app.acquire.spec import (AcquisitionSpec, ChecksumSource, DeltaSpec, Endpoint, Filter,
                               RemoteFile, Transport)
from app.acquire.transport_torrent import TorrentTransport
from app.plugins.base import LicenseClass

pytest.importorskip("libtorrent")


def _corpus(tmp_path) -> Path:
    corpus = tmp_path / "wikivec"
    for lang, n in (("en", 3), ("tr", 2)):
        d = corpus / "data" / lang
        d.mkdir(parents=True)
        for i in range(n):
            (d / f"{i:03d}.parquet").write_bytes(bytes([i]) * (2048 + i))
    (corpus / "data" / ".pack").mkdir()
    (corpus / "data" / ".pack" / "junk").write_bytes(b"x")
    return corpus


def test_per_language_torrents_share_the_root_and_carry_one_language_each(tmp_path):
    corpus = _corpus(tmp_path)
    data = corpus / "data"
    out = {}
    for lang in ("en", "tr"):
        out[lang] = torrent_publish.publish_corpus(
            data, tmp_path / f"wikivec-{lang}.torrent", name=f"wikivec-{lang}",
            include_patterns=(f"*data/{lang}/*",))
    assert out["en"].files == 3 and out["tr"].files == 2
    assert out["en"].info_hash != out["tr"].info_hash
    for lang in ("en", "tr"):
        ep = Endpoint(transport=Transport.TORRENT, url=str(out[lang].torrent_path))
        listed = sorted(f.rel_path for f in TorrentTransport().list_files(ep, None))
        # torrent-relative paths start at the root `data`, exactly the
        # plugin's layout, and hold ONLY this language (never the hidden cache)
        assert listed == sorted(f"data/{lang}/{i:03d}.parquet" for i in range(3 if lang == "en" else 2))
        assert not any(".pack" in p for p in listed)


def test_the_seeding_record_holds_several_torrents_per_collection(isolated_config, tmp_path):
    c = collections_mod.default_collection()
    con = db.connect(c)
    a = {"name": "wikivec-en", "info_hash": "aaa", "torrent_path": str(tmp_path / "a.torrent"),
         "root": str(tmp_path), "files": 3, "bytes": 30, "magnet": "magnet:?xt=urn:btih:aaa"}
    b = {**a, "name": "wikivec-tr", "info_hash": "bbb", "torrent_path": str(tmp_path / "b.torrent")}
    seeding.record_published(con, a)
    seeding.record_published(con, b)
    assert [e["name"] for e in seeding.read_published(con)] == ["wikivec-en", "wikivec-tr"]
    # re-publishing the same torrent replaces its entry, in place of a duplicate
    seeding.record_published(con, {**a, "files": 4})
    entries = seeding.read_published(con)
    assert [e["name"] for e in entries] == ["wikivec-tr", "wikivec-en"]
    assert next(e for e in entries if e["name"] == "wikivec-en")["files"] == 4
    # an older single-dict record still reads as one entry
    db.set_control(con, seeding.TORRENT_PUBLISHED_KEY, __import__("json").dumps(a))
    assert len(seeding.read_published(con)) == 1


class _Peer:
    def __init__(self, peer_source):
        self.peer_source = peer_source


def test_peer_sources_accept_one_or_several_in_order():
    assert engine.peer_sources(_Peer(None)) == ()
    assert engine.peer_sources(_Peer("magnet:?xt=a")) == ("magnet:?xt=a",)
    assert engine.peer_sources(_Peer("magnet:?xt=a\nmagnet:?xt=b\n")) == ("magnet:?xt=a", "magnet:?xt=b")
    assert engine.peer_sources(_Peer("magnet:?xt=a, /x/b.torrent")) == ("magnet:?xt=a", "/x/b.torrent")
    assert engine.peer_sources(_Peer(["magnet:?xt=a", "magnet:?xt=b"])) == ("magnet:?xt=a", "magnet:?xt=b")


def test_several_peer_sources_are_fetched_as_torrents_in_order(isolated_config, tmp_path):
    """No collection file carries `peer_source` since the pre-Codex shape
    left (2026-10-08); the collection is built here with one."""
    loaded = replace(collections_mod.default_collection(), acquire_enabled=True,
                     peer_source="magnet:?xt=urn:btih:aaa\nmagnet:?xt=urn:btih:bbb")
    assert engine.peer_sources(loaded) == ("magnet:?xt=urn:btih:aaa", "magnet:?xt=urn:btih:bbb")

    spec = AcquisitionSpec(
        source_id="embedded", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="http://origin"),),
        layout="container-files", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"tr": Filter()},
        estimated_bytes={"tr": 1}, license_class=LicenseClass.GREEN)
    eff = engine.effective_spec(loaded, spec)
    assert eff.transport is Transport.TORRENT
    assert [e.url for e in eff.endpoints] == ["magnet:?xt=urn:btih:aaa", "magnet:?xt=urn:btih:bbb"]


def test_several_torrents_are_fetched_one_after_the_other_in_listed_order(isolated_config, tmp_path, monkeypatch):
    """Amendment 37's principle: the first language is complete — and the
    pipeline busy on it — before the next starts."""
    c = replace(collections_mod.default_collection(), acquire_enabled=True,
                peer_source="magnet:?xt=urn:btih:aaa\nmagnet:?xt=urn:btih:bbb")
    con = db.connect(c)
    spec = AcquisitionSpec(
        source_id="gutenberg", transport=Transport.HTTPS,
        endpoints=(Endpoint(transport=Transport.HTTPS, url="http://origin"),),
        layout="one-dir-per-doc", checksums=ChecksumSource(kind="none"),
        delta=DeltaSpec(mode="full-resync"), profiles={"demo": Filter()},
        estimated_bytes={"demo": 1}, license_class=LicenseClass.GREEN)
    seen = []

    def fake_claimed(con_, coll, spec_, profile, stop, priority, until=None):
        seen.append([e.url for e in spec_.endpoints])
        return {"fetched": 1, "skipped": 0, "failed": 0, "aborted": 0, "adopted": 0, "total": 1}
    monkeypatch.setattr(engine, "_run_acquisition_claimed", fake_claimed)
    result = engine.run_acquisition(con, c, spec, "demo")
    assert seen == [["magnet:?xt=urn:btih:aaa"], ["magnet:?xt=urn:btih:bbb"]], seen
    assert result["fetched"] == 2 and result["total"] == 2
