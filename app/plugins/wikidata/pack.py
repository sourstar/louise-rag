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
  still a value other entities name; `language(id, code)`, the Wikimedia
  language code (P424) of every language item; and `name(id, lang,
  label)`, the name by names.py's order of every entity with no English
  and no `mul` label (operator, 2026-10-24: an id is meaningless to a
  reader) — written by a pass over the pack's own lines AFTER the chunks,
  since a name may need the code of a language item later in the dump;
- the CLASS TREE and the FAMILIES (version 3, 2026-10-24, operator: "Go
  with A"; families.py): `subclass(id, parent)`, every item's P279 links,
  and `chunk_top`, each chunk's largest item number — a pass over the
  pack's own lines after the names; from them `families-<h>.json`, the
  family map this dump gives (the map a collection created on this dump
  pins, `creation_map`), and for a map, `families-<h>.u8`: one byte an
  entity, its family code in dump order — what the records build slices by
  without parsing 75.9 M documents (`family_code_at`) — with
  `families-<h>.report.json`, each family's size and the classes that fill
  it, read before `create` (the family list is fixed for the collection's
  life);
- `meta.json`: the dump's size and mtime, PACK_VERSION, the languages, the
  files complete so far — the RESUME POINT.

Resumable at the chunk (invariant 2): each chunk file is written to a tmp
and renamed, its labels committed in the same step, then `meta.json`
rewritten; a build stopped by SIGTERM or a crash resumes by re-reading the
stream (bz2 does not seek) and SKIPPING the lines of complete chunks
without parsing them — decompression alone, about a fifth of the pass.
Like PubMed's pack a cache, never a member, never exchanged; a dump of
another size, mtime or PACK_VERSION rebuilds it — but a complete version-1
pack is UPGRADED in place (RULE 1, named): version 2 adds the `language`
and `name` tables and changes nothing else, so their rows come from two
passes over the pack's own lines (`_finish`, each resumable at a chunk)
instead of a new pass over the bz2; the chunk files and the `label` rows
are those a version-2 pass writes, byte for byte
(tests/test_plugins_wikidata_names.py). A complete version-2 pack is
upgraded to 3 the same way: the class tree and the families, from its own
lines. Parser 2 reads a pack of version 2 or later whose names are done
(`ready`): anything else defers; the families do not hold it up.

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

import hashlib
import json
import multiprocessing
import os
import re
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

from app import config, cpu_limit, parallel
from app.plugins.wikidata import dump as dump_mod, families, ids, names

#: 2 (2026-10-24): the `name` table; 3 (2026-10-24): the class tree and the
#: families (families.py); a complete 1 or 2 is upgraded in place
PACK_VERSION = 3
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
_NAMES_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS name (id TEXT PRIMARY KEY, lang TEXT NOT NULL, label TEXT NOT NULL) WITHOUT ROWID"
)
_LANGUAGES_SCHEMA = "CREATE TABLE IF NOT EXISTS language (id TEXT PRIMARY KEY, code TEXT NOT NULL) WITHOUT ROWID"
#: version 3: every item's P279 links (item numbers), each chunk's largest
#: item number, and per family map each chunk's summary for the report
_SUBCLASS_SCHEMA = ("CREATE TABLE IF NOT EXISTS subclass (id INTEGER NOT NULL, parent INTEGER NOT NULL, "
                    "PRIMARY KEY (id, parent)) WITHOUT ROWID")
_CHUNK_TOP_SCHEMA = "CREATE TABLE IF NOT EXISTS chunk_top (chunk INTEGER PRIMARY KEY, max_item INTEGER NOT NULL)"
_FAMILY_CHUNK_SCHEMA = ("CREATE TABLE IF NOT EXISTS family_chunk (map TEXT NOT NULL, chunk INTEGER NOT NULL, "
                        "summary TEXT NOT NULL, PRIMARY KEY (map, chunk)) WITHOUT ROWID")
#: the labels whose absence gives an entity a `name` row: the plugin's
#: fallback language and `mul` — what every text's chain ends with
NAMED_UNLESS = ("en", "mul")


class PackError(RuntimeError):
    """The pack cannot be built or read as the dump stands."""


def _beside(dump: Path) -> Path:
    return Path(dump).parent / PACK_DIRNAME / Path(dump).name


def _target(dump: Path) -> Path:
    """Where the dump's pack belongs: on the node's fast drive when one is
    named (`derived.place`, operator 2026-10-20 — the label table is read at
    random, ~15 lookups a document, and from tank's spinning disks that was
    the whole cost of a parse), else beside the dump as before. The place is
    kept per process (`derived.place_kept`: its realpath was ~7 % of a
    records-build child); WHICH of the two holds the pack is still asked
    every time (`pack_dir`, two stats)."""
    from app import derived
    place = derived.place_kept("wikidata-pack", dump)
    return _beside(dump) if place is None else place / Path(dump).name


def pack_dir(dump: Path) -> Path:
    """Where the dump's pack IS: its place (`_target`) once a pack is there,
    or — while the one built beside the dump has not been carried yet
    (`adopt_beside`: ~100 GB, many minutes, renamed whole at the end) —
    beside the dump. A reader never meets a place still being filled: on
    2026-10-20 the fts stage read the fast drive while the copy ran, and
    two documents were marked "not in its pack" ("Failed to open local
    file") before wikidata was paused. Two stats a call."""
    target, old = _target(dump), _beside(dump)
    if target != old and not _meta_path(target).is_file() and _meta_path(old).is_file():
        return old
    return target


def adopt_beside(dump: Path, *, log: Callable[[str], None] | None = None) -> bool:
    """A pack built beside the dump before the fast drive was named, carried
    there once (`derived.adopt`: whole or not at all; the original left for
    the operator), so the pass resumes from its last chunk and nothing is
    rebuilt (RULE 1). A directory there without a meta.json holds no pack
    (a pass that started before the copy finished) and is replaced."""
    import shutil
    from app import derived
    target, old = _target(dump), _beside(dump)
    if target == old or _meta_path(target).is_file() or not _meta_path(old).is_file():
        return False
    if target.exists():
        shutil.rmtree(target)
    t0 = time.monotonic()
    done = derived.adopt(old, target)
    if done and log is not None:
        log(f"[wikidata] pack: {Path(dump).name}: adopted into the fast drive {target} from {old} "
            f"in {time.monotonic() - t0:.0f} s (left in place there)")
    return done


