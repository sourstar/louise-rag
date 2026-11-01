"""ONE VECTOR TABLE PER VALUE OF A SEARCH KEY (operator, 2026-11-01:
"Different tables, make it a standard practice when separated searches
might be done"; docs/notes/vector-tables-split.md, 09 §9.6).

A plugin that declares a TABLE KEY (`slicing.table_key_for`: wikipedia's
`lang`, wikidata's `family`) keeps one vector table per value of it, so a
search narrowed by the key reads its tables whole instead of masking rows
of one big table. Measured on wikipedia (2026-10-31): `lang=en` took
202 ms against 57 ms with no filter; the time went on the prefilter's row
mask, and Lance keeps no IVF index per value of a column.

ON DISK. `<lance_dir>/layout.json` (`{"format": 1, "table_key": …}`) is the
switch; each value is a Hive-style directory `<key>=<value>` (the value
percent-encoded) holding an ordinary LanceDB table `chunks`, today's schema
and storage options. Without `layout.json`, the single `chunks.lance` of
before. This is the REST form only: bundles, attestations, `space_id` and
`collection_id` do not change.

A PART is the collection with its own `lance_dir` (`view`), so every
function of `app/embeddings.py` and `app/vector_index.py` works on a part
as it worked on the one table. The public functions of `embeddings`
dispatch at their entry: writes and reads by document to the document's
part, a search to the parts its filter selects, scans and counts over all.

ROUTING. A document's part is its slice's value for the key, read from the
manifest row WITHOUT parsing (`slicing.slice_hint`): Wikipedia's from the
native id alone, Wikidata's from the family byte the pack keeps. A slice
key is stable for the life of the record (slicing.py), so a document never
moves between parts and a chunk id lives in exactly one.

Standards: Hive partition directories for the names; list partitioning
with partition pruning, as PostgreSQL calls it; Milvus's partition key is
the vector-database precedent. Lance (pylance 9.0.1) has no partitioned
table. Lance Namespace (`DirectoryNamespace`) is the standard alternative
for the layout: it organises tables, routes nothing, and would pay once
tables live in object storage or a catalogue (the note, §8).
"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, unquote

LAYOUT = "layout.json"
FORMAT = 1
#: the table inside each part's directory (embeddings.TABLE's directory)
TABLE_DIR = "chunks.lance"
#: plugins whose native id alone says the slice: no manifest read to route
PURE = frozenset({"wikipedia", "embedded"})
#: manifest rows read per routing query
ROUTE_BATCH = 500


class Unrouted(RuntimeError):
    """A document whose part cannot be named: no manifest row, or a slice
    without the key. Never guessed — a row written to the wrong part would
    be invisible to every search narrowed by the key."""


class LayoutError(RuntimeError):
    """`layout.json` says something this code does not read."""


_cache_lock = threading.Lock()
_layouts: dict[str, tuple[int, dict]] = {}


def layout(collection) -> dict | None:
    """The collection's layout file, or None (one table). Re-read when the
    file changes, so a process sees a split the moment it is swapped in."""
    p = Path(collection.lance_dir) / LAYOUT
    try:
        mtime = p.stat().st_mtime_ns
    except (FileNotFoundError, NotADirectoryError):
        return None
    key = str(p)
    with _cache_lock:
        hit = _layouts.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT or not isinstance(data.get("table_key"), str) or not data["table_key"]:
        raise LayoutError(f"{p}: format {data.get('format')!r}, table_key {data.get('table_key')!r} — "
                          f"this code reads format {FORMAT} with a table key")
    with _cache_lock:
        _layouts[key] = (mtime, data)
    return data


def declared_key(collection) -> str | None:
    """The table key the collection's plugin declares for it (slicing),
    whatever is on disk."""
    from .pack import slicing
    return slicing.table_key_for(collection)


def key_of(collection) -> str | None:
    lay = layout(collection)
    return lay["table_key"] if lay else None


def is_split(collection) -> bool:
    """Whether this is a split collection's whole (never true of a part)."""
    return getattr(collection, "vector_part", None) is None and layout(collection) is not None


def single_exists(collection) -> bool:
    return (Path(collection.lance_dir) / TABLE_DIR).exists()


