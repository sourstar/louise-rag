"""`pack build`: a collection's documents become sliced record shards + a
manifest — resumably, for gutenberg, pubmed and a synthesized wikipedia
dump. (Reading a pack back as a collection, the `packed` plugin, is boxed
in attic/ with its round-trip checks, 2026-09-14.)"""
import bz2
import threading
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery
from app.pack import build as pack_build, manifest as manifest_mod, records, slicing

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


def _pack_uids(pack_dir, m):
    return {r["uid"] for s in m["shards"]
            for r in records.iter_records(pack_dir / s["path"])}


def test_gutenberg_pack_slices_by_language_and_carries_every_record(isolated_config, tmp_path,
                                                                    corpus_dir):
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
    assert [s["path"] for s in m["shards"]] == ["records/lang=en/00001.jsonl.zst",
                                                "records/lang=fr/00001.jsonl.zst"]
    for s in m["shards"]:
        assert s["sha256"] == records.sha256_of(pack_dir / s["path"])
    assert (pack_dir / "validation.json").is_file()
    assert sorted(_pack_uids(pack_dir, m)) == \
        ["gutenberg:10001", "gutenberg:10002", "gutenberg:10003"]
    # a packed record is readable as JSON, keys in the fixed order
    rec = records.read_record(pack_dir / m["shards"][0]["path"], "gutenberg:10001")
    assert list(rec) == list(records.RECORD_KEYS)
    # §9.5 admits "exactly these keys": `layer` and `chunks` were carried for
    # the legacy verifier and are in neither §9.5's table nor 03 §3.2, which
    # derives the layer "from the Codex's rules, never reading it from the
    # record, which carries none". The shard still publishes its chunk COUNT
    # (the snapshot's per-shard field) — a record just no longer asserts
    # its own.
    assert "layer" not in rec and "chunks" not in rec
    assert m["shards"][0]["chunks"] >= 1, "the count is published per shard"


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
    # tombstoned (DeleteCitation) documents never enter a pack
    con = db.connect(origin)
    deleted = {r["uid"] for r in con.execute("SELECT uid FROM documents WHERE error IS NOT NULL")}
    con.close()
    assert not deleted & _pack_uids(pack_dir, m)


def _page(pid: int, title: str, text: str) -> str:
    return (f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n    <id>{pid}</id>\n"
            f"    <revision>\n      <id>{pid}00</id>\n      <text>{text}</text>\n    </revision>\n  </page>\n")


@pytest.fixture
def wiki_corpus(tmp_path) -> Path:
    # enwiki: simplewiki is not one of the collection's languages
    # (wikipedia parser 4), and used to pass as "en"
    d = tmp_path / "wiki-corpus" / "enwiki" / "latest"
    d.mkdir(parents=True)
    s1 = bz2.compress((_page(1, "The Moon", "'''The Moon''' is a satellite.\n\n== Orbit ==\nIt orbits.")
                       + _page(2, "Cheese", "'''Cheese''' is food.")).encode())
    s2 = bz2.compress(_page(1_500_000, "Far page", "A page with a high id.").encode())
    (d / "enwiki-latest-pages-articles-multistream.xml.bz2").write_bytes(s1 + s2)
    index = [f"0:1:The Moon", f"0:2:Cheese", f"{len(s1)}:1500000:Far page"]
    (d / "enwiki-latest-pages-articles-multistream-index.txt.bz2").write_bytes(
        bz2.compress(("\n".join(index) + "\n").encode()))
    from tests import wikipedia_props
    wikipedia_props.write(d / "enwiki-latest-pages-articles-multistream.xml.bz2",
                          [(1, "wikibase_item", "Q405")])
    return tmp_path / "wiki-corpus"