def _signature(dump: Path) -> tuple[int, int]:
    st = Path(dump).stat()
    return st.st_size, st.st_mtime_ns


def _meta_path(d: Path) -> Path:
    return d / "meta.json"


#: meta.json parsed, by its path, its mtime and its size: read once while it
#: stands, not once per document's line (a records build asks it ~75.9 M times)
_metas: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def read_meta(dump: Path) -> dict[str, Any] | None:
    path = _meta_path(pack_dir(dump))
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
        got = _metas.get(str(path))
        if got is not None and got[0] == stamp:
            return dict(got[1])
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _metas[str(path)] = (stamp, meta)
    return dict(meta)


def _meta_matches(meta: dict[str, Any] | None, dump: Path, languages: tuple[str, ...]) -> bool:
    if not meta:
        return False
    size, mtime = _signature(dump)
    return (meta.get("pack_version") == PACK_VERSION and meta.get("size") == size
            and meta.get("mtime_ns") == mtime and tuple(meta.get("languages") or ()) == tuple(languages))


def is_complete(dump: Path, languages: tuple[str, ...]) -> bool:
    """The pack covers the whole dump as it is now, its names and its
    families included."""
    adopt_beside(dump)
    meta = read_meta(dump)
    return (_meta_matches(meta, dump, languages) and bool(meta.get("complete")) and bool(meta.get("names_done"))
            and bool(meta.get("families_done")))


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


def _name_of(obj: dict, codes: dict[str, str]) -> tuple[str, str, str] | None:
    """The `name` row of an entity with no English and no `mul` label."""
    if names.has_label(obj, NAMED_UNLESS):
        return None
    got = names.display_name(obj, codes)
    return (str(obj.get("id")), got[0], got[1]) if got else None


def _language_of(obj: dict) -> tuple[str, str] | None:
    """The `language` row of a language item: its Wikimedia code (P424)."""
    code = names.language_code_of(obj)
    return (str(obj.get("id")), code) if code else None


def _row(entity: dump_mod.Entity, pruned: bool) -> dict[str, Any]:
    obj = entity.obj
    eid = str(obj.get("id") or "")
    if not ids.is_entity_id(eid):
        raise PackError(f"line {entity.index}: not an item or property id: {eid!r}")
    lastrevid = obj.get("lastrevid")
    return {"index": entity.index, "id": eid, "kind": ids.kind_of(eid), "native_id": ids.native_id_of(eid),
            "modified": str(obj.get("modified") or ""), "lastrevid": int(lastrevid) if isinstance(lastrevid, int) else -1,
            "pruned": pruned, "line": entity.raw}


def _write_chunk(table: pa.Table, tmp: Path) -> None:
    pq.write_table(table, tmp, compression="zstd", compression_level=3, row_group_size=ROW_GROUP)


#: Arrow's binary chunk limit (kBinaryMemoryLimit): `from_pylist` starts a new
#: chunk at the value that would take the current one past it
_BINARY_LIMIT = 2**31 - 2


def _split_points(lengths: Iterable[int], limit: int = _BINARY_LIMIT) -> list[int]:
    """Where `from_pylist` cuts a binary column of these value lengths."""
    out: list[int] = []
    cum = 0
    for i, n in enumerate(lengths):
        if cum + n > limit:
            out.append(i)
            cum = 0
        cum += n
    return out


def _as_serial_table(tables: list[pa.Table], limit: int = _BINARY_LIMIT) -> pa.Table:
    """A chunk's slices as the table the serial pass builds with ONE
    `from_pylist`: one chunk per column, and `line` cut exactly where
    from_pylist cuts it. The Parquet writer's pages follow the chunks: the
    same content cut elsewhere is other bytes — measured on the node,
    chunk 00000 (3.5 GB of JSON, cut at row 61,707): slices concatenated
    gave sha256 743458c0…, this gives the serial pass's 655c8fe2…."""
    import pyarrow.compute as pc

    t = pa.concat_tables(tables)
    cols = []
    for name in t.column_names:
        c = t.column(name)
        if name != "line":
            cols.append(c.combine_chunks())
            continue
        lengths = pc.binary_length(c).to_pylist() if len(c) else []
        bounds = [0, *_split_points(lengths, limit), len(c)]
        cols.append(pa.chunked_array([c.slice(a, b - a).combine_chunks() for a, b in zip(bounds, bounds[1:])],
                                     type=pa.binary()))
    return pa.Table.from_arrays(cols, schema=SCHEMA)


def _write_slices(tables: list[pa.Table], tmp: Path) -> None:
    _write_chunk(_as_serial_table(tables), tmp)


#: entities per child task: a chunk is cut into these so a task is ~1/10 of
#: a chunk's bytes (a chunk of 100,000 is 1.0–1.3 GB of JSON on the node,
#: 2026-10-20 — eleven children each holding a whole one would need ~30 GB)
SUB = 5_000


