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

ON EVERY CORE (2026-10-20, the operator: "squeeze everything from the
cpu"). The serial pass ran at ~29 MB/s of JSON on one core of twelve —
measured on the node: bz2 decompresses at 76 MB/s, `json.loads` parses at
48 MB/s, one after the other in one thread. With `config.PARSE_PROCESSES`
> 1 (the parse pool's size; 1 in the test suite) the pass is split: THIS
process decompresses and cuts the lines (`dump.iter_lines`, no parse), a
pool of spawned children (never forked: the worker holds SQLite
connections and threads; nice 10, as the parse pool's) each parses ONE
WHOLE CHUNK and writes its Parquet to a tmp file, and this process commits
the chunks strictly in order — labels, rename, meta — so the files, the
labels and the resume point are the serial pass's, byte for byte. A child
that fails or does not answer within `CHUNK_TIMEOUT_S` ends the pass at the
last committed chunk (resumable, nothing lost; deferral, never an error)
and the rest of this process's life runs serial. Decompression itself is
one stream: `lbzip2` on the PATH decompresses it on several cores (the
same bytes), else the standard library's bz2.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from app import config
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
#: how long the pass waits for one child's chunk before it stops (resumable)
CHUNK_TIMEOUT_S = 1800.0
#: a failed pool turns the rest of this process's passes serial
_parallel_failed = False

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


def _write_chunk(rows: list[dict[str, Any]], tmp: Path) -> None:
    table = pa.Table.from_pylist(rows, schema=SCHEMA)
    pq.write_table(table, tmp, compression="zstd", compression_level=3, row_group_size=ROW_GROUP)


def _chunk_job(lines: list[bytes], start: int, languages: tuple[str, ...],
               tmp: str) -> tuple[int, list[tuple[str, str, str]]]:
    """In a child: one chunk's lines parsed exactly as the serial pass parses
    them, its Parquet written to `tmp`. Returns (pruned, labels)."""
    rows: list[dict[str, Any]] = []
    labels: list[tuple[str, str, str]] = []
    pruned_n = 0
    for i, raw in enumerate(lines):
        try:
            obj = json.loads(raw)
        except ValueError as e:
            raise dump_mod.DumpError(f"entity {start + i}: not JSON ({e})") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            raise dump_mod.DumpError(f"entity {start + i}: an entity is an object with an `id`")
        pruned = is_pruned(obj)
        pruned_n += pruned
        rows.append(_row(dump_mod.Entity(start + i, raw, obj), pruned))
        labels.extend(_labels_of(obj, languages))
    _write_chunk(rows, Path(tmp))
    return pruned_n, labels


def _child_nice() -> None:
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def processes() -> int:
    """The pass's worker processes: the parse pool's size (1 = serial)."""
    return 1 if _parallel_failed else max(1, int(getattr(config, "PARSE_PROCESSES", 1)))


@contextmanager
def _decompressed(dump: Path, threads: int) -> Iterator[Any]:
    """The dump's bytes: through `lbzip2` on several cores when it is on the
    PATH (a non-zero exit is a stream it could not decode: truncated), else
    `dump.open_dump`."""
    exe = shutil.which("lbzip2") if str(dump).endswith(".bz2") else None
    if exe is None:
        with dump_mod.open_dump(dump) as f:
            yield f
        return
    proc = subprocess.Popen([exe, "-d", "-c", "-n", str(max(1, threads)), str(dump)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20)
    try:
        yield proc.stdout
    finally:
        proc.stdout.close()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def build(dump: Path, *, languages: tuple[str, ...] = ("en",), log: Callable[[str], None] | None = None,
          stop: Callable[[], bool] | None = None, chunk: int = CHUNK,
          workers: int | None = None) -> dict[str, Any]:
    """One streaming pass, or its continuation. Returns the meta written:
    `{complete, chunks, entities, pruned, ...}`; `complete` False when `stop`
    ended the pass at a chunk boundary (resumable, nothing lost). `workers`:
    the pass's processes (default `processes()`); 1 is the serial pass."""
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
    n = processes() if workers is None else max(1, int(workers))
    if n > 1:
        try:
            return _build_parallel(dump, d, meta, con, languages=languages, say=say, stop=stop,
                                   chunk=chunk, workers=n, t0=t0)
        finally:
            con.close()
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


def _tmp_path(d: Path, chunk_no: int, token: str) -> Path:
    """A parallel pass's own tmp name: a child left running by a timed-out
    pass can never write into a later pass's file."""
    return chunk_path(d, chunk_no).with_name(f"{chunk_no:05d}.parquet.{token}.tmp")


def _build_parallel(dump: Path, d: Path, meta: dict[str, Any], con: sqlite3.Connection, *,
                    languages: tuple[str, ...], say: Callable[[str], None],
                    stop: Callable[[], bool] | None, chunk: int, workers: int, t0: float) -> dict[str, Any]:
    """`build`'s pass on `workers` processes (module docstring): the same
    files, labels and meta as the serial pass, committed in chunk order."""
    global _parallel_failed
    from concurrent.futures.process import BrokenProcessPool

    done = int(meta["chunks"])
    skip = done * chunk
    state = {"next": done, "pruned": int(meta["pruned"])}
    pending: dict[int, Future] = {}
    token = f"{os.getpid()}-{time.monotonic_ns()}"
    for stale in d.glob("*.parquet.*.tmp"):          # an earlier parallel pass's, never committed
        stale.unlink(missing_ok=True)
    ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_child_nice)
    hung = False

    def write_meta() -> None:
        tmp_meta = _meta_path(d).with_name("meta.json.tmp")
        tmp_meta.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, _meta_path(d))

    def commit(chunk_no: int, fut: Future, *, final: bool) -> None:
        pruned, labels = fut.result(timeout=CHUNK_TIMEOUT_S)
        con.execute("BEGIN IMMEDIATE")
        con.executemany("INSERT OR REPLACE INTO label (id, lang, label) VALUES (?, ?, ?)", labels)
        con.execute("COMMIT")
        os.replace(_tmp_path(d, chunk_no, token), chunk_path(d, chunk_no))
        state["pruned"] += pruned
        state["next"] = chunk_no + 1
        if final:
            meta.update(chunks=state["next"], pruned=state["pruned"], complete=True)
        else:
            meta.update(chunks=state["next"], entities=state["next"] * chunk, pruned=state["pruned"],
                        complete=False)
        write_meta()

    def submit(chunk_no: int, lines: list[bytes], start: int) -> Future:
        return ex.submit(_chunk_job, lines, start, languages, str(_tmp_path(d, chunk_no, token)))

    try:
        result = dump_mod.ReadResult()
        batch: list[bytes] = []
        batch_start = skip
        chunk_no = done
        with _decompressed(dump, workers) as stream:
            for _index, raw in dump_mod.iter_lines(stream, result=result, skip=skip):
                batch.append(raw)
                if len(batch) < chunk:
                    continue
                pending[chunk_no] = submit(chunk_no, batch, batch_start)
                batch_start += len(batch)
                batch = []
                chunk_no += 1
                # commit in order what is done; wait for the oldest when every child is busy
                while state["next"] in pending and (pending[state["next"]].done() or len(pending) >= workers):
                    c = state["next"]
                    commit(c, pending.pop(c), final=False)
                    say(f"[wikidata] pack: {dump.name}: {state['next'] * chunk} entities, "
                        f"{state['next']} chunk file(s), {time.monotonic() - t0:.0f} s")
                    if stop is not None and stop():
                        return meta
        while pending:
            c = state["next"]
            commit(c, pending.pop(c), final=False)
            say(f"[wikidata] pack: {dump.name}: {state['next'] * chunk} entities, "
                f"{state['next']} chunk file(s), {time.monotonic() - t0:.0f} s")
        if not result.complete:
            if batch:
                say(f"[wikidata] pack: {dump.name}: the stream ended {result.truncated_at or 'unexpectedly'}: "
                    f"{len(batch)} entities of an incomplete chunk not written")
            raise PackError(f"{dump.name}: the dump ended before its closing bracket (truncated download?)")
        meta["entities"] = batch_start + len(batch)
        if batch:
            commit(chunk_no, submit(chunk_no, batch, batch_start), final=True)
        else:
            meta.update(pruned=state["pruned"], complete=True)
            write_meta()
        say(f"[wikidata] pack: {dump.name}: complete — {meta['entities']} entities, {meta['pruned']} pruned, "
            f"{meta['chunks']} chunk file(s), {time.monotonic() - t0:.0f} s, {workers} processes")
        return meta
    except (BrokenProcessPool, TimeoutError) as e:
        _parallel_failed = True
        hung = isinstance(e, TimeoutError)
        say(f"[wikidata] pack: {dump.name}: the pool failed ({type(e).__name__}: {e}); stopped after "
            f"{meta['chunks']} chunk(s), resumable; the next pass of this process is serial")
        return meta
    finally:
        # the running children finish (one chunk, bounded) unless one hung
        ex.shutdown(wait=not hung, cancel_futures=True)
        for c in pending:
            _tmp_path(d, c, token).unlink(missing_ok=True)


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
