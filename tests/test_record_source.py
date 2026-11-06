"""One parse: a stage reads the document's fresh record instead of parsing
its origin (operator, 2026-10-31; app/record_source.py,
docs/notes/one-parse.md)."""
from types import SimpleNamespace

import pytest

from app import record_source as rs
from app.pack import records as pack_records

SECTIONS = {"g:1": [["Chapter 1", "Call me Ishmael."]], "g:2": [["", "It was the best of times."]],
            "g:3": [["Part I", "Happy families are all alike."]]}


@pytest.fixture
def source(tmp_path, monkeypatch):
    part = tmp_path / "shard" / "records" / "lang=en" / "00000.jsonl.zst"
    part.parent.mkdir(parents=True)
    pack_records.write_shard(part, [
        {"uid": uid, "native_id": int(uid[2:]), "lang": "en", "title": uid, "sections": secs,
         "origin_hash": f"o{uid[2:]}"} for uid, secs in SECTIONS.items()])
    shard = SimpleNamespace(path="records/lang=en/00000.jsonl.zst", slice="lang=en", n=0)
    snap = SimpleNamespace(shards_of=lambda nid, order="bytes": [shard],
                           lookup_order=lambda nid, order="bytes": [shard])
    codex = SimpleNamespace(slicing={"order": "numeric"}, parser=SimpleNamespace(version=3))
    collection = SimpleNamespace(name="g", manifest_path=tmp_path / "state" / "manifest.db")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(rs, "_context_now", lambda c: (tmp_path / "shard", snap, codex))
    rs._contexts.clear()
    pack_records.reset_caches()
    monkeypatch.setattr(rs, "SAMPLE_EVERY", 10**9)          # no sampling unless a test asks
    return collection


def _row(uid, origin=None):
    return {"uid": uid, "native_id": uid[2:], "txt_hash": origin if origin is not None else f"o{uid[2:]}"}


def test_a_fresh_record_is_read_and_the_origin_is_not_parsed(source):
    parsed = []
    got = rs.sections(source, _row("g:1"), lambda: parsed.append(1) or [("x", "parsed")])
    assert got == [("Chapter 1", "Call me Ishmael.")] and parsed == []
    assert rs.lookup(source, _row("g:3"))[1] == "fresh"


@pytest.mark.parametrize("row, why", [
    (_row("g:2", origin="another"), "origin-differs"),
    (_row("g:9"), "not-in-snapshot"),
    ({"uid": "g:1", "native_id": "1", "txt_hash": None, "path": "elsewhere"}, "no-origin-hash"),
])
def test_a_row_without_a_fresh_record_is_parsed(source, row, why):
    assert rs.lookup(source, row) == (None, why)
    assert rs.sections(source, row, lambda: [("x", "parsed")]) == [("x", "parsed")]


def test_no_records_for_the_collection_parses(source, monkeypatch):
    monkeypatch.setattr(rs, "_context_now", lambda c: None)
    rs._contexts.clear()
    assert rs.lookup(source, _row("g:1")) == (None, "no-records")


def test_a_sampled_mismatch_stops_the_shortcut_for_the_collection(source, monkeypatch):
    monkeypatch.setattr(rs, "SAMPLE_EVERY", 1)                # every row sampled
    same = rs.sections(source, _row("g:1"), lambda: [("Chapter 1", "Call me Ishmael.")])
    assert same == [("Chapter 1", "Call me Ishmael.")] and not rs.stopped(source)
    differs = rs.sections(source, _row("g:2"), lambda: [("", "It was the worst of times.")])
    assert differs == [("", "It was the worst of times.")], "the parse is used, not the record"
    assert rs.stopped(source)
    assert (source.manifest_path.parent / rs.MARK).exists()
    parsed = []
    rs.sections(source, _row("g:3"), lambda: parsed.append(1) or [("x", "y")])
    assert parsed == [1], "no record is trusted until a person removes the mark"


def test_a_different_parser_version_means_no_records(tmp_path, monkeypatch):
    codex = SimpleNamespace(parser=SimpleNamespace(version=3), slicing={})
    plugin = SimpleNamespace(PARSER_VERSION=4)
    collection = SimpleNamespace(name="g", codex=codex, source="origin",
                                 plugin_instance=lambda: plugin, manifest_path=tmp_path / "m.db")
    assert rs._context_now(collection) is None
    fetched = SimpleNamespace(**{**vars(collection), "source": "shard"})
    assert rs._context_now(fetched) is None