def _rows_job(lines: list[bytes], start: int,
              languages: tuple[str, ...]) -> tuple[int, list[tuple[str, str, str]], list[tuple[str, str]],
                                                   pa.Table]:
    """In a child: a slice of a chunk's lines parsed exactly as the serial
    pass parses them. Returns (pruned, labels, language codes, the slice's
    rows as a table of SCHEMA) — the parent writes the chunk's file from its
    slices."""
    rows: list[dict[str, Any]] = []
    labels: list[tuple[str, str, str]] = []
    codes: list[tuple[str, str]] = []
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
        code = _language_of(obj)
        if code is not None:
            codes.append(code)
    return pruned_n, labels, codes, pa.Table.from_pylist(rows, schema=SCHEMA)


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
    adopt_beside(dump, log=say)
    d = pack_dir(dump)
    d.mkdir(parents=True, exist_ok=True)
    size, mtime = _signature(dump)
    meta = read_meta(dump)
    n_workers = processes() if workers is None else max(1, int(workers))
    if not _meta_matches(meta, dump, languages) and _upgradable(meta, dump, languages):
        return _finish(dump, d, meta, say=say, stop=stop, workers=n_workers, upgrade=True)
    newer = (meta or {}).get("pack_version")
    if isinstance(newer, int) and newer > PACK_VERSION:
        # A NEWER CODE'S PACK is never started over (München, 2026-10-24: the
        # deployed version-1 worker would have unlinked the 100 GB a version-2
        # upgrade had just written, had its scan reached the dump): refused,
        # left as it is, for the code that wrote it
        raise PackError(f"{dump.name}: its pack is version {newer}, written by newer code than this "
                        f"(version {PACK_VERSION}); left untouched")
    if not _meta_matches(meta, dump, languages):
        # another dump, an older version or other languages: start over
        for p in d.glob("*.parquet"):
            p.unlink()
        for p in (d / "labels.sqlite", d / "labels.sqlite-wal", d / "labels.sqlite-shm"):
            p.unlink(missing_ok=True)
        meta = {"pack_version": PACK_VERSION, "size": size, "mtime_ns": mtime, "languages": list(languages),
                "chunk": chunk, "chunks": 0, "entities": 0, "pruned": 0, "complete": False, "dump": dump.name}
        _meta_path(d).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    if meta.get("complete"):
        return meta if meta.get("families_done") else _finish(dump, d, meta, say=say, stop=stop, workers=n_workers)
    if meta.get("chunk") != chunk:
        raise PackError(f"the pack was started with chunk {meta.get('chunk')}; resume with the same")
    done_chunks = int(meta["chunks"])
    skip = done_chunks * chunk
    con = sqlite3.connect(d / "labels.sqlite", isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(_LABELS_SCHEMA)
    con.execute(_LANGUAGES_SCHEMA)
    rows: list[dict[str, Any]] = []
    labels: list[tuple[str, str, str]] = []
    codes: list[tuple[str, str]] = []
    pruned_n = int(meta["pruned"])
    t0 = time.monotonic()

    def flush_chunk(chunk_no: int, complete: bool) -> None:
        nonlocal rows, labels, codes, done_chunks
        if rows:
            tmp = chunk_path(d, chunk_no).with_name(f"{chunk_no:05d}.parquet.tmp")
            table = pa.Table.from_pylist(rows, schema=SCHEMA)
            pq.write_table(table, tmp, compression="zstd", compression_level=3, row_group_size=ROW_GROUP)
            con.execute("BEGIN IMMEDIATE")
            con.executemany("INSERT OR REPLACE INTO label (id, lang, label) VALUES (?, ?, ?)", labels)
            con.executemany("INSERT OR REPLACE INTO language (id, code) VALUES (?, ?)", codes)
            con.execute("COMMIT")
            os.replace(tmp, chunk_path(d, chunk_no))
            done_chunks = chunk_no + 1
            meta.update(chunks=done_chunks, entities=done_chunks * chunk if not complete else meta["entities"])
        meta.update(pruned=pruned_n, complete=complete)
        tmp_meta = _meta_path(d).with_name("meta.json.tmp")
        tmp_meta.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, _meta_path(d))
        rows, labels, codes = [], [], []

    say(f"[wikidata] pack: {dump.name}: {'resuming after' if skip else 'starting;'} {skip} entities"
        if skip else f"[wikidata] pack: {dump.name}: starting")
    if n_workers > 1:
        try:
            result = _build_parallel(dump, d, meta, con, languages=languages, say=say, stop=stop,
                                     chunk=chunk, workers=n_workers, t0=t0)
        finally:
            con.close()
        return _finish(dump, d, result, say=say, stop=stop, workers=n_workers) if result.get("complete") \
            else result
    watch = cpu_limit.Watch(lambda m: say(f"[wikidata] pack: {dump.name}: {m}"))
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
            code = _language_of(obj)
            if code is not None:
                codes.append(code)
            count += 1
            if len(rows) >= chunk:
                flush_chunk(chunk_no, complete=False)
                chunk_no += 1
                say(f"[wikidata] pack: {dump.name}: {skip + count} entities, {chunk_no} chunk file(s), "
                    f"{time.monotonic() - t0:.0f} s")
                if stop is not None and stop():
                    con.close()
                    return meta
                # the node profile's pause (app/cpu_limit.py), at this chunk's commit
                if watch.limit().cores == 0 and not cpu_limit.wait_while_paused(stop, watch):
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
    return _finish(dump, d, meta, say=say, stop=stop, workers=n_workers)


# ---- after the chunks: the codes and the names, from the pack's own lines --------------------

#: an English or mul label, as the dump writes one: `"en":{"language":"en","value":"<not empty>`
_NAMED_LABEL = re.compile(rb'"en":\{"language":"en","value":"[^"]|"mul":\{"language":"mul","value":"[^"]')


def _upgradable(meta: dict[str, Any] | None, dump: Path, languages: tuple[str, ...]) -> bool:
    """A COMPLETE version-1 or version-2 pack of this very dump and
    languages: version 2 only adds the `language` and `name` tables, version
    3 the class tree and the families, so it is upgraded, not rebuilt."""
    if not meta or meta.get("pack_version") not in (1, 2) or not meta.get("complete"):
        return False
    size, mtime = _signature(dump)
    return (meta.get("size") == size and meta.get("mtime_ns") == mtime
            and tuple(meta.get("languages") or ()) == tuple(languages))


def maybe_unnamed(line: bytes) -> bool:
    """False only when the line certainly has an English or `mul` label, so
    no `name` row: the label map runs from `"labels":` to `"descriptions":`
    (a JSON string cannot hold an unescaped quote, so neither key occurs in
    a value, and the dump writes the labels first). Anything else is parsed."""
    a = line.find(b'"labels":')
    b = line.find(b'"descriptions":', a) if a >= 0 else -1
    return a < 0 or b < 0 or _NAMED_LABEL.search(line, a, b) is None


def _lines(path: str) -> Iterator[tuple[int, bytes]]:
    pf = pq.ParquetFile(path)
    for g in range(pf.num_row_groups):
        t = pf.read_row_group(g, columns=["index", "line"])
        yield from zip(t.column("index").to_pylist(), t.column("line").to_pylist())


def _parsed(path: str, index: int, raw: bytes) -> dict:
    try:
        return json.loads(raw)
    except ValueError as e:
        raise PackError(f"{Path(path).name}: entity {index} is not JSON ({e})") from None


def _languages_job(path: str) -> list[tuple[str, str]]:
    """One chunk file's `language` rows (a version-1 pack's upgrade): only
    the lines that name P424 are parsed."""
    out = []
    for index, raw in _lines(path):
        if b'"P424":[' in raw:
            row = _language_of(_parsed(path, index, raw))
            if row is not None:
                out.append(row)
    return out


_CODES: dict[str, str] = {}


def _set_codes(codes: dict[str, str]) -> None:
    _CODES.clear()
    _CODES.update(codes)


def _names_init(codes: dict[str, str]) -> None:
    """A names child: the language codes, once; nice as every pack child."""
    _child_nice()
    _set_codes(codes)


