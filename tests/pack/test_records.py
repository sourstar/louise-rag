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
