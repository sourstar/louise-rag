"""Record shards (pack-format.md §3): JSON Lines in independent zstd
frames plus a trailing skippable frame holding the index. Round trip,
single-record access, plain-zstd readability, corruption refused."""
import io
import json
import struct

import pyarrow as pa
import pytest

from app.pack import records


def _rec(i: int, chunks: int = 1) -> dict:
    return {"uid": f"fake:{i}", "native_id": str(i), "lang": "en", "title": f"Doc {i}",
            "creators": ["A. Author"], "date": "2001", "subjects": [], "keys": [],
            "sections": [["Abstract", f"text of document {i} " * 3]], "extra": {"j": i},
            "layer": 0, "chunks": chunks,
            "content_hash": records.content_hash(f"Doc {i}", [["Abstract", "x"]]),
            "origin_hash": "00" * 16}


def test_round_trip_and_single_record_access(tmp_path):
    path = tmp_path / "00000.jsonl.zst"
    info = records.write_shard(path, (_rec(i, chunks=2) for i in range(1, 2501)), frame_records=1000)
    assert info.records == 2500 and info.chunks == 5000
    assert info.first_uid == "fake:1" and info.last_uid == "fake:2500"
    assert info.id_min == 1 and info.id_max == 2500
    assert info.sha256 == records.sha256_of(path)
    index = records.read_index(path)
    assert index.records == 2500 and len(index.frames) == 3
    assert [f[3] for f in index.frames] == [1000, 1000, 500]
    assert index.uids["fake:1001"] == [1, 0] and index.uids["fake:2500"] == [2, 499]
    # streamed, in order
    got = list(records.iter_records(path))
    assert [r["uid"] for r in got] == [f"fake:{i}" for i in range(1, 2501)]
    assert list(got[0].keys()) == list(records.RECORD_KEYS), "keys in the fixed order"
    # one record = one frame
    r = records.read_record(path, "fake:1777")
    assert r["title"] == "Doc 1777" and r["extra"] == {"j": 1777}
    with pytest.raises(KeyError):
        records.read_record(path, "fake:9999")


def test_plain_zstd_reads_the_records_and_skips_the_index(tmp_path):
    """`zstd -d` semantics: the file is a sequence of standard frames, the
    index is a skippable frame. Decoding frame by frame with the plain
    codec yields exactly the JSONL, and nothing else."""
    path = tmp_path / "s.jsonl.zst"
    records.write_shard(path, (_rec(i) for i in range(1, 11)), frame_records=4)
    data = path.read_bytes()
    index = records.read_index(path)
    text = b""
    for offset, csize, rsize, n, first in index.frames:
        frame = data[offset:offset + csize]
        assert struct.unpack("<I", frame[:4])[0] == 0xFD2FB528, "a standard zstd frame magic"
        text += bytes(pa.Codec("zstd").decompress(frame, decompressed_size=rsize))
    lines = text.decode("utf-8").splitlines()
    assert len(lines) == 10 and json.loads(lines[0])["uid"] == "fake:1"
    # the tail is the skippable frame and nothing follows it
    last = index.frames[-1]
    tail = data[last[0] + last[1]:]
    assert struct.unpack("<I", tail[:4])[0] == records.SKIPPABLE_MAGIC
    assert tail.endswith(records.FOOTER)
    out = io.BytesIO()
    assert records.dump_jsonl(path, out) == 10
    assert out.getvalue() == text


def test_duplicate_uid_and_corruption_are_refused(tmp_path):
    path = tmp_path / "d.jsonl.zst"
    with pytest.raises(records.ShardFormatError, match="duplicate"):
        records.write_shard(path, [_rec(1), _rec(1)])
    assert not path.exists() and not path.with_name(path.name + ".tmp").exists()
    records.write_shard(path, [_rec(1), _rec(2)])
    truncated = tmp_path / "t.jsonl.zst"
    truncated.write_bytes(path.read_bytes()[:-3])
    with pytest.raises(records.ShardFormatError):
        records.read_index(truncated)


def test_canonical_line_and_content_hash_are_stable():
    a = records.canonical_line({"uid": "x:1", "title": "T", "sections": [["A", "b"]], "extra": {"k": 1}})
    b = records.canonical_line({"extra": {"k": 1}, "sections": [["A", "b"]], "title": "T", "uid": "x:1"})
    assert a == b and a.endswith(b"\n")
    assert records.content_hash("T", [("A", "b")]) == records.content_hash("T", [["A", "b"]])
    assert records.content_hash("T", [("A", "b")]) != records.content_hash("T", [("A", "c")])
    assert len(records.content_hash("T", [])) == 32


# ---- the other container: Parquet (09 §9.5 "The file (B)", operator 2026-10-20) --------------------

def _unicode_rec(i: int) -> dict:
    r = _rec(i)
    r.update(title=f"Dóc «{i}» — ﬁne \"quoted\" \\ back", creators=["Ærøskøbing, Ø.", "日本"],
             subjects=["a/b", "c\td"], date=None, extra={"j": i, "s": "ü\n", "n": None, "l": [1, {"k": "v"}]},
             sections=[["Ch 1", "τεχτ " * 2], ["", "empty title"]])
    return r


