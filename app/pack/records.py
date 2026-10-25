"""Record shards — pack-format.md §3.

One shard = JSON Lines, one record per line, UTF-8, keys in RECORD_KEYS
order, compressed as INDEPENDENT zstd frames of FRAME_RECORDS records so
that one record costs one frame (measured 1.0 ms), followed by a zstd
SKIPPABLE frame holding the frame table and the uid index. Standard zstd
tooling ignores skippable frames, so

    zstd -d shard.jsonl.zst | less

is the whole inspection, and no dependency beyond pyarrow's bundled zstd
codec is needed to read or write one.

Layout of the trailing skippable frame (all integers little-endian):

    magic u32 = 0x184D2A5E   size u32   payload[size]
    payload = TAG (16 bytes) | raw_len u32 | zstd(json index) | frame_len u32 | FOOTER (4 bytes)

The reader takes the last 8 bytes (frame_len + FOOTER), seeks back
frame_len bytes to the payload start, and decodes the index. The index:

    {"records": N, "frames": [[offset, csize, rsize, n, first_uid], ...],
     "uids": {uid: [frame, line], ...}}

Frame offsets are absolute in the file, so a frame is one pread.

THE OTHER CONTAINER (09 §9.5 "The file (B): Parquet", operator 2026-10-20;
docs/notes/gutenberg-graph-amendments.md A-K4). A part whose path ends in
`.parquet` is one Parquet file: one row per record in record order, one
STRING column per key of the record line in the line's key order — a
scalar (`uid`, `title`, `date`, …) as the string or null, a nested value
(`creators`, `subjects`, `keys`, `sections`, `extra`, `figures`) as its
compact JSON text — so that the §9.5 LINE is a pure function of the row
(`line_of_row`, byte for byte `canonical_line`); zstd columns, row groups
of FRAME_RECORDS, no statistics, the record and chunk counts in the
schema metadata. The row group is the frame: `read_index` lists them as
`[group, 0, 0, n, first_uid]` with the uid map built from the `uid`
column, `read_frame` renders a row group's lines, and everything above
them — `iter_records`, `read_record`, the caches, `dump_jsonl` — reads
either container unchanged. `records_root` is over the records'
`content_hash` values and so the same for both; `root` is the file's.
The container is the Codex's to say (`slicing.container`, Codex version
9) and the path's suffix to tell.
"""
import hashlib
import io
import json
import os
import struct
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

import pyarrow as pa

FRAME_RECORDS = 1000
ZSTD_LEVEL = 9
SKIPPABLE_MAGIC = 0x184D2A5E
TAG = b"LOUISE-PACK-IDX1"          # 16 bytes
FOOTER = b"LPK1"
SUFFIX = ".jsonl.zst"
#: the Parquet container's suffix (09 §9.5 "The file (B)")
PARQUET_SUFFIX = ".parquet"
#: the record line's nested values: JSON text in their column
NESTED_KEYS = frozenset({"creators", "subjects", "keys", "sections", "extra", "figures"})
#: the schema metadata a Parquet part carries (the JSONL index's counts)
_META_RECORDS, _META_CHUNKS = b"louise.records", b"louise.chunks"
#: who compresses a part's bytes: the zstd pyarrow bundles (statically
#: linked, its own version not exported), so the wheel's version names it.
#: A snapshot's part entry carries it (09 §9.4 `writer`): a peer rebuilding
#: a part from a patch matches the root for certain only under the same one.
WRITER = f"pyarrow {pa.__version__}"