def test_neighbouring_rows_share_one_index_read_and_one_frame(source, monkeypatch):
    """The cost: a part's index is read once and a frame decompressed once
    for the rows that fall in it (rows arrive contiguous, parallel._partition)."""
    reads = {"index": 0, "frame": 0}
    real_index, real_frame = pack_records.read_index, pack_records.read_frame
    monkeypatch.setattr(pack_records, "read_index",
                        lambda p: reads.__setitem__("index", reads["index"] + 1) or real_index(p))
    monkeypatch.setattr(pack_records, "read_frame",
                        lambda p, i, k: reads.__setitem__("frame", reads["frame"] + 1) or real_frame(p, i, k))
    for uid in SECTIONS:
        rs.sections(source, _row(uid), lambda: [])
    assert reads == {"index": 1, "frame": 1}


def test_a_row_without_its_hash_is_fresh_when_the_record_locates_its_own_unit(source, monkeypatch):
    """wikipedia's dump layer, 2026-10-31: every row's txt_hash is NULL (re-
    discovered). The record whose `origin` is the row's own locator is the
    same unit: fresh. Another locator: parsed."""
    monkeypatch.setattr(rs, "_locator", lambda c, row: row["path"])
    part = rs._context_now(source)[0] / "records" / "lang=en" / "00000.jsonl.zst"
    pack_records.write_shard(part, [
        {"uid": "g:1", "native_id": 1, "lang": "en", "title": "g:1", "sections": SECTIONS["g:1"],
         "origin_hash": "o1", "origin": "dumps/enwiki.xml.bz2::10::20"}])
    pack_records.reset_caches()
    row = {"uid": "g:1", "native_id": "1", "txt_hash": None, "path": "dumps/enwiki.xml.bz2::10::20"}
    assert rs.lookup(source, row)[1] == "fresh"
    assert rs.lookup(source, {**row, "path": "dumps/enwiki.xml.bz2::30::40"})[1] == "no-origin-hash"


def test_a_lookup_reads_the_true_part_first_when_a_part_straddles_every_id(tmp_path, monkeypatch):
    """The cost, not only the answer (scheduling.md §6.7): wikipedia's
    straddling parts made a lookup read 166 indexes, 166 ms a document."""
    from app.hold import snapshot as sn
    tree = tmp_path / "shard"
    def part(sl, n, recs):
        p = tree / "records" / sl / f"{n:05d}.jsonl.zst"
        p.parent.mkdir(parents=True, exist_ok=True)
        pack_records.write_shard(p, [{"uid": f"g:{i}", "native_id": i, "lang": "en", "title": "t",
                                      "sections": [["", f"text {i}"]], "origin_hash": f"o{i}"} for i in recs])
        return p
    shards = []
    for k in range(20):                                   # twenty straddlers, none holds 5050
        part(f"lang={k:02d}", 1, [10 + k, 10**9 + k])
        shards.append(sn.Shard(slice=f"lang={k:02d}", n=1, root="r", records_root="r", first_id=str(10 + k),
                               last_id=str(10**9 + k), records=2, chunks=2, chunks_root="c", tokens=2, bytes=1))
    part("lang=zz", 1, [5000, 5050, 5100])
    shards.append(sn.Shard(slice="lang=zz", n=1, root="r", records_root="r", first_id="5000", last_id="5100",
                           records=3, chunks=3, chunks_root="c", tokens=3, bytes=1))
    snap = sn.Snapshot(collection_id="sha256:" + "0" * 64, snapshot_id="2026-11", previous=None,
                       shards=tuple(shards), delta=sn.Delta((), (), ()))
    codex = SimpleNamespace(slicing={"order": "numeric"}, parser=SimpleNamespace(version=3))
    monkeypatch.setattr(rs, "_context_now", lambda c: (tree, snap, codex))
    rs._contexts.clear()
    pack_records.reset_caches()
    reads = []
    real = pack_records.read_index
    monkeypatch.setattr(pack_records, "read_index", lambda p: reads.append(p) or real(p))
    collection = SimpleNamespace(name="g", manifest_path=tmp_path / "m.db")
    rec, why = rs.lookup(collection, {"uid": "g:5050", "native_id": "5050", "txt_hash": "o5050"})
    assert why == "fresh" and rec["uid"] == "g:5050"
    assert [p.parent.name for p in reads] == ["lang=zz"], f"read {len(reads)} indexes for one record"
