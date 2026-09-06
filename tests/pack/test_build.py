"""`pack build` and the `packed` source: a collection's documents become
sliced record shards + a manifest; a collection over the pack yields the
same uids, the same chunks and the same possession hashes as the origin
— resumably, for gutenberg, pubmed and a synthesized wikipedia dump."""
import bz2
import json
import threading
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery, layers, plugin_io
from app.pack import build as pack_build, manifest as manifest_mod, records, slicing
from app.plugins.base import DiscoverState
from app.plugins.packed import for_pack

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _collection(tmp_path, name, plugin, corpus_dir, **kw):
    from dataclasses import replace
    from app import licensing
    d = tmp_path / name
    c = collections_mod.Collection(
        name=name, plugin=plugin, profile=None, embed_mode_default=None,
        corpus_dir=Path(corpus_dir), manifest_path=d / "manifest.db",
        fts_dir=d / "fts", lance_dir=d / "lancedb", **kw)
    # a yellow-class origin (pubmed, wikipedia, a yellow pack) needs the
    # acknowledgement before its data is packed or scanned (invariant 9)
    if "license_ack" not in kw and licensing.state_for(c) == licensing.MISSING:
        c = replace(c, license_ack=licensing.make_ack(c))
    return c


def _discovered(tmp_path, name, plugin, corpus_dir, **kw):
    c = _collection(tmp_path, name, plugin, corpus_dir, **kw)
    con = db.connect(c)
    discovery.sync_collection(con, c)
    con.close()
    return c


def _chunks_of(collection):
    con = db.connect(collection)
    plugin = collection.plugin_instance()
    out = {}
    for row in con.execute("SELECT uid, native_id, path, lang FROM documents WHERE error IS NULL ORDER BY uid"):
        sections = plugin_io.sections(plugin, row)
        out[row["uid"]] = [(c["id"], c["text"]) for c in layers.chunks_for_document(collection, row, sections)]
    con.close()
    return out


def _raw_hashes(collection):
    con = db.connect(collection)
    plugin = collection.plugin_instance()
    out = {row["uid"]: plugin_io.canonical_doc(plugin, row).raw_hash
           for row in con.execute("SELECT uid, native_id, path, lang FROM documents WHERE error IS NULL")}
    con.close()
    return out


def test_gutenberg_pack_round_trips_uids_chunks_and_possession(isolated_config, tmp_path, corpus_dir):
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / "gutenberg"
    result = pack_build.build(origin, pack_dir, snapshot="2026-08-26", records_per_shard=2, sign=False,
                              log=lambda *_: None)
    # two English books, one French: two slices, one shard each at 2 records/shard
    assert result.records == 3 and result.shards == 2 and result.slices == 2 and not result.interrupted
    m = manifest_mod.load(pack_dir)
    assert m["plugin"] == "gutenberg" and m["license"]["class"] == "green"
    assert m["layers"] == [{"chunker": {"id": "book", "version": 1}, "rule": "default"}]
    assert list(m["slices"]) == ["lang=en", "lang=fr"] and m["slices"]["lang=en"]["records"] == 2
    assert [s["path"] for s in m["shards"]] == ["records/lang=en/00000.jsonl.zst",
                                                "records/lang=fr/00000.jsonl.zst"]
    for s in m["shards"]:
        assert s["sha256"] == records.sha256_of(pack_dir / s["path"])
    assert (pack_dir / "validation.json").is_file()
    # the pack served as a corpus
    packed = _discovered(tmp_path, "gutenberg-pack", "packed", pack_dir)
    assert packed.plugin_instance().source_id == "gutenberg"
    con = db.connect(packed)
    uids = sorted(r["uid"] for r in con.execute("SELECT uid FROM documents"))
    con.close()
    assert uids == ["gutenberg:10001", "gutenberg:10002", "gutenberg:10003"]
    assert _chunks_of(packed) == _chunks_of(origin), "identical chunk ids and text"
    assert _raw_hashes(packed) == _raw_hashes(origin), "possession hashes agree with the origin"
    # a packed record is readable as JSON, keys in the fixed order
    rec = records.read_record(pack_dir / m["shards"][0]["path"], "gutenberg:10001")
    assert list(rec) == list(records.RECORD_KEYS) and rec["chunks"] >= 1 and rec["layer"] == 0