# The record's keys, in the order they are written (a fixed order is what
# makes two writers produce byte-identical lines for the same record).
#: `origin` landed 2026-09-10, by operator decision, and it is 09 §9.5's
#: order: after `extra`, before the two hashes. It is HALF OF INVARIANT 11
#: — `origin_hash` is the hash of the origin unit and `origin` locates
#: that unit — and without it a peer that RECEIVED a shard could never
#: re-fetch its source, which is what 03 §3.4 and two rows of §3.6's level
#: table rest on. Adding it changes each line's bytes and so each shard
#: file's transport root; it changes NO `records_root` (over `content_hash`
#: values), no `chunks_root`, no chunk id and no vector.
#:
#: `layer` and `chunks` ARE GONE, 2026-09-12. They were in NEITHER §9.5's
#: table nor 03 §3.2 — which says the layer is derived "from the Codex's
#: rules, never reading it from the record, which carries none" — and
#: §9.5 admits "exactly these keys", so a line carrying two more was not a
#: compliant line. They stayed for the legacy pack verifier; that verifier
#: reads them with `.get(...) or 0`, which is the right answer for a
#: single-layer collection and is now what it gets.
#:
#: They are still PUT on the in-memory record by `pack/build.py`, and
#: `write_shard` still totals a shard's chunks from there — `canonical_line`
#: takes only the keys below and ignores the rest, so nothing internal had
#: to move to stop publishing them.
RECORD_KEYS = ("uid", "native_id", "lang", "title", "creators", "date",
               "subjects", "keys", "sections", "extra",
               "origin", "content_hash", "origin_hash")

#: 09 §9.5's OTHER shape. A line has no version field of its own: its shape
#: is its Codex's `record_line`, 1 without `figures` and 2 with it, and the
#: key sits between `sections` and `extra` because that is where the table
#: puts it. A collection whose records gain plates is a NEW collection (02
#: §2.7) — `record_line` is inside `collection_id` — so a shard never
#: mixes the two, and the writer takes the line as an argument rather than
#: sniffing a record for a `figures` key: a record_line-2 book with no
#: plates carries `"figures": []` and must still be written as shape 2.
RECORD_KEYS_2 = tuple(
    k for key in RECORD_KEYS
    for k in ((key, "figures") if key == "sections" else (key,)))

RECORD_KEYS_BY_LINE = {1: RECORD_KEYS, 2: RECORD_KEYS_2}


class ShardFormatError(RuntimeError):
    """The file is not a record shard this reader understands."""


@dataclass(frozen=True)
class ShardInfo:
    path: Path
    records: int
    chunks: int
    bytes: int
    sha256: str
    first_uid: str | None
    last_uid: str | None
    id_min: int | None
    id_max: int | None


def _codec(level: int | None = None):
    return pa.Codec("zstd", compression_level=level) if level else pa.Codec("zstd")


def keys_for_line(record_line: int) -> tuple[str, ...]:
    try:
        return RECORD_KEYS_BY_LINE[int(record_line)]
    except (KeyError, TypeError, ValueError):
        raise ShardFormatError(
            f"record_line is 1 or 2 (09 §9.5), got {record_line!r}") from None


def is_parquet(path) -> bool:
    """Which container a part's path names (09 §9.5)."""
    return str(path).endswith(PARQUET_SUFFIX)


def _ordered(record: dict, record_line: int) -> dict:
    keys = keys_for_line(record_line)
    ordered = {k: record.get(k) for k in keys}
    if record_line == 2 and ordered["figures"] is None:
        # `[]` when it has none, never null: §9.5 makes the key required in
        # shape 2, and a reader distinguishing "no plates" from "the writer
        # forgot" is a distinction the format does not offer.
        ordered["figures"] = []
    return ordered


def canonical_line(record: dict, *, record_line: int = 1) -> bytes:
    """The record as one JSON line: the shape's key order, compact, UTF-8.

    `record_line` defaults to 1 — the shape every collection that predates
    the figures amendment writes, and the one a caller that has no Codex to
    hand (a test fixture, `dev migrate pack`) means.
    """
    ordered = _ordered(record, record_line)
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


def _cell(key: str, value) -> str | None:
    """A record value as its Parquet cell: nested values as the JSON text
    the line writes for them, scalars as the string or null."""
    if key in NESTED_KEYS:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShardFormatError(f"{key}: a record line's scalar is a string or null (09 §9.5), "
                               f"got {type(value).__name__} {value!r}")
    return value


