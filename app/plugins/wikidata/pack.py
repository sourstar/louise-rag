"""The Wikidata dump's PACK and LABEL TABLE — the derived files one streaming
pass over `wikidata-<date>-all.json.bz2` leaves beside it (docs/notes/
wikidata-build-plan.md §2 "The pack", "Why textification is the PARSER's").

The dump is one bz2 stream of ~100 GB, ~1.5 TB of JSON, one entity a line:
nothing in it is addressable without a pass, and a line's text names other
lines' labels. So the pass writes, under `<dump dir>/.pack/<dump name>/`:

- `NNNNN.parquet`, one file per `CHUNK` entities in dump order — columns
  `index` (the line's ordinal, the locator's member), `id`, `kind`,
  `native_id` (ids.py), `modified`, `lastrevid`, `pruned` and `line` (the
  line's exact bytes, zstd by the writer; `origin_hash` is over them); row
  groups of `ROW_GROUP` rows, so a record is one small read;
- `labels.sqlite`: `label(id, lang, label)` for the languages asked and
  `mul` — of EVERY entity, pruned ones included, since a pruned article is
  still a value other entities name;
- `meta.json`: the dump's size and mtime, PACK_VERSION, the languages, the
  files complete so far — the RESUME POINT.

Resumable at the chunk (invariant 2): each chunk file is written to a tmp
and renamed, its labels committed in the same step, then `meta.json`
rewritten; a build stopped by SIGTERM or a crash resumes by re-reading the
stream (bz2 does not seek) and SKIPPING the lines of complete chunks
without parsing them — decompression alone, about a fifth of the pass.
Like PubMed's pack a cache, never a member, never exchanged; a dump of
another size, mtime or PACK_VERSION rebuilds it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from app.plugins.wikidata import dump as dump_mod, ids

PACK_VERSION = 1
#: entities per chunk file: the resume granularity and the index listing's unit
CHUNK = 100_000
#: rows per Parquet row group: one record is one row-group read
ROW_GROUP = 1_000
#: `P31` values that make an entity a scholarly article: not a record (the
#: design's corpus left them out; invariant 11 allows pruning whole entities)
PRUNED_CLASSES = frozenset({"Q13442814"})
PACK_DIRNAME = ".pack"

SCHEMA = pa.schema([
    pa.field("index", pa.int64()),
    pa.field("id", pa.string()),
    pa.field("kind", pa.string()),
    pa.field("native_id", pa.string()),
    pa.field("modified", pa.string()),
    pa.field("lastrevid", pa.int64()),
    pa.field("pruned", pa.bool_()),
    pa.field("line", pa.binary()),
])

_LABELS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS label (id TEXT NOT NULL, lang TEXT NOT NULL, label TEXT NOT NULL, "
    "PRIMARY KEY (id, lang)) WITHOUT ROWID"
)


class PackError(RuntimeError):
    """The pack cannot be built or read as the dump stands."""


def pack_dir(dump: Path) -> Path:
    return Path(dump).parent / PACK_DIRNAME / Path(dump).name


def _signature(dump: Path) -> tuple[int, int]:
    st = Path(dump).stat()
    return st.st_size, st.st_mtime_ns


def _meta_path(d: Path) -> Path:
    return d / "meta.json"


def read_meta(dump: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_meta_path(pack_dir(dump)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _meta_matches(meta: dict[str, Any] | None, dump: Path, languages: tuple[str, ...]) -> bool:
    if not meta:
        return False
    size, mtime = _signature(dump)
    return (meta.get("pack_version") == PACK_VERSION and meta.get("size") == size
            and meta.get("mtime_ns") == mtime and tuple(meta.get("languages") or ()) == tuple(languages))


def is_complete(dump: Path, languages: tuple[str, ...]) -> bool:
    """The pack covers the whole dump as it is now."""
    meta = read_meta(dump)
    return _meta_matches(meta, dump, languages) and bool(meta.get("complete"))


def chunk_path(d: Path, chunk: int) -> Path:
    return d / f"{chunk:05d}.parquet"


def is_pruned(obj: dict) -> bool:
    """An entity whose `P31` (any rank) names a pruned class."""
    for st in (obj.get("claims") or {}).get("P31") or ():
        if not isinstance(st, dict):
            continue
        dv = ((st.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(dv, dict) and dv.get("id") in PRUNED_CLASSES:
            return True
    return False


def _labels_of(obj: dict, languages: tuple[str, ...]) -> list[tuple[str, str, str]]:
    eid = obj.get("id")
    out = []
    labels = obj.get("labels") or {}
    for lang in (*languages, "mul"):
        v = labels.get(lang)
        if isinstance(v, dict) and isinstance(v.get("value"), str) and v["value"]:
            out.append((eid, lang, v["value"]))
    return out


def _row(entity: dump_mod.Entity, pruned: bool) -> dict[str, Any]:
    obj = entity.obj
    eid = str(obj.get("id") or "")
    if not ids.is_entity_id(eid):
        raise PackError(f"line {entity.index}: not an item or property id: {eid!r}")
    lastrevid = obj.get("lastrevid")
    return {"index": entity.index, "id": eid, "kind": ids.kind_of(eid), "native_id": ids.native_id_of(eid),
            "modified": str(obj.get("modified") or ""), "lastrevid": int(lastrevid) if isinstance(lastrevid, int) else -1,
            "pruned": pruned, "line": entity.raw}


def build(dump: Path, *, languages: tuple[str, ...] = ("en",), log: Callable[[str], None] | None = None,
          stop: Callable[[], bool] | None = None, chunk: int = CHUNK) -> dict[str, Any]:
    """One streaming pass, or its continuation. Returns the meta written:
    `{complete, chunks, entities, pruned, ...}`; `complete` False when `stop`
    ended the pass at a chunk boundary (resumable, nothing lost)."""
    dump = Path(dump)
    say = log or (lambda m: None)
    d = pack_dir(dump)
    d.mkdir(parents=True, exist_ok=True)
    size, mtime = _signature(dump)
    meta = read_meta(dump)
    if not _meta_matches(meta, dump, languages):
        # another dump, another version or other languages: start over
        for p in d.glob("*.parquet"):
            p.unlink()
        for p in (d / "labels.sqlite", d / "labels.sqlite-wal", d / "labels.sqlite-shm"):
            p.unlink(missing_ok=True)
        meta = {"pack_version": PACK_VERSION, "size": size, "mtime_ns": mtime, "languages": list(languages),
                "chunk": chunk, "chunks": 0, "entities": 0, "pruned": 0, "complete": False, "dump": dump.name}
        _meta_path(d).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    if meta.get("complete"):
        return meta
    if meta.get("chunk") != chunk:
        raise PackError(f"the pack was started with chunk {meta.get('chunk')}; resume with the same")
    done_chunks = int(meta["chunks"])
    skip = done_chunks * chunk
    con = sqlite3.connect(d / "labels.sqlite", isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(_LABELS_SCHEMA)
    rows: list[dict[str, Any]] = []
    labels: list[tuple[str, str, str]] = []
    pruned_n = int(meta["pruned"])
    t0 = time.monotonic()

    def flush_chunk(chunk_no: int, complete: bool) -> None:
        nonlocal rows, labels, done_chunks
        if rows:
            tmp = chunk_path(d, chunk_no).with_name(f"{chunk_no:05d}.parquet.tmp")
            table = pa.Table.from_pylist(rows, schema=SCHEMA)
            pq.write_table(table, tmp, compression="zstd", compression_level=3, row_group_size=ROW_GROUP)
            con.execute("BEGIN IMMEDIATE")
            con.executemany("INSERT OR REPLACE INTO label (id, lang, label) VALUES (?, ?, ?)", labels)
            con.execute("COMMIT")
            os.replace(tmp, chunk_path(d, chunk_no))
            done_chunks = chunk_no + 1
            meta.update(chunks=done_chunks, entities=done_chunks * chunk if not complete else meta["entities"])
        meta.update(pruned=pruned_n, complete=complete)
        tmp_meta = _meta_path(d).with_name("meta.json.tmp")
        tmp_meta.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, _meta_path(d))
        rows, labels = [], []

    say(f"[wikidata] pack: {dump.name}: {'resuming after' if skip else 'starting;'} {skip} entities"
        if skip else f"[wikidata] pack: {dump.name}: starting")
    with dump_mod.open_dump(dump) as stream:
        result = dump_mod.ReadResult()
        count = 0
        chunk_no = done_chunks
        for entity in dump_mod.iter_entities(stream, result=result, skip=skip):
            obj = entity.obj
            pruned = is_pruned(obj)
            if pruned:
                pruned_n += 1
            rows.append(_row(entity, pruned))
            labels.extend(_labels_of(obj, languages))
            count += 1
            if len(rows) >= chunk:
                flush_chunk(chunk_no, complete=False)
                chunk_no += 1
                say(f"[wikidata] pack: {dump.name}: {skip + count} entities, {chunk_no} chunk file(s), "
                    f"{time.monotonic() - t0:.0f} s")
                if stop is not None and stop():
                    con.close()
                    return meta
        # the tail: the last, partial chunk — complete only when the stream ended whole
        if not result.complete:
            if rows:
                say(f"[wikidata] pack: {dump.name}: the stream ended {result.truncated_at or 'unexpectedly'}: "
                    f"{len(rows)} entities of an incomplete chunk not written")
            con.close()
            raise PackError(f"{dump.name}: the dump ended before its closing bracket (truncated download?)")
        meta["entities"] = skip + count
        flush_chunk(chunk_no, complete=True)
    con.close()
    say(f"[wikidata] pack: {dump.name}: complete — {meta['entities']} entities, {meta['pruned']} pruned, "
        f"{meta['chunks']} chunk file(s), {time.monotonic() - t0:.0f} s")
    return meta


# ---- reading ---------------------------------------------------------------------------------

_files_lock = threading.Lock()
_groups: OrderedDict = OrderedDict()                 # (chunk path, row group) -> {index: line}
_MAX_GROUPS = 8


def chunk_files(dump: Path) -> list[Path]:
    d = pack_dir(dump)
    meta = read_meta(dump) or {}
    return [chunk_path(d, i) for i in range(int(meta.get("chunks") or 0))]


def index_columns(path: Path) -> pa.Table:
    """A chunk file's index columns — everything but the lines."""
    return pq.read_table(path, columns=["index", "id", "kind", "native_id", "modified", "lastrevid", "pruned"])