def test_pubmed_pack_slices_by_year_and_keeps_the_abstract_chunker(isolated_config, tmp_path):
    origin = _discovered(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    pack_dir = tmp_path / "packs" / "pubmed"
    result = pack_build.build(origin, pack_dir, snapshot="2026-08-26", sign=False, log=lambda *_: None)
    m = manifest_mod.load(pack_dir)
    assert m["plugin"] == "pubmed" and m["license"]["class"] == "yellow"
    assert m["license"]["terms_sha256"] and (pack_dir / "TERMS.txt").is_file()
    assert all(s.startswith("year=") for s in m["slices"]), m["slices"]
    assert m["layers"][0]["chunker"]["id"] == "abstract"
    assert result.records == sum(s["records"] for s in m["slices"].values()) > 0
    packed = _discovered(tmp_path, "pubmed-pack", "packed", pack_dir)
    assert packed.plugin_instance().chunking_profile == "abstract"
    assert _chunks_of(packed) == _chunks_of(origin)
    # tombstoned (DeleteCitation) documents never enter a pack
    con = db.connect(origin)
    deleted = {r["uid"] for r in con.execute("SELECT uid FROM documents WHERE error IS NOT NULL")}
    con.close()
    con = db.connect(packed)
    assert not deleted & {r["uid"] for r in con.execute("SELECT uid FROM documents")}
    con.close()


def _page(pid: int, title: str, text: str) -> str:
    return (f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n    <id>{pid}</id>\n"
            f"    <revision>\n      <id>{pid}00</id>\n      <text>{text}</text>\n    </revision>\n  </page>\n")


@pytest.fixture
def wiki_corpus(tmp_path) -> Path:
    d = tmp_path / "wiki-corpus" / "simplewiki" / "latest"
    d.mkdir(parents=True)
    s1 = bz2.compress((_page(1, "The Moon", "'''The Moon''' is a satellite.\n\n== Orbit ==\nIt orbits.")
                       + _page(2, "Cheese", "'''Cheese''' is food.")).encode())
    s2 = bz2.compress(_page(1_500_000, "Far page", "A page with a high id.").encode())
    (d / "simplewiki-latest-pages-articles-multistream.xml.bz2").write_bytes(s1 + s2)
    index = [f"0:1:The Moon", f"0:2:Cheese", f"{len(s1)}:1500000:Far page"]
    (d / "simplewiki-latest-pages-articles-multistream-index.txt.bz2").write_bytes(
        bz2.compress(("\n".join(index) + "\n").encode()))
    return tmp_path / "wiki-corpus"


def test_wikipedia_pack_slices_by_language_and_page_range(isolated_config, tmp_path, wiki_corpus):
    origin = _discovered(tmp_path, "wikipedia", "wikipedia", wiki_corpus)
    pack_dir = tmp_path / "packs" / "wikipedia"
    pack_build.build(origin, pack_dir, snapshot="2026-08-26", sign=False, log=lambda *_: None)
    m = manifest_mod.load(pack_dir)
    assert set(m["slices"]) == {"lang=en/pages=0M", "lang=en/pages=1M"}
    assert m["layers"][0]["chunker"]["id"] == "cdc"
    packed = _discovered(tmp_path, "wikipedia-pack", "packed", pack_dir)
    assert _chunks_of(packed) == _chunks_of(origin)
    assert (pack_dir / "records" / "lang=en" / "pages=1M" / "00000.jsonl.zst").is_file()


def test_build_is_resumable_and_idempotent(isolated_config, tmp_path, corpus_dir, monkeypatch):
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / "g"
    stop = threading.Event()
    written = []
    real = records.write_shard

    def stop_after_first(path, recs, **kw):
        info = real(path, recs, **kw)
        written.append(path.name)
        stop.set()                      # a SIGTERM between shards
        return info
    monkeypatch.setattr(records, "write_shard", stop_after_first)
    r1 = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, stop=stop, log=lambda *_: None)
    assert r1.interrupted and written == ["00000.jsonl.zst"]
    assert not (pack_dir / manifest_mod.FILENAME).exists()
    monkeypatch.setattr(records, "write_shard", real)
    r2 = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    assert not r2.interrupted and r2.shards == 3 and r2.records == 3
    first = (pack_dir / "records/lang=en/00000.jsonl.zst").stat().st_mtime_ns
    r3 = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    assert r3.shards == 3
    assert (pack_dir / "records/lang=en/00000.jsonl.zst").stat().st_mtime_ns == first, "closed shards are never rewritten"
    m = manifest_mod.load(pack_dir)
    assert [s["ids"][0] for s in m["shards"]] == ["gutenberg:10001", "gutenberg:10002", "gutenberg:10003"]