def line_of_row(keys: Sequence[str], row: dict) -> bytes:
    """09 §9.5's line from a Parquet row — the bytes `canonical_line` writes
    for the record, without the newline (as `read_frame` returns lines)."""
    parts = []
    for k in keys:
        v = row[k]
        frag = v if k in NESTED_KEYS else json.dumps(v, ensure_ascii=False)
        parts.append(json.dumps(k) + ":" + frag)
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def content_hash(title: str, sections: list) -> str:
    """BLAKE2b-128 of the CANONICAL text — the fields the chunker sees, in
    a fixed encoding. Same hash → same chunks → same vectors, whatever the
    origin's version fields say (pack-format.md §6)."""
    blob = json.dumps([title or "", [[n or "", b or ""] for n, b in sections]],
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(blob, digest_size=16).hexdigest()


def write_shard(path: Path, records: Iterable[dict], *, level: int = ZSTD_LEVEL,
                frame_records: int = FRAME_RECORDS,
                record_line: int = 1) -> ShardInfo:
    """Write `records` (dicts with RECORD_KEYS) as one shard, atomically
    (tmp + rename). Records are written in the order given. A path ending
    in `.parquet` is written in the other container (09 §9.5 "The file (B)").

    `record_line` comes from the collection's Codex and is 1 or 2 (09
    §9.5); it is the SHARD's, not the record's, so every line in a file is
    the same shape.
    """
    keys_for_line(record_line)          # refuse a bad line before any bytes
    path = Path(path)
    if is_parquet(path):
        return _write_parquet(path, records, level=level, row_group=frame_records, record_line=record_line)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    codec = _codec(level)
    frames: list[list] = []
    uids: dict[str, list[int]] = {}
    n_records = n_chunks = 0
    first_uid = last_uid = None
    id_min = id_max = None
    sha = hashlib.sha256()

    def flush(buf: list[bytes], f) -> None:
        nonlocal n_records
        if not buf:
            return
        raw = b"".join(buf)
        comp = bytes(codec.compress(raw))
        offset = f.tell()
        f.write(comp)
        sha.update(comp)
        frames.append([offset, len(comp), len(raw), len(buf), first_in_frame[0]])
        buf.clear()

    try:
        with open(tmp, "wb") as f:
            buf: list[bytes] = []
            first_in_frame = [None]
            for record in records:
                uid = record["uid"]
                if uid in uids:
                    raise ShardFormatError(f"duplicate uid in shard: {uid}")
                if not buf:
                    first_in_frame[0] = uid
                uids[uid] = [len(frames), len(buf)]
                buf.append(canonical_line(record, record_line=record_line))
                n_records += 1
                n_chunks += int(record.get("chunks") or 0)
                first_uid = first_uid or uid
                last_uid = uid
                try:
                    nid = int(record.get("native_id"))
                    id_min = nid if id_min is None else min(id_min, nid)
                    id_max = nid if id_max is None else max(id_max, nid)
                except (TypeError, ValueError):
                    pass
                if len(buf) >= frame_records:
                    flush(buf, f)
            flush(buf, f)
            index = {"records": n_records, "chunks": n_chunks, "frames": frames, "uids": uids}
            raw_index = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            comp_index = bytes(codec.compress(raw_index))
            body = TAG + struct.pack("<I", len(raw_index)) + comp_index
            # frame_len = the WHOLE skippable frame: 8-byte header + payload,
            # where payload = body + this u32 + FOOTER
            payload = body + struct.pack("<I", len(body) + 16) + FOOTER
            frame = struct.pack("<II", SKIPPABLE_MAGIC, len(payload)) + payload
            f.write(frame)
            sha.update(frame)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ShardInfo(path=path, records=n_records, chunks=n_chunks,
                     bytes=path.stat().st_size, sha256=sha.hexdigest(),
                     first_uid=first_uid, last_uid=last_uid, id_min=id_min, id_max=id_max)


def _write_parquet(path: Path, records: Iterable[dict], *, level: int, row_group: int,
                   record_line: int) -> ShardInfo:
    """09 §9.5 "The file (B)": the records as one Parquet file, atomically.
    The same bytes for the same records under the same pyarrow (the
    platform image pins it, as it pins the zstd of the frames), so honest
    peers agree on `root` here too."""
    import pyarrow.parquet as pq
    keys = keys_for_line(record_line)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    cols: dict[str, list] = {k: [] for k in keys}
    seen: set[str] = set()
    n_records = n_chunks = 0
    first_uid = last_uid = None
    id_min = id_max = None
    for record in records:
        uid = record["uid"]
        if uid in seen:
            raise ShardFormatError(f"duplicate uid in shard: {uid}")
        seen.add(uid)
        ordered = _ordered(record, record_line)
        for k in keys:
            cols[k].append(_cell(k, ordered[k]))
        n_records += 1
        n_chunks += int(record.get("chunks") or 0)
        first_uid = first_uid or uid
        last_uid = uid
        try:
            nid = int(record.get("native_id"))
            id_min = nid if id_min is None else min(id_min, nid)
            id_max = nid if id_max is None else max(id_max, nid)
        except (TypeError, ValueError):
            pass
    table = pa.table({k: pa.array(cols[k], type=pa.string()) for k in keys})
    table = table.replace_schema_metadata({_META_RECORDS: str(n_records).encode(),
                                           _META_CHUNKS: str(n_chunks).encode()})
    try:
        pq.write_table(table, tmp, compression="zstd", compression_level=level,
                       row_group_size=row_group, use_dictionary=False, write_statistics=False,
                       data_page_version="2.0", version="2.6")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ShardInfo(path=path, records=n_records, chunks=n_chunks, bytes=path.stat().st_size,
                     sha256=sha256_of(path), first_uid=first_uid, last_uid=last_uid,
                     id_min=id_min, id_max=id_max)


def _parquet_file(path: Path):
    import pyarrow.parquet as pq
    try:
        pf = pq.ParquetFile(path)
    except (pa.ArrowInvalid, pa.ArrowException, OSError) as e:
        raise ShardFormatError(f"{Path(path).name}: not a Parquet part ({e})") from None
    names = pf.schema_arrow.names
    if "uid" not in names or any(k not in names for k in RECORD_KEYS):
        raise ShardFormatError(f"{Path(path).name}: a Parquet part has one column per key of the "
                               f"record line (09 §9.5); this one has {', '.join(names)}")
    return pf


def _parquet_index(path: Path) -> "ShardIndex":
    """The row groups as frames — `[group, 0, 0, n, first_uid]` — and the
    uid map from the `uid` column alone, one small read per group."""
    path = Path(path)
    pf = _parquet_file(path)
    frames: list[list] = []
    uids: dict[str, list[int]] = {}
    n = 0
    for g in range(pf.metadata.num_row_groups):
        col = pf.read_row_group(g, columns=["uid"]).column("uid").to_pylist()
        frames.append([g, 0, 0, len(col), col[0] if col else None])
        for i, uid in enumerate(col):
            uids[uid] = [g, i]
        n += len(col)
    meta = pf.schema_arrow.metadata or {}
    if meta.get(_META_RECORDS) is not None and int(meta[_META_RECORDS]) != n:
        raise ShardFormatError(f"{path.name}: {n} rows, the file says {int(meta[_META_RECORDS])}")
    return ShardIndex(path=path, mtime_ns=path.stat().st_mtime_ns, records=n,
                      chunks=int(meta.get(_META_CHUNKS) or 0), frames=frames, uids=uids)


def _parquet_frame(path: Path, index: "ShardIndex", k: int) -> list[bytes]:
    """The lines of row group k, rendered from its rows."""
    pf = _parquet_file(path)
    _group, _, _, n, _first = index.frames[k]
    table = pf.read_row_group(k)
    keys = table.schema.names
    lines = [line_of_row(keys, row) for row in table.to_pylist()]
    if len(lines) != n:
        raise ShardFormatError(f"{Path(path).name}: row group {k} holds {len(lines)} rows, index says {n}")
    return lines


def write_lines(path: Path, lines: Iterable[bytes], *, key_of, level: int = ZSTD_LEVEL,
                frame_records: int = FRAME_RECORDS, chunks: int = 0,
                frame_counts: list[int] | None = None) -> ShardInfo:
    """§9.5's framing over lines that are not records — 09 §9.14's edge
    lines — with the index's `uids` map keyed by `key_of(line)` (the frame
    and line of the first line of each key). The same frames, index and
    footer as `write_shard`, so `read_index`, `read_frame` and
    `iter_records` read the file; `chunks` is 0.

    A record part rebuilt from its lines (app/share/patch.py) passes the
    producer's `chunks` and `frame_counts` (the lines per frame, from its
    index): the same lines then give `write_shard`'s bytes exactly, so the
    rebuilt file's root is the snapshot's (tests/share/test_patch.py)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    codec = _codec(level)
    frames: list[list] = []
    uids: dict[str, list[int]] = {}
    n = 0
    first = last = None
    sha = hashlib.sha256()
    try:
        with open(tmp, "wb") as f:
            buf: list[bytes] = []
            first_in_frame = [None]

            def flush() -> None:
                if not buf:
                    return
                raw = b"".join(buf)
                comp = bytes(codec.compress(raw))
                frames.append([f.tell(), len(comp), len(raw), len(buf), first_in_frame[0]])
                f.write(comp)
                sha.update(comp)
                buf.clear()
            for line in lines:
                key = key_of(line)
                if not buf:
                    first_in_frame[0] = key
                uids.setdefault(key, [len(frames), len(buf)])
                buf.append(line if line.endswith(b"\n") else line + b"\n")
                n += 1
                first = first or key
                last = key
                limit = (frame_counts[len(frames)] if frame_counts and len(frames) < len(frame_counts)
                         else frame_records)
                if len(buf) >= limit:
                    flush()
            flush()
            index = {"records": n, "chunks": chunks, "frames": frames, "uids": uids}
            raw_index = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            comp_index = bytes(codec.compress(raw_index))
            body = TAG + struct.pack("<I", len(raw_index)) + comp_index
            payload = body + struct.pack("<I", len(body) + 16) + FOOTER
            frame = struct.pack("<II", SKIPPABLE_MAGIC, len(payload)) + payload
            f.write(frame)
            sha.update(frame)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return ShardInfo(path=path, records=n, chunks=chunks, bytes=path.stat().st_size, sha256=sha.hexdigest(),
                     first_uid=first, last_uid=last, id_min=None, id_max=None)


@dataclass
class ShardIndex:
    path: Path
    mtime_ns: int
    records: int
    chunks: int
    frames: list[list]                 # [offset, csize, rsize, n, first_uid]
    uids: dict[str, list[int]]         # uid -> [frame, line]


def read_index(path: Path) -> ShardIndex:
    """Parse the trailing skippable frame. Two small reads. A Parquet part's
    index is its row groups (09 §9.5 "The file (B)")."""
    path = Path(path)
    if is_parquet(path):
        return _parquet_index(path)
    st = path.stat()
    with open(path, "rb") as f:
        if st.st_size < 16:
            raise ShardFormatError(f"{path.name}: too short to be a shard")
        f.seek(st.st_size - 8)
        frame_len, footer = struct.unpack("<I", f.read(4))[0], f.read(4)
        if footer != FOOTER:
            raise ShardFormatError(f"{path.name}: no pack index footer")
        start = st.st_size - frame_len
        if start < 0:
            raise ShardFormatError(f"{path.name}: index frame length exceeds the file")
        f.seek(start)
        magic, size = struct.unpack("<II", f.read(8))
        if magic != SKIPPABLE_MAGIC or size != frame_len - 8:
            raise ShardFormatError(f"{path.name}: index frame is not a zstd skippable frame")
        payload = f.read(size)
    if payload[:16] != TAG:
        raise ShardFormatError(f"{path.name}: unknown index tag")
    raw_len = struct.unpack("<I", payload[16:20])[0]
    comp = payload[20:len(payload) - 8]
    raw = bytes(_codec().decompress(comp, decompressed_size=raw_len))
    index = json.loads(raw.decode("utf-8"))
    return ShardIndex(path=path, mtime_ns=st.st_mtime_ns, records=int(index["records"]),
                      chunks=int(index.get("chunks") or 0), frames=index["frames"],
                      uids=index["uids"])


def read_frame(path: Path, index: ShardIndex, k: int) -> list[bytes]:
    """The lines of frame k (one pread + one decompress); of row group k
    for a Parquet part."""
    if is_parquet(path):
        return _parquet_frame(path, index, k)
    offset, csize, rsize, n, _first = index.frames[k]
    with open(path, "rb") as f:
        f.seek(offset)
        comp = f.read(csize)
    raw = bytes(_codec().decompress(comp, decompressed_size=rsize))
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) != n:
        raise ShardFormatError(f"{path.name}: frame {k} holds {len(lines)} lines, index says {n}")
    return lines


def iter_records(path: Path) -> Iterator[dict]:
    """Stream every record of a shard, in order."""
    index = read_index(path)
    for k in range(len(index.frames)):
        for line in read_frame(path, index, k):
            yield json.loads(line)


# ---- cached single-record access (the reader panel, the packed source) ----

_lock = threading.Lock()
_indexes: OrderedDict = OrderedDict()      # path -> ShardIndex
_frames: OrderedDict = OrderedDict()       # (path, k) -> list[bytes]
_MAX_INDEXES = 64
_MAX_FRAMES = 16


def cached_index(path: Path) -> ShardIndex:
    key = str(path)
    mtime = Path(path).stat().st_mtime_ns
    with _lock:
        hit = _indexes.get(key)
        if hit is not None and hit.mtime_ns == mtime:
            _indexes.move_to_end(key)
            return hit
    index = read_index(path)
    with _lock:
        _indexes[key] = index
        _indexes.move_to_end(key)
        while len(_indexes) > _MAX_INDEXES:
            _indexes.popitem(last=False)
        for k in [k for k in _frames if k[0] == key]:
            _frames.pop(k, None)
    return index


def cached_frame(path: Path, k: int) -> list[bytes]:
    key = (str(path), k)
    with _lock:
        hit = _frames.get(key)
        if hit is not None:
            _frames.move_to_end(key)
            return hit
    lines = read_frame(path, cached_index(path), k)
    with _lock:
        _frames[key] = lines
        _frames.move_to_end(key)
        while len(_frames) > _MAX_FRAMES:
            _frames.popitem(last=False)
    return lines


def read_record(path: Path, uid: str) -> dict:
    index = cached_index(path)
    loc = index.uids.get(uid)
    if loc is None:
        raise KeyError(f"{Path(path).name}: no record {uid}")
    frame, line = loc
    return json.loads(cached_frame(path, frame)[line])


def read_fields(path: Path, uid: str, fields: Sequence[str]) -> dict:
    """Some of one record's fields, without the rest: from a Parquet part
    only those COLUMNS of its row group are decompressed — the edition facts
    of a hit (app/ask/works.py) need `title`, `date` and `extra`, and
    reading the whole row group decompressed whole books' `sections` for
    them (München, 2026-10-22: a median 967 ms a search against 25 ms
    before). A frame part has no columns: the record is read and cut."""
    if not is_parquet(path):
        record = read_record(path, uid)
        return {f: record.get(f) for f in fields}
    index = cached_index(path)
    loc = index.uids.get(uid)
    if loc is None:
        raise KeyError(f"{Path(path).name}: no record {uid}")
    group, row = loc
    pf = _parquet_file(path)
    names = set(pf.schema_arrow.names)
    cols = [f for f in fields if f in names]
    table = pf.read_row_group(group, columns=cols)
    out: dict = {}
    for f in fields:
        if f not in names:
            out[f] = None
            continue
        v = table.column(f)[row].as_py()
        out[f] = json.loads(v) if (f in NESTED_KEYS and v is not None) else v
    return out


def iter_fields(path: Path, fields: Sequence[str]) -> Iterator[dict]:
    """Stream every record's `fields`, in order, without the rest — what a
    pass over one field of every record held needs (a range facet's values,
    app/ask/ranges.py): from a Parquet part only those columns are
    decompressed, as `read_fields` does for one record; a frame part has no
    columns, so each record is read and cut."""
    if not is_parquet(path):
        for record in iter_records(path):
            yield {f: record.get(f) for f in fields}
        return
    pf = _parquet_file(path)
    names = set(pf.schema_arrow.names)
    cols = [f for f in fields if f in names]
    for g in range(pf.metadata.num_row_groups):
        table = pf.read_row_group(g, columns=cols)
        data = {c: table.column(c).to_pylist() for c in cols}
        for i in range(table.num_rows):
            out: dict = {}
            for f in fields:
                v = data[f][i] if f in data else None
                out[f] = json.loads(v) if (f in NESTED_KEYS and v is not None) else v
            yield out


def read_record_at(path: Path, frame: int, line: int) -> dict:
    return json.loads(cached_frame(path, frame)[line])


def reset_caches() -> None:
    with _lock:
        _indexes.clear()
        _frames.clear()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def dump_jsonl(path: Path, out: io.RawIOBase | io.BufferedIOBase) -> int:
    """`pack dump`: the shard as plain JSONL — what `zstd -d` would print."""
    index = read_index(path)
    n = 0
    for k in range(len(index.frames)):
        for line in read_frame(path, index, k):
            out.write(line + b"\n")
            n += 1
    return n