def test_wikipedia_pack_slices_by_language_and_page_range(isolated_config, tmp_path, wiki_corpus):
    origin = _discovered(tmp_path, "wikipedia", "wikipedia", wiki_corpus)
    pack_dir = tmp_path / "packs" / "wikipedia"
    pack_build.build(origin, pack_dir, snapshot="2026-08-26", sign=False, log=lambda *_: None)
    m = manifest_mod.load(pack_dir)
    assert set(m["slices"]) == {"lang=en/pages=0M", "lang=en/pages=1M"}
    assert m["layers"][0]["chunker"]["id"] == "cdc"
    assert (pack_dir / "records" / "lang=en" / "pages=1M" / "00001.jsonl.zst").is_file()


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
    # the parts handed to the writer threads before the stop was seen are
    # finished and recorded (2026-10-30), never left half-written
    assert r1.interrupted and "00001.jsonl.zst" in written
    assert r1.shards == len(written), "every part written is recorded"
    assert not (pack_dir / manifest_mod.FILENAME).exists()
    monkeypatch.setattr(records, "write_shard", real)
    said: list[str] = []
    r2 = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *a: said.append(" ".join(map(str, a))))
    assert not r2.interrupted and r2.shards == 3 and r2.records == 3
    assert any(m.endswith("lang=fr: 1 shard(s)") for m in said), said   # the shards written, not the next index
    first = (pack_dir / "records/lang=en/00001.jsonl.zst").stat().st_mtime_ns
    r3 = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    assert r3.shards == 3
    assert (pack_dir / "records/lang=en/00001.jsonl.zst").stat().st_mtime_ns == first, "closed shards are never rewritten"
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


def _planned(c, pack_dir, *, stop=None):
    con_src, con_build = db.connect(c), pack_build._build_db(pack_dir)
    try:
        pack_build.plan(c, con_src, con_build, log=lambda *_: None, stop=stop)
        return (sorted(tuple(r) for r in con_build.execute("SELECT uid, slice, ord FROM plan")),
                sorted(tuple(r) for r in con_build.execute("SELECT uid, reason FROM skipped")),
                pack_build._meta(con_build, "plan_done"))
    finally:
        con_src.close()
        con_build.close()


