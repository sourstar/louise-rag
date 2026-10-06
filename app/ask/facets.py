"""`ask facets <collection> [<field>] [--under VALUE]` (05 §5.7; 07 §7.3) —
the fields a collection can be filtered on, declared in its Codex, and
the values this peer holds for each, with this peer's counts. Migration
step 11. A listing, not a search: nothing is ranked.

The values are read out of the records this peer holds, through the
manifest's metadata columns — `lang`, `authors` (joined by `; `),
`subjects` (joined by `|`) — one pass per field, cached under `index/`
beside the text index for the snapshot it was built for (05 §5.7: "the
index is local and derived"). A `tree` facet splits each value on its
separator and counts every prefix path; a `range` facet lists bounds. A
field the manifest does not carry is said so, not guessed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.hold.codex import CodexError

#: facet field -> (manifest column, the join the pipeline writes)
COLUMNS = {"lang": ("lang", None), "language": ("lang", None), "languages": ("lang", None),
           "authors": ("authors", "; "), "creators": ("authors", "; "),
           "subjects": ("subjects", "|"), "title": ("title", None)}
BUILD_BOUND = 500_000            # documents: above this the maintainer builds, a request does not
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


def count(con, codex, field: str, *, where_in_scope: bool = True, postings=None) -> dict[str, Any]:
    """One pass over the manifest's column: `{field, kind, documents,
    values: {value: count}}` for flat and tree facets (a tree counts every
    prefix path), `{bounds: [min, max]}` for a range. `postings`, when
    given, receives every (value, native_id) of the pass — the filter
    index (app/ask/filters.py) built in the same pass as the counts."""
    f = facet_of(codex, field)
    column, join = COLUMNS.get(field, (None, None))
    if column is None:
        raise FacetError(f"{field!r}: the manifest carries no column for it yet; its values are in "
                         f"the records (09 §9.5) and a pass over the shards is not built")
    scope = " WHERE in_scope=1" if where_in_scope else ""
    kind, sep = f["kind"], f.get("separator")
    counts: dict[str, int] = {}
    lo = hi = None
    n = 0
    for native_id, raw in con.execute(f"SELECT native_id, {column} FROM documents{scope}"):
        n += 1
        for value in _split(raw, join):
            if postings is not None and kind != "range":
                try:
                    postings.append((value, int(native_id)))
                except (TypeError, ValueError):
                    pass
            if kind == "range":
                lo = value if lo is None or value < lo else lo
                hi = value if hi is None or value > hi else hi
                continue
            if kind == "tree" and sep:
                parts = value.split(sep)
                for i in range(1, len(parts) + 1):
                    path = sep.join(parts[:i])
                    counts[path] = counts.get(path, 0) + 1
            else:
                counts[value] = counts.get(value, 0) + 1
    out: dict[str, Any] = {"field": field, "kind": kind, "separator": sep, "authority": f.get("authority"),
                           "documents": n}
    if kind == "range":
        out["bounds"] = [lo, hi]
    else:
        out["values"] = counts
    return out


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
    snap = seeder.current_snapshot(col.drag_of(root, name))
    sid = snap.snapshot_id if snap else None
    index_dir = root / name / "index"
    if not (root / name / "state" / "manifest.sqlite").is_file():
        raise FacetError(f"{name}: no manifest yet — nothing is held to count")
    with closing(cov.manifest(root, name)) as con:
        if field is None:
            return fields(con, codex, index_dir, snapshot_id=sid)
        return values(con, codex, index_dir, field, snapshot_id=sid, under=under)


def _cache_path(index_dir: Path, field: str) -> Path:
    return Path(index_dir) / CACHE / f"{field}.json"


def build(con, codex, index_dir: Path, *, snapshot_id: str | None, fields=None, now: float | None = None) -> list[str]:
    """Count every declared facet (or `fields`) and write the cache; the
    fields written. What a maintainer calls at each snapshot."""
    from app.ask import filters as filters_mod
    wanted = [f["field"] for f in declared(codex)] if fields is None else list(fields)
    written = []
    for field in wanted:
        if field not in COLUMNS:
            continue
        postings: list = []
        got = count(con, codex, field, postings=postings)
        if got["kind"] != "range":
            pcon = filters_mod.connect(Path(index_dir))
            try:
                filters_mod.write_postings(pcon, field, postings, snapshot_id=snapshot_id,
                                           documents=got["documents"])
            finally:
                pcon.close()
        got.update({"snapshot_id": snapshot_id, "built_at": time.time() if now is None else now})
        p = _cache_path(index_dir, field)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(json.dumps(got, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        written.append(field)
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
    (from the cache where there is one; `null` where not built yet)."""
    out = []
    for f in declared(codex):
        got = cached(index_dir, f["field"], snapshot_id=snapshot_id)
        n = None
        if got is not None:
            n = len(got.get("values") or {}) if f["kind"] != "range" else (2 if got.get("bounds", [None])[0] else 0)
        out.append({**f, "values": n, "built": got is not None,
                    "in_manifest": f["field"] in COLUMNS})
    return {"collection_id": codex.collection_id, "snapshot_id": snapshot_id, "fields": out,
            "note": "counts are this peer's: what it holds and can search"}


def values(con, codex, index_dir: Path, field: str, *, snapshot_id: str | None, under: str | None = None,
           documents: int | None = None) -> dict[str, Any]:
    """`ask facets <collection> <field> [--under VALUE]`: the values with this
    peer's counts, from the cache for this snapshot or counted now when the
    collection is small enough; `under` keeps one subtree of a tree facet
    (its own count first)."""
    got = cached(index_dir, field, snapshot_id=snapshot_id)
    if got is None:
        f = facet_of(codex, field)
        if field not in COLUMNS:
            raise FacetError(f"{field!r}: the manifest carries no column for it yet (05 §5.7)")
        n = documents if documents is not None else con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if n > BUILD_BOUND:
            raise NotBuilt(f"{field!r} over {n} documents is counted by the node's index maintainer, "
                           f"not on request; ask again when it has run")
        build(con, codex, index_dir, snapshot_id=snapshot_id, fields=[field])
        got = cached(index_dir, field, snapshot_id=snapshot_id) or count(con, codex, field)
        del f
    if under is not None and got.get("kind") == "tree":
        sep = got.get("separator") or "/"
        vals = got.get("values") or {}
        got = {**got, "under": under,
               "values": {k: v for k, v in vals.items() if k == under or k.startswith(under + sep)}}
    if "values" in got:
        got["values"] = dict(sorted(got["values"].items(), key=lambda kv: (-kv[1], kv[0])))
    got.setdefault("note", "counts are this peer's: what it holds and can search")
    return got