def line(dump: Path, index: int) -> bytes:
    """Entity `index`'s own line bytes, from its row group (cached)."""
    meta = read_meta(dump) or {}
    chunk = int(meta.get("chunk") or CHUNK)
    path = chunk_path(pack_dir(dump), index // chunk)
    group = (index % chunk) // ROW_GROUP
    key = (str(path), group)
    with _files_lock:
        got = _groups.get(key)
        if got is not None:
            _groups.move_to_end(key)
    if got is None:
        pf = pq.ParquetFile(path)
        if group >= pf.num_row_groups:
            raise PackError(f"{path.name}: no row group {group} for entity {index}")
        t = pf.read_row_group(group, columns=["index", "line"])
        got = dict(zip(t.column("index").to_pylist(), t.column("line").to_pylist()))
        with _files_lock:
            _groups[key] = got
            while len(_groups) > _MAX_GROUPS:
                _groups.popitem(last=False)
    try:
        return got[index]
    except KeyError:
        raise PackError(f"{path.name}: entity {index} not in row group {group}") from None


class LabelTable:
    """`labels(id, lang)` over the pack's sqlite, one connection per thread,
    a bounded cache in front: the textifier asks for every referenced id."""

    def __init__(self, dump: Path, cache: int = 200_000):
        self.path = pack_dir(dump) / "labels.sqlite"
        self._local = threading.local()
        self._cache: OrderedDict = OrderedDict()
        self._max = cache
        self._lock = threading.Lock()

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._local.con = con
        return con

    def __call__(self, entity_id: str, lang: str) -> Optional[str]:
        key = (entity_id, lang)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        row = self._con().execute("SELECT label FROM label WHERE id=? AND lang=?", key).fetchone()
        got = row[0] if row else None
        with self._lock:
            self._cache[key] = got
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return got


_tables: dict[str, LabelTable] = {}


def labels(dump: Path) -> LabelTable:
    key = str(pack_dir(dump))
    with _files_lock:
        t = _tables.get(key)
        if t is None:
            t = _tables[key] = LabelTable(dump)
    return t


def reset_caches() -> None:
    with _files_lock:
        _groups.clear()
        _tables.clear()
