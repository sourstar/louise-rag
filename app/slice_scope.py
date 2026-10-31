"""WHAT A NODE HOLDS IS WHAT IT PROCESSES (operator, 2026-10-31, on wikidata:
"As for other collections the grow profile should drive what's fetched and
processed"; docs/notes/wikidata-family-slices.md §9).

A Wikipedia language is its own dump: a node that holds Italian fetches
itwiki and processes nothing else, because nothing else is in its manifest.
Wikidata's every family comes in ONE dump, so its manifest lists every
entity whatever the node holds, and the stages would index and embed all of
them. Here the collection file's `slices` (07 §7.8: `family=person`, …)
narrow `documents.in_scope` to the documents of the slices held: a document
outside them is neither indexed nor embedded on this node. Its record is
untouched (invariant 11: the records stay whole; this is what the node
PROCESSES).

A document's slice is known without parsing it (`slicing.slice_hint`: the
byte the pack keeps for each entity's family). The pass walks the manifest
in rowid windows from a cursor kept in the control table, each window one
short write that also advances the cursor (invariant 2: the mark with the
write). It runs on the scan thread after discovery, resumes where it
stopped, and is redone whole when what it was computed for changes — the
slices held, the family map, the dump (a new dump re-places every entity).
Until it has caught up, `ready` says no and the collection's pipeline
waits: nothing outside the holding is processed meanwhile.

Measure on the node before widening: one Python call a row (~76 M rows for
wikidata), bound by SQLite's single writer; if the computation shows as a
core at 100 % (`docker top`), the slice lookups go to the parse pool.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from . import db

KEY = "slice_scope"
WINDOW = 50_000
#: plugins whose every slice comes from one origin unit, so the manifest
#: holds what the node does not: the only ones narrowed here
ONE_ORIGIN = ("wikidata",)


def held(collection) -> tuple[str, ...] | None:
    """The slices whose documents this node processes, or None for all of
    them (the file holds every slice, or its plugin's origin divides by
    slice already)."""
    if getattr(collection, "plugin", None) not in ONE_ORIGIN:
        return None
    slices = getattr(collection, "slices", None)
    if slices == "all" or slices is None:
        return None
    return tuple(sorted(str(s) for s in slices))


def is_held(slice_name: str | None, slices: tuple[str, ...]) -> bool:
    """`slice_name` is one of `slices` or lies under one (`family=person/
    range=3` under `family=person`). An unknown slice is not held: the pass
    fails closed."""
    if not slice_name:
        return False
    return any(slice_name == s or slice_name.startswith(s + "/") for s in slices)


def signature(slices: tuple[str, ...], context: Any) -> str:
    """What the in_scope column is computed for: the slices, the family map
    and the dump. Any of them changing redoes the pass from the start."""
    fmap = getattr(context, "fmap", None)
    dump = getattr(context, "dump", None)
    return json.dumps({"slices": list(slices), "map": getattr(fmap, "sha256", None),
                       "dump": getattr(dump, "name", None)}, sort_keys=True)


def _state(con) -> dict:
    try:
        return json.loads(db.get_control(con, KEY) or "{}")
    except (TypeError, ValueError):
        return {}


def _max_rowid(con) -> int:
    return int(con.execute("SELECT COALESCE(MAX(rowid), 0) FROM documents").fetchone()[0])


def ready(con, collection) -> bool:
    """May this collection's stages select documents now? True when nothing
    is narrowed, or when the pass has caught up with the manifest for the
    slices the file holds now. Cheap: two lookups, no walk."""
    slices = held(collection)
    if slices is None:
        return True
    state = _state(con)
    if json.loads(state.get("signature") or "{}").get("slices") != list(slices):
        return False
    return bool(state.get("done")) and int(state.get("cursor", 0)) >= _max_rowid(con)


def invalidate(con) -> None:
    """In-scope was rewritten by something else (`discovery.apply_scope`
    widens every row): the pass starts again from the first row."""
    state = _state(con)
    if state:
        db.set_control(con, KEY, json.dumps({**state, "cursor": 0, "done": False}))


def apply(con, collection, *, stop=None, log: Callable[[str], None] | None = None,
          context_for=None, slice_hint=None) -> dict:
    """Bring in_scope in line with the slices held: resumable, in windows.
    Returns what it did — {"skipped"}, {"deferred": reason}, or the counts.
    `context_for`/`slice_hint`: `slicing`'s, replaceable in tests."""
    from .pack import slicing
    context_for = context_for or slicing.context_for
    # the parse pool computes the hints (multi-core rule, 2026-10-30): the
    # first pass ran on one core, 75.9 M rows in 954 s (München, 10-31)
    pooled = slice_hint is None
    slice_hint = slice_hint or slicing.slice_hint
    say = log or (lambda m: None)
    slices = held(collection)
    if slices is None:
        return {"skipped": "the node processes every slice"}
    try:
        context = context_for(collection, log=say, stop=stop)
    except slicing.ContextUnavailable as e:
        # deferral, not failure: the stages wait (`ready` is False) and the
        # next scan tries again
        return {"deferred": str(e)}
    sig = signature(slices, context)
    state = _state(con)
    if state.get("signature") != sig:
        state = {"signature": sig, "cursor": 0, "done": False}
    # a held document narrowed by this pass before (the holding grew) is
    # widened back, unless the scope config narrows something itself
    widen = _default_narrowing(con)
    cursor = int(state.get("cursor", 0))
    counts = {"held": 0, "not_held": 0, "unknown": 0, "rows": 0}
    while True:
        if stop is not None and stop.is_set():
            return {**counts, "interrupted": True, "cursor": cursor}
        rows = con.execute("SELECT rowid, native_id, path, in_scope FROM documents "
                           "WHERE rowid > ? ORDER BY rowid LIMIT ?", (cursor, WINDOW)).fetchall()
        if not rows:
            break
        out, back = [], []
        names = _hints(collection, rows, context, slice_hint, pooled)
        for r, name in zip(rows, names):
            if name is None:
                counts["unknown"] += 1
            if is_held(name, slices):
                counts["held"] += 1
                if widen and not r["in_scope"]:
                    back.append(r["rowid"])
            else:
                counts["not_held"] += 1
                if r["in_scope"]:
                    out.append(r["rowid"])
        last = rows[-1]["rowid"]
        con.execute("BEGIN IMMEDIATE")
        try:
            for ids, value in ((out, 0), (back, 1)):
                for i in range(0, len(ids), 900):
                    part = ids[i:i + 900]
                    con.execute(f"UPDATE documents SET in_scope={value} WHERE rowid IN "
                                f"({','.join('?' * len(part))})", part)
            state = {**state, "cursor": last, "done": False}
            db.set_control(con, KEY, json.dumps(state))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        cursor = last
        counts["rows"] += len(rows)
    db.set_control(con, KEY, json.dumps({**state, "cursor": cursor, "done": True}))
    if counts["rows"]:
        say(f"slice scope: {counts['held']} documents in the slices held, {counts['not_held']} "
            f"out of them ({counts['unknown']} whose slice is not known yet)")
    return counts


def _hints(collection, rows, context, slice_hint, pooled: bool) -> list[str | None]:
    """Each row's slice: in the parse pool's children where there is a pool
    (each child builds the family context once, `parallel._one`), in this
    process otherwise and for any row a child could not place."""
    from . import parallel
    if pooled and parallel.pool() is not None:
        got = parallel.compute("slice-hint", collection,
                               [{"native_id": r["native_id"], "path": r["path"]} for r in rows])
        return [v if ok else slice_hint(collection.plugin, r, context)
                for (ok, v), r in zip(got, rows)]
    return [slice_hint(collection.plugin, r, context) for r in rows]


def _default_narrowing(con) -> bool:
    """The scope config narrows nothing but its embed mode (which is not a
    narrowing of in_scope): a held document may be widened back."""
    scope = db.get_scope(con)
    return not any(scope.get(k) for k in ("languages", "book_ids", "limit", "sample_per_lang"))
