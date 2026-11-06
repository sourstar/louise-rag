"""`ask facets <collection> [<field>] [--under VALUE]` (05 §5.7; 07 §7.3) —
the fields a collection can be filtered on, declared in its Codex, and
the values this peer holds for each, with this peer's counts. Migration
step 11. A listing, not a search: nothing is ranked.

The values are read out of the records this peer holds, through the
manifest's metadata columns — `lang`, `authors` (joined by `; `),
`subjects` (joined by `|`) — one pass per field, cached under `index/`
beside the text index for the snapshot it was built for (05 §5.7: "the
index is local and derived"). A `tree` facet splits each value on its
separator and counts every prefix path; a `range` facet lists bounds, and
— since the manifest carries no `date` and no `extra` — its values are read
out of the record parts held instead (app/ask/ranges.py; amended
2026-10-25). A flat or tree field the manifest does not carry is said so,
not guessed.

EVERY COST HERE IS BOUNDED, after the 2026-10-07 incident: the first
version kept every (value, id) of a field in a list and every distinct
value in a dict, and on pubmed's 33 million records the worker reached
34 GB and was killed. Now a field with more than MAX_DISTINCT distinct
values is NOT counted — the cache says so and `ask facets` repeats it —
and the postings a filter reads (app/ask/filters.py) are written in a
second pass, streamed in batches, and only for values NARROW enough for
a filter to use at all (at most `filters.MAX_IDS` records: a broader
value is refused before any posting is read), the rows per field capped
at POSTINGS_ROWS_BOUND, beyond which the field has no postings and the
cache says why.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from app.hold.codex import CodexError

#: facet field -> (manifest column, the join the pipeline writes)
COLUMNS = {"lang": ("lang", None), "language": ("lang", None), "languages": ("lang", None),
           "authors": ("authors", "; "), "creators": ("authors", "; "),
           "subjects": ("subjects", "|"), "title": ("title", None)}
BUILD_BOUND = 500_000            # documents: above this the maintainer builds, a request does not
MAX_DISTINCT = 200_000           # distinct values a field may have and still be counted here
POSTINGS_ROWS_BOUND = 20_000_000  # (value, id) rows a field's postings may reach
CACHE = "facets"                 # under the collection's index/


class FacetError(CodexError):
    exit_code = 2


class NotBuilt(CodexError):
    """Too large to count on request: the maintainer builds it (exit 3)."""
    exit_code = 3


def declared(codex) -> list[dict[str, Any]]:
    """The Codex's `facets` (02 §2.1), as dicts."""
    return [f.to_dict() if hasattr(f, "to_dict") else dict(f) for f in (getattr(codex, "facets", None) or ())]


def facet_of(codex, field: str) -> dict[str, Any]:
    for f in declared(codex):
        if f["field"] == field:
            return f
    raise FacetError(f"{field!r} is not a facet of this collection; its Codex declares "
                     f"{', '.join(f['field'] for f in declared(codex)) or 'none'} (05 §5.7)")


def _split(raw: str | None, join: str | None) -> list[str]:
    if not raw:
        return []
    if join is None:
        return [raw.strip()] if raw.strip() else []
    return [v.strip() for v in raw.split(join) if v.strip()]


def _pass(con, column: str, join: str | None, *, where_in_scope: bool = True) -> Iterator[tuple[int, list[str]]]:
    """One streamed pass over the manifest's column: `(native_id, values)`
    per record that has a numeric id."""
    scope = " WHERE in_scope=1" if where_in_scope else ""
    for native_id, raw in con.execute(f"SELECT native_id, {column} FROM documents{scope}"):
        try:
            nid = int(native_id)
        except (TypeError, ValueError):
            continue
        yield nid, _split(raw, join)