def test_the_plan_is_walked_in_rowid_windows_by_the_children(isolated_config, tmp_path, monkeypatch):
    """wikidata's plan walked 75.9 M rows on one core, 17–19 min (München,
    2026-10-30; multicore-audit.md item 11). The walk is cut into rowid
    windows the parse pool's children read and slice, committed in rowid
    order: the plan is the serial one, a stop between windows resumes."""
    import threading
    from app import config, parallel
    c = _discovered(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    serial = _planned(c, tmp_path / "serial")
    assert serial[0] and serial[2] == "1"
    plan = " ".join(r[3] for r in db.connect(c).execute("EXPLAIN QUERY PLAN " + pack_build.PLAN_WINDOW_SQL, (-1, 9)))
    assert "TEMP B-TREE" not in plan and "USING INTEGER PRIMARY KEY" in plan, plan
    pools = []

    class Spy(parallel.ProcessPoolExecutor):
        def __init__(self, *a, **k):
            pools.append(k.get("max_workers"))
            super().__init__(*a, **k)
    monkeypatch.setattr(parallel, "ProcessPoolExecutor", Spy)
    monkeypatch.setattr(config, "PARSE_PROCESSES", 2)
    monkeypatch.setattr(pack_build, "PLAN_WINDOW", max(1, len(serial[0]) // 5))
    assert _planned(c, tmp_path / "pooled") == serial and pools == [2]
    # stopped after its first window, then resumed: the same plan
    stop = threading.Event()
    real = pack_build._set_meta

    def stop_after_first(con, key, value):
        real(con, key, value)
        if key == "plan_rowid":
            stop.set()
    monkeypatch.setattr(pack_build, "_set_meta", stop_after_first)
    first = _planned(c, tmp_path / "resumed", stop=stop)
    assert first[2] is None and 0 < len(first[0]) < len(serial[0])
    assert not _has_plan_index(tmp_path / "resumed"), "the read order is built once the plan is done"
    monkeypatch.setattr(pack_build, "_set_meta", real)
    assert _planned(c, tmp_path / "resumed") == serial
    assert _has_plan_index(tmp_path / "resumed")


def _has_plan_index(pack_dir) -> bool:
    con = pack_build._build_db(pack_dir)
    try:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_plan_slice'"
                           ).fetchone() is not None
    finally:
        con.close()


def test_the_plan_s_read_order_is_indexed_once_the_plan_is_done(isolated_config, tmp_path, monkeypatch):
    """Bulk load, then index: the plan's commit is the build DB's single
    writer, and a second growing B-tree per row was half its cost
    (multicore-audit.md item 11). A build DB made before — the index there
    from its creation, the plan not done — has it dropped and made again at
    the end; the build's read of a slice is indexed, never sorted."""
    c = _discovered(tmp_path, "pubmed", "pubmed", FIXTURES / "pubmed-mini")
    pack_dir = tmp_path / "old"
    con = pack_build._build_db(pack_dir)
    con.execute("CREATE INDEX idx_plan_slice ON plan(slice, ord, uid)")     # the layout before
    con.close()
    calls = []
    real = pack_build._set_meta

    def watched(con, key, value):
        if key == "plan_rowid":
            calls.append(con.execute("SELECT 1 FROM sqlite_master WHERE name='idx_plan_slice'").fetchone())
        real(con, key, value)
    monkeypatch.setattr(pack_build, "_set_meta", watched)
    planned = _planned(c, pack_dir)
    assert planned[2] == "1" and calls and all(x is None for x in calls), "no index while the rows went in"
    con = pack_build._build_db(pack_dir)
    try:
        q = " ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT uid, ord FROM plan WHERE slice=? ORDER BY ord, uid", ("x",)))
    finally:
        con.close()
    assert "idx_plan_slice" in q and "TEMP B-TREE" not in q, q


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
    from app.plugins.wikipedia.ids import native_id_of
    # the native id carries the language and the page (wikipedia parser 4)
    assert slicing.wikipedia_slice(doc(language="de"), native_id_of("de", 2_345_678)) \
        == "lang=de/pages=2M"
    assert slicing.embedded_slice(doc(), 10 * 10_000_000_000 + 77) == "lang=tr/pages=0M"
    assert slicing.gutenberg_slice(doc(language="it"), 5) == "lang=it"
    with pytest.raises(ValueError):
        slicing.check_slice_name("../etc")
    assert slicing.scheme_for("genome").records_per_shard == slicing.DEFAULT_SCHEME.records_per_shard


# ---- 09 §9.4's roots, computed where the shards are actually cut ----------