def write_layout(collection, key: str) -> None:
    """The switch, written atomically (a rename)."""
    d = Path(collection.lance_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{LAYOUT}.{os.getpid()}.{threading.get_ident()}"
    tmp.write_text(json.dumps({"format": FORMAT, "table_key": key}, sort_keys=True), encoding="utf-8")
    os.replace(tmp, d / LAYOUT)


def ensure_split(collection) -> bool:
    """For a WRITE: whether it goes to the parts. A collection whose plugin
    declares a key and that holds no vector table yet is created split here,
    its layout written before its first part; one that has a single table
    keeps it until `louise dev split-vectors` moves it."""
    if getattr(collection, "vector_part", None) is not None:
        return False
    if layout(collection) is not None:
        return True
    key = declared_key(collection)
    if key is None or single_exists(collection):
        return False
    write_layout(collection, key)
    return True


def part_name(key: str, value: str) -> str:
    return f"{key}={quote(str(value), safe='')}"


def part_dir(collection, value: str, key: str | None = None) -> Path:
    key = key or key_of(collection) or declared_key(collection)
    if key is None:
        raise LayoutError(f"{collection.name} has no table key")
    return Path(collection.lance_dir) / part_name(key, value)


def view(collection, value: str, key: str | None = None):
    """The part `value` of a split collection: the same collection, its own
    `lance_dir`. Caches keyed on the Lance directory (connections, the
    index's) are per part by construction."""
    return dataclasses.replace(collection, lance_dir=part_dir(collection, value, key),
                               vector_part=str(value))


def values(collection) -> list[str]:
    """The parts that hold a table, by value, sorted."""
    key = key_of(collection)
    if key is None:
        return []
    prefix = f"{key}="
    out = []
    try:
        entries = list(Path(collection.lance_dir).iterdir())
    except FileNotFoundError:
        return []
    for e in entries:
        if e.name.startswith(prefix) and (e / TABLE_DIR).exists():
            out.append(unquote(e.name[len(prefix):]))
    return sorted(out)


def views(collection, only: Iterable[str] | None = None) -> list:
    """Every table of the collection as a collection of its own: `[collection]`
    for one table, its parts (those in `only`, when given) when split."""
    if not is_split(collection):
        return [collection]
    vals = values(collection)
    if only is not None:
        want = {str(v) for v in only}
        vals = [v for v in vals if v in want]
    return [view(collection, v) for v in vals]


def component(slice_name: str | None, key: str) -> str | None:
    """`lang=en/pages=3M`, `lang` → `en`."""
    if not slice_name:
        return None
    for seg in slice_name.split("/"):
        k, _, v = seg.partition("=")
        if k == key and v:
            return v
    return None


def _source_id(collection) -> str:
    from . import collections as collections_mod
    return getattr(collections_mod.PLUGIN_REGISTRY.get(collection.plugin), "source_id", collection.plugin)


def values_of_books(collection, book_ids) -> dict[int, str | None]:
    """Each document's part, by its book id (the native id): None where it
    cannot be named. Pure for the plugins whose native id says the slice;
    otherwise one indexed manifest read per ROUTE_BATCH documents (by uid,
    the primary key) and the slice from the row, never a parse."""
    from .pack import slicing
    key = key_of(collection) or declared_key(collection)
    books = sorted({int(b) for b in book_ids})
    out: dict[int, str | None] = {b: None for b in books}
    if key is None or not books:
        return out
    plugin = collection.plugin
    if plugin in PURE:
        for b in books:
            out[b] = component(slicing.slice_hint(plugin, {"native_id": str(b)}), key)
        return out
    ctx = slicing.router_context(collection)
    from . import db
    con = db.read_only_connect(collection)
    if con is None:
        return out
    source = _source_id(collection)
    try:
        for i in range(0, len(books), ROUTE_BATCH):
            part = books[i:i + ROUTE_BATCH]
            marks = ",".join("?" for _ in part)
            for r in con.execute(f"SELECT native_id, path, lang FROM documents WHERE uid IN ({marks})",
                                 [f"{source}:{b}" for b in part]):
                out[int(r["native_id"])] = component(slicing.slice_hint(plugin, r, ctx), key)
    finally:
        con.close()
    return out


def route(collection, book_ids, *, probe: Callable[[object, list[int]], set] | None = None
          ) -> dict[str, list[int]]:
    """value → the documents of `book_ids` in that part. A document the
    router cannot place raises Unrouted — unless `probe` (part view, books →
    the books that part holds) is given: then every existing part is asked,
    and an unplaced document found nowhere is left out (it holds nothing)."""
    placed = values_of_books(collection, book_ids)
    groups: dict[str, list[int]] = {}
    lost = []
    for b, v in placed.items():
        if v is None:
            lost.append(b)
        else:
            groups.setdefault(v, []).append(b)
    if lost:
        if probe is None:
            raise Unrouted(f"{collection.name}: no {key_of(collection) or declared_key(collection)} "
                           f"for document(s) {lost[:5]}{'…' if len(lost) > 5 else ''} — "
                           f"no manifest row, or its slice lacks the key")
        for v in values(collection):
            found = probe(view(collection, v), lost)
            if found:
                groups.setdefault(v, []).extend(sorted(int(b) for b in found))
    return groups


def book_of_chunk(chunk_id: str) -> int | None:
    """`{source}:{native_id}:{seq}:{version}` → native_id."""
    parts = str(chunk_id).rsplit(":", 2)
    if len(parts) != 3:
        return None
    try:
        return int(parts[0].rsplit(":", 1)[-1])
    except ValueError:
        return None


def forget(collection) -> None:
    with _cache_lock:
        _layouts.pop(str(Path(collection.lance_dir) / LAYOUT), None)