def _names_job(path: str) -> list[tuple[str, str, str]]:
    """One chunk file's `name` rows: every line read, the unnamed ones parsed."""
    out = []
    for index, raw in _lines(path):
        if maybe_unnamed(raw):
            row = _name_of(_parsed(path, index, raw), _CODES)
            if row is not None:
                out.append(row)
    return out


# ---- the class tree and the families (version 3) ---------------------------------------------

def _subclass_job(path: str) -> tuple[list[tuple[int, int]], int]:
    """One chunk file's P279 links between items (child, parent; the
    non-deprecated values, families.item_values) and its largest item
    number — of every entity, pruned ones included: the class tree is the
    dump's. Only the lines that name P279 anywhere are parsed — a line
    without the bytes `"P279"` has no such statement, however it is spaced."""
    ids_ = pq.read_table(path, columns=["id"]).column("id").to_pylist()
    top = max((q for q in map(families.number, ids_) if q is not None), default=0)
    edges: list[tuple[int, int]] = []
    for index, raw in _lines(path):
        if b'"P279"' not in raw:
            continue
        obj = _parsed(path, index, raw)
        q = families.number(obj.get("id"))
        if q is None:
            continue
        for parent in families.item_values((obj.get("claims") or {}).get("P279")):
            edges.append((q, parent))
    return edges, top


def _write_subclass(con: sqlite3.Connection, k: int, result: tuple[list[tuple[int, int]], int]) -> None:
    edges, top = result
    con.executemany("INSERT OR REPLACE INTO subclass (id, parent) VALUES (?, ?)", edges)
    con.execute("INSERT OR REPLACE INTO chunk_top (chunk, max_item) VALUES (?, ?)", (k, top))


#: a codes child's map and the walk of the classes newer than it
_FMAP: dict[str, Any] = {}
#: per chunk and family, the classes kept for the report
REPORT_CLASSES = 200


def _set_fmap(raw: bytes, walked: dict[int, int]) -> None:
    _FMAP.clear()
    _FMAP.update(map=families.load_map(raw), walked=walked)


def _codes_init(raw: bytes, walked: dict[int, int]) -> None:
    """A codes child: the map and the walk, once; nice as every pack child."""
    _child_nice()
    _set_fmap(raw, walked)


def _codes_job(path: str) -> tuple[int, bytes, dict[str, Any]]:
    """One chunk file's family codes, one byte an entity in dump order, and
    its summary (records and pruned per family, the classes that decided);
    a line without the bytes `"P31"` and `"P1435"` anywhere has neither
    statement, and is not parsed."""
    from collections import Counter
    fmap, walked = _FMAP["map"], _FMAP["walked"]
    pf = pq.ParquetFile(path)
    out = bytearray()
    start = None
    records: Counter = Counter()
    pruned_n: Counter = Counter()
    deciding: Counter = Counter()
    walked_n = 0
    for g in range(pf.num_row_groups):
        t = pf.read_row_group(g, columns=["index", "id", "pruned", "line"])
        for index, eid, pruned, raw in zip(t.column("index").to_pylist(), t.column("id").to_pylist(),
                                           t.column("pruned").to_pylist(), t.column("line").to_pylist()):
            if start is None:
                start = index
            if eid[:1] != "Q" or (b'"P31"' not in raw and b'"P1435"' not in raw):
                code, by = families.family_and_class({"id": eid}, fmap)
            else:
                code, by = families.family_and_class(_parsed(path, index, raw), fmap, walked)
            out.append(code)
            (pruned_n if pruned else records)[code] += 1
            if by is not None:
                deciding[(code, by)] += 1
                walked_n += by > fmap.max_item
    top: dict[str, list[list[int]]] = {}
    for (code, cls), n in deciding.most_common():
        got = top.setdefault(str(code), [])
        if len(got) < REPORT_CLASSES:
            got.append([cls, n])
    summary = {"records": {str(c): n for c, n in sorted(records.items())},
               "pruned": {str(c): n for c, n in sorted(pruned_n.items())},
               "classes": top, "walked": walked_n}
    return int(start or 0), bytes(out), summary


def _hex(sha: str) -> str:
    return sha.split(":", 1)[1][:16]


def map_path(d: Path, sha: str) -> Path:
    return d / f"families-{_hex(sha)}.json"


def codes_path(d: Path, sha: str) -> Path:
    return d / f"families-{_hex(sha)}.u8"


def report_path(d: Path, sha: str) -> Path:
    return d / f"families-{_hex(sha)}.report.json"


def _codes_pass(dump: Path, d: Path, meta: dict[str, Any], con: sqlite3.Connection, fmap: families.FamilyMap, *,
                say: Callable[[str], None], stop: Callable[[], bool] | None, workers: int) -> bool:
    """Every entity's family code under `fmap`, chunk by chunk into its
    `.u8` (preallocated, written at the chunk's first index, synced before
    the resume point moves), then the report. The classes newer than the
    map are walked over THIS dump's links (families.walk). True when done."""
    done = dict(meta.get("families_codes") or {})
    if done.get(fmap.sha256):
        return True
    rows = con.execute("SELECT id, parent FROM subclass WHERE id > ?", (fmap.max_item,)).fetchall()
    walked = families.walk((r[0] for r in rows), (r[1] for r in rows), fmap)
    if walked:
        say(f"[wikidata] pack: {dump.name}: {len(walked)} classes newer than the map's dump "
            f"{fmap.dump}, placed by this dump's P279 links")
    out = codes_path(d, fmap.sha256)
    total = int(meta.get("entities") or 0)
    if not out.is_file() or out.stat().st_size != total:
        with open(out, "wb") as f:
            f.truncate(total)

    def write(con: sqlite3.Connection, k: int, result: tuple[int, bytes, dict[str, Any]]) -> None:
        start, codes, summary = result
        with open(out, "r+b") as f:
            f.seek(start)
            f.write(codes)
            f.flush()
            os.fsync(f.fileno())
        con.execute("INSERT OR REPLACE INTO family_chunk (map, chunk, summary) VALUES (?, ?, ?)",
                    (fmap.sha256, k, json.dumps(summary, sort_keys=True)))

    if not _lines_pass(dump, d, meta, con, what=f"codes_{_hex(fmap.sha256)}", job=_codes_job, insert=write,
                       say=say, stop=stop, workers=workers, init=_codes_init, serial_init=_set_fmap,
                       init_args=(fmap.raw, walked)):
        return False
    _write_report(dump, d, con, fmap, say=say)
    meta.pop(f"codes_{_hex(fmap.sha256)}_chunks", None)
    done[fmap.sha256] = True
    meta["families_codes"] = done
    _write_meta(d, meta)
    return True