def test_a_parquet_part_reads_as_the_jsonl_part_does_and_writes_the_same_lines(tmp_path):
    recs = [_unicode_rec(i) for i in range(1, 2501)]
    jsonl, parquet = tmp_path / "00001.jsonl.zst", tmp_path / "00001.parquet"
    a = records.write_shard(jsonl, recs, frame_records=1000)
    b = records.write_shard(parquet, recs, frame_records=1000)
    assert (b.records, b.chunks, b.first_uid, b.last_uid, b.id_min, b.id_max) == (
        a.records, a.chunks, a.first_uid, a.last_uid, a.id_min, a.id_max) == (2500, 2500, "fake:1", "fake:2500", 1, 2500)
    assert b.sha256 == records.sha256_of(parquet) and b.bytes == parquet.stat().st_size
    assert parquet.read_bytes()[:4] == b"PAR1", "a Parquet file any tool opens"
    # the same index shape: row groups as frames, the uid map
    ix = records.read_index(parquet)
    assert ix.records == 2500 and ix.chunks == 2500 and [f[3] for f in ix.frames] == [1000, 1000, 500]
    assert [f[4] for f in ix.frames] == ["fake:1", "fake:1001", "fake:2001"]
    assert ix.uids["fake:1001"] == [1, 0] and ix.uids["fake:2500"] == [2, 499]
    # every line byte for byte the JSONL part's: the line is a pure function of the row
    for k in range(3):
        assert records.read_frame(parquet, ix, k) == records.read_frame(jsonl, records.read_index(jsonl), k)
    assert [records.canonical_line(r)[:-1] for r in recs[:1000]] == records.read_frame(parquet, ix, 0)
    # and so the readers above them
    assert list(records.iter_records(parquet)) == list(records.iter_records(jsonl))
    assert records.read_record(parquet, "fake:1777") == records.read_record(jsonl, "fake:1777")
    out_a, out_b = io.BytesIO(), io.BytesIO()
    assert records.dump_jsonl(jsonl, out_a) == records.dump_jsonl(parquet, out_b) == 2500
    assert out_a.getvalue() == out_b.getvalue()
    with pytest.raises(KeyError):
        records.read_record(parquet, "fake:9999")
    # the same bytes for the same records: `root` agrees between honest peers
    again = tmp_path / "again.parquet"
    records.write_shard(again, recs, frame_records=1000)
    assert again.read_bytes() == parquet.read_bytes()
    # record_line 2: `figures` a nested column, `[]` when none
    two = tmp_path / "two.parquet"
    records.write_shard(two, [_rec(1), dict(_rec(2), figures=[{"hash": "ab" * 16}])], record_line=2)
    got = list(records.iter_records(two))
    assert got[0]["figures"] == [] and got[1]["figures"] == [{"hash": "ab" * 16}]
    assert list(got[0].keys()) == list(records.RECORD_KEYS_2)


def test_a_parquet_part_refuses_what_the_line_cannot_carry_and_a_file_that_is_not_one(tmp_path):
    with pytest.raises(records.ShardFormatError, match="duplicate uid"):
        records.write_shard(tmp_path / "dup.parquet", [_rec(1), _rec(1)])
    with pytest.raises(records.ShardFormatError, match="scalar is a string or null"):
        records.write_shard(tmp_path / "int.parquet", [dict(_rec(1), native_id=1)])
    assert not list(tmp_path.glob("*.parquet")), "nothing left behind"
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"PAR1 not really")
    with pytest.raises(records.ShardFormatError, match="not a Parquet part"):
        records.read_index(bad)
    import pyarrow as pa
    import pyarrow.parquet as pq
    pq.write_table(pa.table({"uid": ["a"], "other": ["b"]}), tmp_path / "cols.parquet")
    with pytest.raises(records.ShardFormatError, match="one column per key"):
        records.read_index(tmp_path / "cols.parquet")


def test_the_part_path_names_the_container():
    from app.hold import shard as shard_mod
    assert shard_mod.shard_path("all", 3) == "records/all/00003.jsonl.zst"
    assert shard_mod.shard_path("all", 3, container="parquet") == "records/all/00003.parquet"
    assert shard_mod.shard_path("all", 3, "2026-09", container="parquet") == "records/all/00003.2026-09.parquet"
    with pytest.raises(ValueError, match="container"):
        shard_mod.shard_path("all", 3, container="csv")


@pytest.mark.parametrize("suffix", [".jsonl.zst", ".parquet"])
def test_read_fields_gives_a_records_fields_without_the_rest(tmp_path, suffix):
    """The edition facts of a hit need `title`, `date` and `extra`; reading
    whole row groups for them decompressed whole books (München, 2026-10-22:
    a median 967 ms a search)."""
    path = tmp_path / f"part{suffix}"
    recs = [{**_rec(i), "extra": {"marc": {"260": [f"London : Macmillan, 18{i:02d}"]}, "n": i}}
            for i in range(5)]
    records.write_shard(path, recs)
    records.reset_caches()
    whole = records.read_record(path, recs[3]["uid"])
    got = records.read_fields(path, recs[3]["uid"], ("title", "date", "extra", "no_such_field"))
    assert got == {"title": whole["title"], "date": whole["date"], "extra": whole["extra"],
                   "no_such_field": None}
    with pytest.raises(KeyError):
        records.read_fields(path, "x:nope", ("title",))


def test_read_fields_does_not_decompress_the_text(tmp_path):
    """A Parquet row group of large books: three columns cost a fraction of
    the whole group."""
    import time
    path = tmp_path / "big.parquet"
    big = "lorem ipsum dolor sit amet " * 40_000           # ~1 MB of text a book
    recs = [{**_rec(i), "sections": [["Text", big + str(i)]]} for i in range(40)]
    records.write_shard(path, recs)
    uid = recs[20]["uid"]

    def timed(fn) -> float:
        best = float("inf")
        for _ in range(3):
            records.reset_caches()
            records.cached_index(path)                       # both pay the index alike
            t = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - t)
        return best
    whole = timed(lambda: records.read_record(path, uid))
    some = timed(lambda: records.read_fields(path, uid, ("title", "date", "extra")))
    assert some * 5 < whole, (some, whole)