def _shard_rows(pack_dir):
    import sqlite3
    con = sqlite3.connect(pack_dir / pack_build.BUILD_DIR / pack_build.BUILD_DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM shards ORDER BY slice, n")]
    con.close()
    return rows


def test_a_build_records_the_three_roots_of_9_4(isolated_config, tmp_path, corpus_dir):
    """`root`, `records_root` and `chunks_root` each answer a different
    question, and a snapshot carries all three (09 §9.4)."""
    from app.hold import shard as shard_mod, records as hold_records
    from app.pack import records as pack_records

    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / "g-roots"
    pack_build.build(origin, pack_dir, snapshot="2026-08-26", records_per_shard=2,
                     sign=False, log=lambda *_: None)

    rows = _shard_rows(pack_dir)
    assert rows, "the build wrote no shards"
    for row in rows:
        path = pack_dir / row["path"]
        # `root` is the BitTorrent v2 pieces root (09 §9.5), which the
        # build did not previously compute at all — it stored a plain
        # sha256 of the file beside it. The two COINCIDE for a file of at
        # most one 16 KiB block, because BEP 52 makes such a file its own
        # single leaf; these fixture shards are that small, and the next
        # assertion is what separates them in general.
        assert row["root"] == shard_mod.pieces_root_of(path)
        assert (path.stat().st_size <= shard_mod.BLOCK) == \
            (row["root"] == "sha256:" + row["sha256"])
        # `records_root` is over the content hashes in record order, and is
        # reproducible from the shard's own bytes.
        hashes = [r["content_hash"] for r in pack_records.iter_records(path)]
        assert row["records_root"] == hold_records.records_root(hashes)
        assert row["chunks_root"].startswith("sha256:")
        assert row["first_native_id"] and row["last_native_id"]


def test_chunks_root_reproduces_from_the_records_and_the_layers(
        isolated_config, tmp_path, corpus_dir):
    """The `chunks` check recomputes chunks from the records and compares
    (03 §3.2). If the build's incremental root and a fresh re-chunk
    disagree, the check can never pass."""
    from app import layers as layers_mod
    from app.hold import records as hold_records
    from app.pack import records as pack_records

    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    pack_dir = tmp_path / "packs" / "g-chunks"
    pack_build.build(origin, pack_dir, snapshot="2026-08-26", records_per_shard=2,
                     sign=False, log=lambda *_: None)

    for row in _shard_rows(pack_dir):
        pairs = []
        for r in pack_records.iter_records(pack_dir / row["path"]):
            native_id = int(r["native_id"])
            chunker = layers_mod.chunker_for_document(origin, native_id)
            for c in chunker(r["uid"], native_id, r["sections"]):
                pairs.append((c["id"], c["text"]))
        assert len(pairs) == row["chunks"]
        assert hold_records.chunks_root(pairs) == row["chunks_root"], row["path"]


def test_a_shard_that_is_re_cut_gets_the_same_roots(isolated_config, tmp_path,
                                                    corpus_dir):
    """Two honest peers that build the same cutoff produce the same bytes,
    so a second build over the same corpus reproduces every root."""
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    first = tmp_path / "packs" / "g-a"
    second = tmp_path / "packs" / "g-b"
    for d in (first, second):
        pack_build.build(origin, d, snapshot="2026-08-26", records_per_shard=2,
                         sign=False, log=lambda *_: None)
    a, b = _shard_rows(first), _shard_rows(second)
    assert [(r["root"], r["records_root"], r["chunks_root"]) for r in a] == \
           [(r["root"], r["records_root"], r["chunks_root"]) for r in b]


def test_an_out_of_order_native_id_aborts_the_build(isolated_config, tmp_path,
                                                    corpus_dir, monkeypatch):
    """02 §2.2: refused, naming the record and the shard — not skipped, and
    not placed out of order. A shard cut two ways is not repairable by the
    peer who receives it."""
    from app.hold import records as hold_records

    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    calls = {"n": 0}
    real = hold_records.refuse_out_of_order

    def descending(native_id, open_first_id, order, *, slice_, n):
        # the second record of a shard arrives with an id below the first
        calls["n"] += 1
        if open_first_id is not None:
            return real("1", "999999", order, slice_=slice_, n=n)
        return real(native_id, open_first_id, order, slice_=slice_, n=n)

    monkeypatch.setattr(pack_build.hold_records, "refuse_out_of_order", descending)
    with pytest.raises(hold_records.RecordsError, match="sorts before"):
        pack_build.build(origin, tmp_path / "packs" / "g-bad", snapshot="2026-08-26",
                         records_per_shard=2, sign=False, log=lambda *_: None)
    assert calls["n"] >= 2


def test_parts_are_written_off_the_main_thread_and_recorded_in_order(isolated_config, tmp_path, corpus_dir,
                                                                    monkeypatch):
    """München's samples of wikipedia-v8's rebuild (2026-10-30): compression
    and the pieces root in the build's own thread were most of its time,
    while the pool's children waited for it. The parts are written by the
    writer threads; the build database records them in their order."""
    origin = _discovered(tmp_path, "gutenberg", "gutenberg", corpus_dir)
    threads: list[str] = []
    real = records.write_shard

    def spy(path, recs, **kw):
        threads.append(threading.current_thread().name)
        return real(path, recs, **kw)
    monkeypatch.setattr(records, "write_shard", spy)
    pack_dir = tmp_path / "packs" / "g"
    r = pack_build.build(origin, pack_dir, records_per_shard=1, sign=False, log=lambda *_: None)
    assert r.shards == 3 and threads and all(t.startswith("part-writer") for t in threads), threads
    m = manifest_mod.load(pack_dir)
    assert [s["ids"][0] for s in m["shards"]] == ["gutenberg:10001", "gutenberg:10002", "gutenberg:10003"]