def _write_report(dump: Path, d: Path, con: sqlite3.Connection, fmap: families.FamilyMap, *,
                  say: Callable[[str], None]) -> None:
    """Each family's records and pruned entities, and the classes that put
    the most entities in it (merged from each chunk's first
    `REPORT_CLASSES`: exact counts for every class that leads a chunk), with
    their labels — what is read before `create` pins the map."""
    from collections import Counter
    records: Counter = Counter()
    pruned_n: Counter = Counter()
    classes: dict[int, Counter] = {}
    walked_n = 0
    for (raw,) in con.execute("SELECT summary FROM family_chunk WHERE map=? ORDER BY chunk", (fmap.sha256,)):
        s = json.loads(raw)
        records.update({int(c): n for c, n in s["records"].items()})
        pruned_n.update({int(c): n for c, n in s["pruned"].items()})
        for c, pairs in s["classes"].items():
            classes.setdefault(int(c), Counter()).update({cls: n for cls, n in pairs})
        walked_n += int(s.get("walked") or 0)
    labels_of = LabelTable(dump)

    def label(q: int) -> str | None:
        for lang in ("en", "mul"):
            got = labels_of(f"Q{q}", lang)
            if got:
                return got
        return None
    # `walked_entities`: entities decided by a class newer than the map,
    # placed by this dump's links — 0 on the map's own dump, by definition
    report = {"map": fmap.sha256, "map_dump": fmap.dump, "dump": dump.name, "walked_entities": walked_n,
              "families": [{"family": f, "records": records.get(code, 0), "pruned": pruned_n.get(code, 0),
                            "roots": [[q, label(q)] for q in fmap.roots.get(f, ())],
                            "top_classes": [[q, label(q), n] for q, n in classes.get(code, Counter()).most_common(
                                50 if code == fmap.other else 25)]}
                           for code, f in enumerate(fmap.families, 1)],
              # well-known classes and the family the map gives them (families.PROBES)
              "probes": [[q, label(q), fmap.name(fmap.code_of_class(q))] for q in families.PROBES]}
    tmp = report_path(d, fmap.sha256).with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, report_path(d, fmap.sha256))
    say(f"[wikidata] pack: {dump.name}: families under map {fmap.sha256[:23]}… — " + ", ".join(
        f"{f} {records.get(code, 0)}" + (f" (+{pruned_n[code]} pruned)" if pruned_n.get(code) else "")
        for code, f in enumerate(fmap.families, 1)) + f"; report {report_path(d, fmap.sha256).name}")


def _lines_pass(dump: Path, d: Path, meta: dict[str, Any], con: sqlite3.Connection, *, what: str,
                job: Callable[[str], Any], insert: str | Callable[[sqlite3.Connection, int, Any], None],
                say: Callable[[str], None], stop: Callable[[], bool] | None, workers: int,
                init: Callable | None = None, serial_init: Callable | None = None, init_args: tuple = ()) -> bool:
    """`job` over every chunk file in order, its rows committed per chunk
    with the resume point (`<what>_chunks`) in the meta. `insert` is the
    statement the rows go through, or a callable `(con, chunk, result)`
    that writes them inside the same transaction. True when every chunk is
    done; False when `stop` asked or the pool failed (resumable)."""
    global _parallel_failed
    from collections import deque
    from concurrent.futures.process import BrokenProcessPool

    files = [str(p) for p in chunk_files(dump)]
    key = f"{what}_chunks"
    start = int(meta.get(key) or 0)
    t0 = time.monotonic()

    def commit(k: int, rows: Any) -> bool:
        con.execute("BEGIN IMMEDIATE")
        if callable(insert):
            insert(con, k, rows)
        else:
            con.executemany(insert, rows)
        con.execute("COMMIT")
        meta[key] = k + 1
        _write_meta(d, meta)
        if (k + 1) % 50 == 0 or k + 1 == len(files):
            say(f"[wikidata] pack: {dump.name}: {what}, {k + 1} of {len(files)} chunk files, "
                f"{time.monotonic() - t0:.0f} s")
        return not (stop is not None and stop()) or k + 1 == len(files)

    # THE NODE PROFILE'S CORES (app/cpu_limit.py, operator 2026-10-27): at
    # most that many chunk files computing at once, re-read at each chunk's
    # commit; 0 pauses there — what is computing finishes and is committed,
    # then a bounded wait that honours `stop` (False: resumable, as a stop)
    watch = cpu_limit.Watch(lambda m: say(f"[wikidata] pack: {dump.name}: {what}: {m}"))
    n = 1 if _parallel_failed else workers
    if n <= 1:
        if serial_init is not None:
            serial_init(*init_args)
        for k in range(start, len(files)):
            if watch.limit().cores == 0 and not cpu_limit.wait_while_paused(stop, watch):
                return False
            if not commit(k, job(files[k])):
                return False
        return True
    hung = False
    ex = ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                             initializer=init or _child_nice, initargs=init_args)
    try:
        nxt = start
        pending: deque = deque()
        lim = watch.limit()

        def fill() -> None:
            # computing (not merely answered and waiting for its turn to
            # commit) below the profile's cores; the parent holds at most
            # n + 2 results either way
            nonlocal nxt
            while nxt < len(files) and len(pending) < n + 2 and lim.cores != 0 \
                    and sum(1 for _k, f in pending if not f.done()) < cpu_limit.cap(n + 2, lim):
                pending.append((nxt, ex.submit(job, files[nxt])))
                nxt += 1
        fill()
        while True:
            if not pending:
                if nxt >= len(files):
                    return True
                # paused, with nothing computing: wait at this boundary
                if not cpu_limit.wait_while_paused(stop, watch):
                    return False
                lim = watch.limit()
                fill()
                continue
            k, fut = pending.popleft()
            if not commit(k, fut.result(timeout=CHUNK_TIMEOUT_S)):
                return False
            lim = watch.limit()                  # a chunk's commit: the unit boundary
            fill()
    except (BrokenProcessPool, TimeoutError) as e:
        _parallel_failed = True
        hung = isinstance(e, TimeoutError)
        say(f"[wikidata] pack: {dump.name}: the {what} pool failed ({type(e).__name__}: {e}); stopped after "
            f"{meta.get(key, start)} chunk(s), resumable; the next pass of this process is serial")
        return False
    finally:
        if hung:
            # a stuck child is terminated: a pool left to finish would be
            # joined by the interpreter's exit for as long as it is stuck
            parallel.discard(ex)
        else:
            ex.shutdown(wait=True, cancel_futures=True)