def test_manifest_is_signed_with_the_node_key_and_verifies(isolated_config, tmp_path, corpus_dir, monkeypatch):
    from app import config
    from app.bundles import keys as node_keys
    key_path = tmp_path / "node.key"
    key_path.write_bytes(node_keys.generate_seed())
    key_path.chmod(0o600)
    monkeypatch.setattr(config, "NODE_KEY_PATH", key_path)
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / "signed"
    pack_build.build(origin, pack_dir, log=lambda *_: None)
    m = manifest_mod.load(pack_dir)
    assert m["publisher"] and m["signature"] and manifest_mod.verify(m) is True
    assert m["collection_id"].startswith("urn:louise:gutenberg:")
    m["snapshot"] = "tampered"
    with pytest.raises(manifest_mod.ManifestError):
        manifest_mod.verify(m)


def test_the_plan_walk_is_a_rowid_scan_and_pubmed_hints_read_no_abstracts(isolated_config, tmp_path):
    """Plan-checked (CLAUDE.md): the planning page must never sort — with
    the scope filter in the WHERE clause SQLite sorted every matching row
    per page (500 rows/s live on a 7M-row manifest). And a pubmed hint
    costs two small columns per container, cached, not a row group of
    abstracts per record (20 records/s live)."""
    origin = _discovered(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    con = db.connect(origin)
    plan = " ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + pack_build.PLAN_PAGE_SQL, (-1, 10)))
    assert "TEMP B-TREE" not in plan and "USING INTEGER PRIMARY KEY" in plan, plan
    from app.plugins.pubmed import pack as pubmed_pack
    from app.plugins.pubmed.source import split_locator
    rows = con.execute("SELECT uid, native_id, path, lang FROM documents WHERE error IS NULL").fetchall()
    calls = []
    real = pubmed_pack.record_fields
    pubmed_pack.record_fields = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        hints = {r["uid"]: slicing.slice_hint("pubmed", r) for r in rows}
    finally:
        pubmed_pack.record_fields = real
    assert not calls, "the hint never reads a row group of abstracts"
    for r in rows:
        container, index = split_locator(r["path"])
        assert pubmed_pack.record_dates(container)[index] == real(container, index)["date"]
        assert hints[r["uid"]].startswith("year=")
    con.close()


def test_slicing_rules():
    from app.plugins.base import CanonicalDoc
    def doc(**kw):
        base = dict(uid="x", title="", creators=[], language="en", date=None, subjects=[], keys=[],
                    extra={}, sections=[], raw_hash="")
        base.update(kw)
        return CanonicalDoc(**base)
    assert slicing.pubmed_slice(doc(extra={"date": "1948"}), 1) == "year=pre1950"
    assert slicing.pubmed_slice(doc(extra={"date": "1977-Mar"}), 1) == "year=1975-1979"
    assert slicing.pubmed_slice(doc(extra={"date": "1999"}), 1) == "year=1995-1999"
    assert slicing.pubmed_slice(doc(date="2023-05"), 1) == "year=2023"
    assert slicing.pubmed_slice(doc(), 1) == "year=unknown"
    assert slicing.wikipedia_slice(doc(language="de"), 2_345_678) == "lang=de/pages=2M"
    assert slicing.embedded_slice(doc(), 10 * 10_000_000_000 + 77) == "lang=tr/pages=0M"
    assert slicing.gutenberg_slice(doc(language="it"), 5) == "lang=it"
    with pytest.raises(ValueError):
        slicing.check_slice_name("../etc")
    assert slicing.scheme_for("genome").records_per_shard == slicing.DEFAULT_SCHEME.records_per_shard