def count(con, codex, field: str, *, where_in_scope: bool = True, max_distinct: int | None = None,
          stop: Callable[[], bool] | None = None, max_depth: int | None = None) -> dict[str, Any]:
    """One pass over the manifest's column: `{field, kind, documents,
    values: {value: count}}` for flat and tree facets (a tree counts every
    prefix path, or only those up to `max_depth` separators deep when
    given), `{bounds: [min, max]}` for a range. A field with more than
    `max_distinct` distinct values is not counted: `values` is None and
    `why` says so — bounded memory, never a partial count passed off as
    the count."""
    f = facet_of(codex, field)
    column, join = COLUMNS.get(field, (None, None))
    if column is None:
        raise FacetError(f"{field!r}: the manifest carries no column for it yet; its values are in "
                         f"the records (09 §9.5) and a pass over the shards is not built")
    kind, sep = f["kind"], f.get("separator")
    max_distinct = MAX_DISTINCT if max_distinct is None else int(max_distinct)
    counts: dict[str, int] = {}
    lo = hi = None
    n = 0
    why = None
    for nid, values in _pass(con, column, join, where_in_scope=where_in_scope):
        n += 1
        if stop is not None and n % 100_000 == 0 and stop():
            raise NotBuilt(f"{field!r}: the pass was stopped before it finished")
        for value in values:
            if kind == "range":
                lo = value if lo is None or value < lo else lo
                hi = value if hi is None or value > hi else hi
                continue
            if kind == "tree" and sep:
                parts = value.split(sep)
                deepest = len(parts) if max_depth is None else min(len(parts), max_depth)
                for i in range(1, deepest + 1):
                    path = sep.join(parts[:i])
                    counts[path] = counts.get(path, 0) + 1
            else:
                counts[value] = counts.get(value, 0) + 1
        if len(counts) > max_distinct:
            why = (f"more than {max_distinct} distinct values after {n} records: not counted on this "
                   f"peer (bounded memory; 05 §5.7's counts are for a field a reader can browse)")
            counts = {}
            break
    out: dict[str, Any] = {"field": field, "kind": kind, "separator": sep, "authority": f.get("authority"),
                           "documents": n}
    if kind == "range":
        out["bounds"] = [lo, hi]
    elif why is not None:
        out["values"] = None
        out["why"] = why
    else:
        out["values"] = counts
        if max_depth is not None and kind == "tree":
            out["depth"] = max_depth
    return out


def _narrow_postings(con, column: str, join: str | None, kind: str, sep: str | None,
                     counts: dict[str, int], max_ids: int, depth: int | None = None) -> Iterator[tuple[str, int]]:
    """Second pass: `(value, native_id)` for the values a filter can use —
    at most `max_ids` records each. A tree value's postings are its full
    paths; a narrow ancestor's subtree is the union of its (narrower)
    descendants, so every posting a narrow value needs is written. A tree
    counted to `depth` is posted at that depth: the record's paths cut
    there, each once."""
    for nid, values in _pass(con, column, join):
        if depth is not None and kind == "tree" and sep:
            values = list({sep.join(v.split(sep)[:depth]) for v in values})
        for value in values:
            c = counts.get(value)
            if c is not None and c <= max_ids:
                yield value, nid


def listing(collection: str, field: str | None = None, under: str | None = None,
            root: Path | None = None) -> dict[str, Any]:
    """`ask facets <collection> [<field>] [--under VALUE]` over a held
    collection: the fields, or one field's values with this peer's counts."""
    from contextlib import closing
    from app import collections as collections_mod
    from app.hold import collection as col
    from app.share import coverage as cov, seeder
    root = Path(root) if root is not None else collections_mod.louise_root() / "collections"
    name, codex = col.resolve(root, collection)
    snap = seeder.cached_snapshot(col.shard_dir_of(root, name))
    sid = snap.snapshot_id if snap else None
    index_dir = root / name / "index"
    if not (root / name / "state" / "manifest.sqlite").is_file():
        raise FacetError(f"{name}: no manifest yet — nothing is held to count")
    with closing(cov.manifest(root, name)) as con:
        if field is None:
            return fields(con, codex, index_dir, snapshot_id=sid)
        return values(con, codex, index_dir, field, snapshot_id=sid, under=under,
                      shard_dir=col.shard_dir_of(root, name), snapshot=snap)


def _cache_path(index_dir: Path, field: str) -> Path:
    return Path(index_dir) / CACHE / f"{field}.json"


MARKER = "snapshot.json"         # under the cache: the snapshot the maintainer last indexed whole


def marker_path(index_dir: Path) -> Path:
    return Path(index_dir) / CACHE / MARKER


def read_marker(index_dir: Path) -> tuple[str, str] | None:
    """`(snapshot_id, hash)` of the snapshot whose facets, edges and content
    hashes the maintainer last indexed without a failure, or None. Durable
    on purpose: the worker held this only in memory until 2026-10-28, and
    every restart recounted pubmed's facets — three streamed passes over
    40M rows, 16.5 minutes of the index resource per deploy (München)."""
    p = marker_path(index_dir)
    if not p.is_file():
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
        return str(got["snapshot_id"]), str(got["hash"])
    except (ValueError, KeyError, TypeError):
        return None