def _write_meta(d: Path, meta: dict[str, Any]) -> None:
    tmp_meta = _meta_path(d).with_name("meta.json.tmp")
    tmp_meta.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
    os.replace(tmp_meta, _meta_path(d))


def _finish(dump: Path, d: Path, meta: dict[str, Any], *, say: Callable[[str], None],
            stop: Callable[[], bool] | None, workers: int, upgrade: bool = False) -> dict[str, Any]:
    """After the chunks, each from the pack's own lines and resumable at a
    chunk: the language codes (a version-1 pack's upgrade; a build wrote
    them with the chunks), the names (`names_done`: parser 2 may read from
    here on), the class tree, the family map this dump gives and every
    entity's code under it with the report (`families_done`); upgrading,
    then the version. Returns the meta, `complete` False while this has not
    ended — the caller defers and the next pass resumes."""
    con = sqlite3.connect(d / "labels.sqlite", isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        for schema in (_LANGUAGES_SCHEMA, _NAMES_SCHEMA, _SUBCLASS_SCHEMA, _CHUNK_TOP_SCHEMA, _FAMILY_CHUNK_SCHEMA):
            con.execute(schema)
        if upgrade:
            say(f"[wikidata] pack: {dump.name}: version {meta.get('pack_version')} → {PACK_VERSION} from the "
                f"pack's own lines (RULE 1: no pass over the dump)")
        if upgrade and meta.get("pack_version") == 1 and not meta.get("languages_done"):
            if not _lines_pass(dump, d, meta, con, what="languages", job=_languages_job,
                               insert="INSERT OR REPLACE INTO language (id, code) VALUES (?, ?)",
                               say=say, stop=stop, workers=workers):
                return {**meta, "complete": False}
            meta["languages_done"] = True
            _write_meta(d, meta)
        if not meta.get("names_done"):
            codes = dict(con.execute("SELECT id, code FROM language").fetchall())
            if not _lines_pass(dump, d, meta, con, what="names", job=_names_job,
                               insert="INSERT OR REPLACE INTO name (id, lang, label) VALUES (?, ?, ?)",
                               say=say, stop=stop, workers=workers, init=_names_init, serial_init=_set_codes,
                               init_args=(codes,)):
                return {**meta, "complete": False}
            for k in ("languages_chunks", "names_chunks", "languages_done"):
                meta.pop(k, None)
            meta["names_done"] = True
            _write_meta(d, meta)
            say(f"[wikidata] pack: {dump.name}: {len(codes)} language codes, "
                f"{con.execute('SELECT COUNT(*) FROM name').fetchone()[0]} names")
        if not meta.get("subclass_done"):
            if not _lines_pass(dump, d, meta, con, what="subclass", job=_subclass_job, insert=_write_subclass,
                               say=say, stop=stop, workers=workers):
                return {**meta, "complete": False}
            meta.pop("subclass_chunks", None)
            meta["max_item"] = int(con.execute("SELECT COALESCE(MAX(max_item), 0) FROM chunk_top").fetchone()[0])
            meta["subclass_done"] = True
            _write_meta(d, meta)
            say(f"[wikidata] pack: {dump.name}: the class tree — "
                f"{con.execute('SELECT COUNT(*) FROM subclass').fetchone()[0]} P279 links, "
                f"the largest item Q{meta['max_item']}")
        if not meta.get("families_map"):
            raw = _creation_map(dump, con, int(meta["max_item"]))
            sha = "sha256:" + hashlib.sha256(raw).hexdigest()
            tmp = map_path(d, sha).with_suffix(".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, map_path(d, sha))
            meta["families_map"] = sha
            _write_meta(d, meta)
            say(f"[wikidata] pack: {dump.name}: its family map {sha} ({len(raw)} bytes, {map_path(d, sha).name})")
        fmap = families.load_map(map_path(d, meta["families_map"]).read_bytes())
        if not _codes_pass(dump, d, meta, con, fmap, say=say, stop=stop, workers=workers):
            return {**meta, "complete": False}
        meta["families_done"] = True
        if upgrade:
            meta["pack_version"] = PACK_VERSION
        _write_meta(d, meta)
        return meta
    finally:
        con.close()


def _creation_map(dump: Path, con: sqlite3.Connection, max_item: int) -> bytes:
    """The family map of this dump's own class tree (families.build_map)."""
    from array import array
    child, parent = array("q"), array("q")
    for c, p in con.execute("SELECT id, parent FROM subclass"):
        child.append(c)
        parent.append(p)
    return families.build_map(child, parent, max_item=max_item, dump=Path(dump).name)


_ready: set[str] = set()
_codes_cache: dict[str, dict[str, str]] = {}


def ready(dump: Path) -> bool:
    """A pack of version 2 or later, complete, whose names are done: what
    parser 2 reads — the families (version 3) do not hold it up."""
    key = str(pack_dir(dump))
    if key in _ready:
        return True
    meta = read_meta(dump) or {}
    version = meta.get("pack_version")
    ok = (isinstance(version, int) and 2 <= version <= PACK_VERSION and bool(meta.get("complete"))
          and bool(meta.get("names_done")))
    if ok:
        _ready.add(key)
    return ok


def language_codes(dump: Path) -> dict[str, str]:
    """Language item id → Wikimedia code, from a ready pack; a pack not
    ready DEFERS the document (a text or title written without the names
    would be wrong, not missing)."""
    key = str(pack_dir(dump))
    with _files_lock:
        got = _codes_cache.get(key)
    if got is not None:
        return got
    if not ready(dump):
        from app.chunking import PlatformMissing
        raise PlatformMissing(f"the wikidata pack of {Path(dump).name} is not ready for parser 2 "
                              f"(version {(read_meta(dump) or {}).get('pack_version')}, names not done): the "
                              f"next scan finishes it, and this document is parsed then")
    con = sqlite3.connect((pack_dir(dump) / "labels.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        got = dict(con.execute("SELECT id, code FROM language").fetchall())
    finally:
        con.close()
    with _files_lock:
        _codes_cache[key] = got
    return got


# ---- the families, read ----------------------------------------------------------------------

def creation_map(dump: Path) -> bytes:
    """The bytes of the family map this dump's own class tree gives under
    the CODE's family list and roots (families.py) — what a collection
    created, or a daughter continued, on this dump pins (its sha256 in the
    Codex, the bytes a SHARD member). The pack's own pass computed it with
    the list of its day; a family added to the code since gives another map,
    built here from the pack's class tree (a minute, no pass over the lines)
    and kept beside it, named by its hash. Refused, in plain words, until
    the class tree is done."""
    meta = read_meta(dump) or {}
    if not meta.get("subclass_done"):
        raise PackError(f"the family map of {Path(dump).name} is not built yet (pack version "
                        f"{meta.get('pack_version')}): the worker's next scan of the collection builds it "
                        f"from the pack's own lines — about an hour — and `create` reads it then")
    d = pack_dir(dump)
    sha = meta.get("families_map")
    if sha:
        raw = map_path(d, sha).read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != sha:
            raise PackError(f"{map_path(d, sha)} does not hash to {sha}, the pack's own record of it")
        if _code_rules(raw):
            return raw
    con = sqlite3.connect((d / "labels.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        raw = _creation_map(dump, con, int(meta["max_item"]))
    finally:
        con.close()
    sha = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not map_path(d, sha).is_file():
        tmp = map_path(d, sha).with_name(f".{map_path(d, sha).name}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, map_path(d, sha))
    return raw


def _code_rules(raw: bytes) -> bool:
    """A map written with the code's own family list, roots and fallback."""
    m = json.loads(raw)
    return (m.get("families") == list(families.FAMILIES)
            and m.get("roots") == {f: sorted(int(r) for r in rs) for f, rs in families.ROOTS.items()}
            and m.get("fallback", []) == sorted(families.FALLBACK))


_codes_views: dict[tuple[str, str], Any] = {}


def family_codes(dump: Path, sha: str):
    """Every entity's family code under the map `sha`, one byte an entity in
    dump order (a read-only memory map), or None while the pack has not
    computed them for that map."""
    d = pack_dir(dump)
    key = (str(d), sha)
    with _files_lock:
        got = _codes_views.get(key)
    if got is not None:
        return got
    meta = read_meta(dump) or {}
    if not (meta.get("families_codes") or {}).get(sha):
        return None
    import mmap
    with open(codes_path(d, sha), "rb") as f:
        view = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if os.fstat(f.fileno()).st_size else b""
    with _files_lock:
        _codes_views[key] = view
    return view


#: (the dump as the locator names it, the map) -> its codes, once found. The
#: bytes are a pure function of the dump and the map wherever the pack sits,
#: and a memory map stays readable when a carried pack's old copy goes; so
#: where the pack is (`pack_dir`: a realpath through the /data links and two
#: stats) is asked once, not once a document. Asked once a document, it was
#: the whole cost of the records build's plan: one core, ~11,700
#: documents/s, 1 h 48 min for 75.9 M (München's py-spy, 2026-10-30).
_codes_by_dump: dict[tuple[str, str], Any] = {}


def family_code_at(dump: Path, sha: str, index: int) -> int | None:
    """Entity `index`'s family code under the map `sha`; None while not computed."""
    key = (str(dump), sha)
    view = _codes_by_dump.get(key)
    if view is None:
        view = family_codes(dump, sha)
        if view is None:                # not computed yet: asked again next time
            return None
        _codes_by_dump[key] = view
    if not 0 <= index < len(view):
        return None
    code = view[index]
    return code or None


def ensure_codes(dump: Path, raw_map: bytes, *, log: Callable[[str], None] | None = None,
                 stop: Callable[[], bool] | None = None, workers: int | None = None) -> bool:
    """The family codes of this dump's entities under a PINNED map — the
    collection's, which for a later dump is not the dump's own: a pass over
    the pack's lines, resumable at a chunk, the classes newer than the map
    walked over this dump's links. Needs the pack's class tree (version 3).
    True when the codes are there."""
    say = log or (lambda m: None)
    fmap = families.load_map(raw_map)
    d = pack_dir(dump)
    meta = read_meta(dump) or {}
    if (meta.get("families_codes") or {}).get(fmap.sha256):
        return True
    if not meta.get("subclass_done"):
        raise PackError(f"the pack of {Path(dump).name} has no class tree yet (version {meta.get('pack_version')}): "
                        f"the worker's next scan builds it")
    if meta.get("families_map") == fmap.sha256 and not meta.get("families_done"):
        # the pack's OWN pass computes these codes (`_finish`, in the
        # worker's scan): a second pass beside it would race it for the
        # meta, so this one waits for it instead
        raise PackError(f"the family codes of {Path(dump).name} under its own map are being computed by the "
                        f"pack's pass (the worker's scan of the collection): the build runs once it is done")
    n = processes() if workers is None else max(1, int(workers))
    con = sqlite3.connect(d / "labels.sqlite", isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(_FAMILY_CHUNK_SCHEMA)
        return _codes_pass(dump, d, meta, con, fmap, say=say, stop=stop, workers=n)
    finally:
        con.close()


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

    from collections import deque
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_futures

    done = int(meta["chunks"])
    skip = done * chunk
    sub = max(1, min(SUB, chunk))
    #: slices submitted and not yet folded into a written chunk: bounded, so
    #: the parent holds at most this many slices' lines (~SUB entities each)
    max_slices = workers + 2
    state = {"next": done, "pruned": int(meta["pruned"])}
    closed: deque = deque()               # (chunk_no, [slice futures]) whose lines are all submitted
    current: list[Future] = []            # the open chunk's slice futures
    token = f"{os.getpid()}-{time.monotonic_ns()}"
    for stale in d.glob("*.parquet.*.tmp"):          # an earlier parallel pass's, never committed
        stale.unlink(missing_ok=True)
    ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_child_nice)
    writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wikidata-pack-writer")
    writing: list[tuple[int, Future, int, list, list, bool]] = []   # at most one chunk being written
    hung = False

    def in_flight() -> int:
        return len(current) + sum(len(f) for _, f in closed)

    # THE NODE PROFILE'S CORES (app/cpu_limit.py, operator 2026-10-27): at
    # most that many slices computing, looked up (cached) before each slice
    # is handed to the pool; the children are kept. 0 pauses at that
    # boundary: the slices computing finish, every chunk they complete is
    # written and committed, the open chunk stays in memory, and a bounded
    # wait honours `stop` (then the pass ends as a stop does, resumable)
    watch = cpu_limit.Watch(lambda m: say(f"[wikidata] pack: {dump.name}: {m}"))

    def computing() -> list[Future]:
        return [f for f in current if not f.done()] + [f for _, fs in closed for f in fs if not f.done()]

    def admit() -> bool:
        """Room for one more slice under the profile; False when stopped during a pause."""
        while True:
            lim = watch.limit()
            if lim.cores == 0:
                running = computing()
                if running:
                    done_, _ = wait_futures(running, timeout=CHUNK_TIMEOUT_S)
                    if not done_:
                        raise TimeoutError(f"no slice answered for {CHUNK_TIMEOUT_S:.0f} s")
                    continue
                while closed:                  # every complete chunk written and committed
                    if not write_head():
                        return False
                if writing:
                    commit_written()
                    if state.get("stopped"):
                        return False
                if not cpu_limit.wait_while_paused(stop, watch):
                    return False
                continue
            running = computing()
            if len(running) < cpu_limit.cap(workers, lim):
                return True
            done_, _ = wait_futures(running, timeout=CHUNK_TIMEOUT_S, return_when=FIRST_COMPLETED)
            if not done_:
                raise TimeoutError(f"no slice answered for {CHUNK_TIMEOUT_S:.0f} s")

    def write_meta() -> None:
        tmp_meta = _meta_path(d).with_name("meta.json.tmp")
        tmp_meta.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        os.replace(tmp_meta, _meta_path(d))

    def commit_written() -> None:
        """The chunk the writer finished: labels, rename, meta — in order."""
        chunk_no, fut, pruned, labels, codes, final = writing.pop()
        fut.result(timeout=CHUNK_TIMEOUT_S)
        con.execute("BEGIN IMMEDIATE")
        con.executemany("INSERT OR REPLACE INTO label (id, lang, label) VALUES (?, ?, ?)", labels)
        con.executemany("INSERT OR REPLACE INTO language (id, code) VALUES (?, ?)", codes)
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
        if not final:
            say(f"[wikidata] pack: {dump.name}: {state['next'] * chunk} entities, "
                f"{state['next']} chunk file(s), {time.monotonic() - t0:.0f} s")
            if stop is not None and stop():          # as the serial pass: after each chunk
                state["stopped"] = True

    def write_head(final: bool = False) -> bool:
        """The oldest closed chunk: its slices gathered in order, its file
        handed to the writer thread; the chunk before it committed first.
        False when `stop` asked after that commit: nothing more is started."""
        if writing:
            commit_written()               # in order, and before a later slice can raise
            if state.get("stopped"):
                return False
        chunk_no, futs = closed.popleft()
        pruned_n, labels, codes, tables = 0, [], [], []
        try:
            for f in futs:
                p, lab, cod, t = f.result(timeout=CHUNK_TIMEOUT_S)
                pruned_n += p
                labels.extend(lab)
                codes.extend(cod)
                tables.append(t)
        except BaseException:
            closed.appendleft((chunk_no, futs))
            raise
        writing.append((chunk_no, writer.submit(_write_slices, tables, _tmp_path(d, chunk_no, token)),
                        pruned_n, labels, codes, final))
        return True

    try:
        result = dump_mod.ReadResult()
        lines: list[bytes] = []
        line_start = skip                  # the index of `lines[0]`
        in_chunk = 0                       # lines of the open chunk submitted or held
        chunk_no = done
        with _decompressed(dump, workers) as stream:
            for _index, raw in dump_mod.iter_lines(stream, result=result, skip=skip):
                lines.append(raw)
                in_chunk += 1
                if len(lines) < sub and in_chunk < chunk:
                    continue
                if not admit():
                    return meta
                current.append(ex.submit(_rows_job, lines, line_start, languages))
                line_start += len(lines)
                lines = []
                if in_chunk >= chunk:
                    closed.append((chunk_no, current))
                    current, in_chunk = [], 0
                    chunk_no += 1
                # bounded: fold the oldest closed chunk once too many slices are out
                while closed and (in_flight() > max_slices or all(f.done() for f in closed[0][1])):
                    if not write_head():
                        return meta
                if in_flight() > max_slices and current:
                    wait_futures([current[0]], timeout=CHUNK_TIMEOUT_S)
        while closed:
            if not write_head():
                return meta
        if writing:
            commit_written()
            if state.get("stopped"):
                return meta
        if not result.complete:
            if lines or current:
                say(f"[wikidata] pack: {dump.name}: the stream ended {result.truncated_at or 'unexpectedly'}: "
                    f"{in_chunk} entities of an incomplete chunk not written")
            raise PackError(f"{dump.name}: the dump ended before its closing bracket (truncated download?)")
        meta["entities"] = line_start + len(lines)
        if lines:
            if not admit():
                return meta
            current.append(ex.submit(_rows_job, lines, line_start, languages))
        if current:
            closed.append((chunk_no, current))
            current = []
            write_head(final=True)
            commit_written()
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
        # the running children finish (one slice, bounded) unless one hung,
        # which is terminated (`parallel.discard`): a pool left to finish
        # would be joined by the interpreter's exit for as long as it is stuck
        if hung:
            parallel.discard(ex)
        else:
            ex.shutdown(wait=True, cancel_futures=True)
        writer.shutdown(wait=True)
        for chunk_no, *_ in writing:
            _tmp_path(d, chunk_no, token).unlink(missing_ok=True)
        for chunk_no, _ in closed:
            _tmp_path(d, chunk_no, token).unlink(missing_ok=True)


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
    a bounded cache in front: the textifier asks for every referenced id.
    `lang` = `names.NAME` reads the `name` table — of a version-2 pack only:
    on a pack still being upgraded the lookup DEFERS the document (a text
    written without the names would be a wrong text, not a missing one)."""

    def __init__(self, dump: Path, cache: int = 200_000):
        self.dump = Path(dump)
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
        if lang == names.NAME:
            if not ready(self.dump):
                from app.chunking import PlatformMissing
                raise PlatformMissing(f"the wikidata pack of {self.dump.name} has no names yet (version "
                                      f"{(read_meta(self.dump) or {}).get('pack_version')}): the next scan "
                                      f"finishes it, and this document is parsed then")
            row = self._con().execute("SELECT label FROM name WHERE id=?", (entity_id,)).fetchone()
        else:
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
    _metas.clear()
    with _files_lock:
        _groups.clear()
        _tables.clear()
        _codes_cache.clear()
        _ready.clear()
        _codes_views.clear()