def write_marker(index_dir: Path, snapshot_id: str, snapshot_hash: str, *, now: float | None = None) -> None:
    p = marker_path(index_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps({"snapshot_id": snapshot_id, "hash": snapshot_hash,
                               "indexed_at": time.time() if now is None else now}, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)


def _write_cache(index_dir: Path, field: str, got: dict[str, Any]) -> None:
    p = _cache_path(index_dir, field)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(json.dumps(got, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _build_range(f: dict[str, Any], index_dir: Path, *, shard_dir: Path, snapshot, snapshot_id: str | None,
                 now: float | None, stop: Callable[[], bool] | None) -> dict[str, Any]:
    """A range facet (amended 2026-10-25): its values are read out of the
    record parts held — the manifest carries no `date` and no `extra` — into
    the postings a range filter reads (app/ask/ranges.py); the cache holds
    its bounds and its type."""
    from app.ask import filters as filters_mod, ranges
    pcon = filters_mod.connect(Path(index_dir))
    try:
        got = ranges.index(pcon, f["field"], shard_dir=shard_dir, snapshot=snapshot, snapshot_id=snapshot_id,
                           rows_bound=POSTINGS_ROWS_BOUND, stop=stop)
    except ranges.Stopped as e:
        raise NotBuilt(str(e)) from None
    finally:
        pcon.close()
    return {"field": f["field"], "kind": "range", "separator": None, "authority": f.get("authority"), **got,
            "snapshot_id": snapshot_id, "built_at": time.time() if now is None else now}


def build(con, codex, index_dir: Path, *, snapshot_id: str | None, fields=None, now: float | None = None,
          stop: Callable[[], bool] | None = None, log: Callable[[str], None] | None = None,
          shard_dir: Path | None = None, snapshot=None) -> list[str]:
    """Count every declared facet (or `fields`), write the cache, and write
    the postings a filter reads for the values narrow enough to use; the
    fields written. What a maintainer calls at each snapshot. Memory is
    bounded whatever the collection's size: a pass streams, the counts
    stop at MAX_DISTINCT, the postings at POSTINGS_ROWS_BOUND. A range
    facet is read from the record parts of `snapshot` held in `shard_dir`,
    and skipped without them."""
    from app.ask import filters as filters_mod
    kinds = {f["field"]: f for f in declared(codex)}
    wanted = list(kinds) if fields is None else list(fields)
    written = []
    for field in wanted:
        f = kinds.get(field)
        if f is not None and f["kind"] == "range":
            if shard_dir is None or snapshot is None:
                continue
            got = _build_range(f, index_dir, shard_dir=shard_dir, snapshot=snapshot, snapshot_id=snapshot_id,
                               now=now, stop=stop)
            _write_cache(index_dir, field, got)
            written.append(field)
            if log and not got["postings"]["built"]:
                log(f"facets: {field}: range postings not built — {got['postings']['why']}")
            elif log and got["postings"].get("unparsed"):
                log(f"facets: {field}: {got['postings']['unparsed']} values are not a {got.get('type')} "
                    f"and are in no range")
            continue
        if field not in COLUMNS:
            continue
        got = count(con, codex, field, stop=stop)
        if got["kind"] == "tree" and got.get("values") is None:
            # the full paths explode (pubmed's MeSH descriptor/qualifier pairs,
            # 2026-10-07): the descriptors alone are a bounded, browsable list
            shallow = count(con, codex, field, stop=stop, max_depth=1)
            if shallow.get("values") is not None:
                shallow["note"] = (f"counted to depth 1 only: {got.get('why')}; a filter names a value at "
                                   f"that depth")
                got = shallow
        got["postings"] = None
        if got["kind"] != "range" and got.get("values") is not None:
            column, join = COLUMNS[field]
            counts = got["values"]
            narrow = {v: c for v, c in counts.items() if c <= filters_mod.MAX_IDS}
            rows = sum(narrow.values())
            pcon = filters_mod.connect(Path(index_dir))
            try:
                if rows > POSTINGS_ROWS_BOUND:
                    filters_mod.clear_postings(pcon, field)
                    got["postings"] = {"built": False,
                                       "why": f"{rows} postings for its {len(narrow)} filterable values, more than "
                                              f"the {POSTINGS_ROWS_BOUND} a field's index may reach on this peer"}
                else:
                    n = filters_mod.write_postings(
                        pcon, field, _narrow_postings(con, column, join, got["kind"], got["separator"], counts,
                                                      filters_mod.MAX_IDS, depth=got.get("depth")),
                        snapshot_id=snapshot_id, documents=got["documents"])
                    got["postings"] = {"built": True, "rows": n, "values": len(narrow),
                                       "broad": len(counts) - len(narrow)}
            finally:
                pcon.close()
        elif got["kind"] != "range":
            pcon = filters_mod.connect(Path(index_dir))
            try:
                filters_mod.clear_postings(pcon, field)
            finally:
                pcon.close()
            got["postings"] = {"built": False, "why": got.get("why")}
        got.update({"snapshot_id": snapshot_id, "built_at": time.time() if now is None else now})
        _write_cache(index_dir, field, got)
        written.append(field)
        if log:
            if got.get("values") is None and got["kind"] != "range":
                log(f"facets: {field}: {got.get('why')}")
            elif got["postings"] and not got["postings"]["built"]:
                log(f"facets: {field}: {len(got['values'])} values counted; postings not built — "
                    f"{got['postings']['why']}")
    return written


def cached(index_dir: Path, field: str, *, snapshot_id: str | None) -> dict[str, Any] | None:
    p = _cache_path(index_dir, field)
    if not p.is_file():
        return None
    try:
        got = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return got if got.get("snapshot_id") == snapshot_id else None


def fields(con, codex, index_dir: Path, *, snapshot_id: str | None) -> dict[str, Any]:
    """`ask facets <collection>`: the fields and how many values each has
    (from the cache where there is one; `null` where not built yet, or
    not countable — then `why` says so)."""
    out = []
    for f in declared(codex):
        got = cached(index_dir, f["field"], snapshot_id=snapshot_id)
        n = None
        why = None
        if got is not None:
            if f["kind"] == "range":
                n = 2 if got.get("bounds", [None])[0] else 0
            elif got.get("values") is None:
                why = got.get("why")
            else:
                n = len(got.get("values") or {})
        entry = {**f, "values": n, "built": got is not None, "in_manifest": f["field"] in COLUMNS}
        if why:
            entry["why"] = why
        if got is not None and got.get("postings") is not None:
            entry["filterable"] = bool(got["postings"].get("built"))
        out.append(entry)
    return {"collection_id": codex.collection_id, "snapshot_id": snapshot_id, "fields": out,
            "note": "counts are this peer's: what it holds and can search"}


def values(con, codex, index_dir: Path, field: str, *, snapshot_id: str | None, under: str | None = None,
           documents: int | None = None, shard_dir: Path | None = None, snapshot=None) -> dict[str, Any]:
    """`ask facets <collection> <field> [--under VALUE]`: the values with this
    peer's counts, from the cache for this snapshot or counted now when the
    collection is small enough; `under` keeps one subtree of a tree facet
    (its own count first). A range facet is listed as its bounds, read from
    the record parts held (`shard_dir`, `snapshot`)."""
    got = cached(index_dir, field, snapshot_id=snapshot_id)
    if got is None:
        f = facet_of(codex, field)
        if f["kind"] == "range":
            held = [s for s in (snapshot.shards if snapshot is not None else ())
                    if shard_dir is not None and (Path(shard_dir) / s.path).is_file()]
            if not held:
                raise FacetError(f"{field!r}: the manifest carries no column for it; its values are read "
                                 f"from the record parts, and none is held here")
            n = sum(int(s.records) for s in held)
            if n > BUILD_BOUND:
                raise NotBuilt(f"{field!r} over {n} records is read by the node's index maintainer, "
                               f"not on request; ask again when it has run")
            build(con, codex, index_dir, snapshot_id=snapshot_id, fields=[field], shard_dir=shard_dir,
                  snapshot=snapshot)
            got = cached(index_dir, field, snapshot_id=snapshot_id)
            got.setdefault("note", "counts are this peer's: what it holds and can search")
            return got
        if field not in COLUMNS:
            raise FacetError(f"{field!r}: the manifest carries no column for it yet (05 §5.7)")
        n = documents if documents is not None else con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if n > BUILD_BOUND:
            raise NotBuilt(f"{field!r} over {n} documents is counted by the node's index maintainer, "
                           f"not on request; ask again when it has run")
        build(con, codex, index_dir, snapshot_id=snapshot_id, fields=[field])
        got = cached(index_dir, field, snapshot_id=snapshot_id) or count(con, codex, field)
        del f
    if got.get("kind") != "range" and got.get("values") is None:
        got.setdefault("note", "counts are this peer's: what it holds and can search")
        return got
    if under is not None and got.get("kind") == "tree":
        sep = got.get("separator") or "/"
        vals = got.get("values") or {}
        got = {**got, "under": under,
               "values": {k: v for k, v in vals.items() if k == under or k.startswith(under + sep)}}
    if "values" in got and got["values"] is not None:
        got["values"] = dict(sorted(got["values"].items(), key=lambda kv: (-kv[1], kv[0])))
    got.setdefault("note", "counts are this peer's: what it holds and can search")
    return got
