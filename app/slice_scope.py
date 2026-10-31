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

HOLD ALL, EMBED SOME (operator, 2026-10-31: "hold all 14 and embed 9"):
the file's `embed_slices` names what the encoder spends on, out of what is
held. A document outside them is DECLINED (`embed_done = -1`): out of the
embed stage's candidates — whose index prefix is embed_done = 0, so it
costs no skip per round — never counted as embedded (every count reads
`= 1`), given back (0) when the list grows. One already embedded keeps its
vectors.

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
#: `documents.embed_done` of a document the node does not embed by choice
#: (07 §7.8 `embed_slices`): out of the encoder's candidates (their index
#: prefix is embed_done = 0), never counted as embedded (`= 1`)
DECLINED = -1
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


def embedded(collection) -> tuple[str, ...] | None:
    """The slices the node's encoder spends on (07 §7.8 `embed_slices`;
    operator, 2026-10-31: wikidata "hold all 14 and embed 9"), or None for
    every slice held. Any plugin: a slice is a slice."""
    got = getattr(collection, "embed_slices", None)
    return None if got is None else tuple(sorted(str(s) for s in got))


def signature(slices: tuple[str, ...] | None, embed: tuple[str, ...] | None,
              context: Any) -> str:
    """What the columns are computed for: the slices held (in_scope), the
    slices embedded (embed_done -1), the family map and the dump. Any of
    them changing redoes the pass from the start."""
    fmap = getattr(context, "fmap", None)
    dump = getattr(context, "dump", None)
    return json.dumps({"slices": list(slices) if slices is not None else None,
                       "embed": list(embed) if embed is not None else None,
                       "map": getattr(fmap, "sha256", None),
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
    is narrowed and nothing was, or when the pass has caught up with the
    manifest for what the file says now. Cheap: two lookups, no walk."""
    slices, embed = held(collection), embedded(collection)
    state = _state(con)
    if slices is None and embed is None and not state.get("signature"):
        return True
    sig = json.loads(state.get("signature") or "{}")
    if sig.get("slices") != (list(slices) if slices is not None else None) \
            or sig.get("embed") != (list(embed) if embed is not None else None):
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
    """Bring in_scope in line with the slices held, and embed_done with the
    slices embedded (-1: "not embedded here", out of the encoder's
    candidates; back to 0 when the list grows; a document already embedded
    keeps its vectors): resumable, in windows. With neither narrowed and a
    narrowing left from before, it is unwound and the state removed.
    Returns what it did — {"skipped"}, {"deferred": reason}, or the counts.
    `context_for`/`slice_hint`: `slicing`'s, replaceable in tests."""
    from .pack import slicing
    context_for = context_for or slicing.context_for
    # the parse pool computes the hints (multi-core rule, 2026-10-30): the
    # first pass ran on one core, 75.9 M rows in 954 s (München, 10-31)
    pooled = slice_hint is None
    slice_hint = slice_hint or slicing.slice_hint
    say = log or (lambda m: None)
    slices, embed = held(collection), embedded(collection)
    state = _state(con)
    unwind = slices is None and embed is None
    if unwind and not state.get("signature"):
        return {"skipped": "the node processes and embeds every slice it holds"}
    context = None
    if not unwind:
        try:
            context = context_for(collection, log=say, stop=stop)
        except slicing.ContextUnavailable as e:
            # deferral, not failure: the stages wait (`ready` is False) and
            # the next scan tries again
            return {"deferred": str(e)}
    sig = signature(slices, embed, context)
    if state.get("signature") != sig:
        state = {"signature": sig, "cursor": 0, "done": False}
    # a held document narrowed by this pass before (the holding grew) is
    # widened back, unless the scope config narrows something itself
    widen = _default_narrowing(con)
    cursor = int(state.get("cursor", 0))
    counts = {"held": 0, "not_held": 0, "embedded": 0, "not_embedded": 0, "unknown": 0, "rows": 0}
    while True:
        if stop is not None and stop.is_set():
            return {**counts, "interrupted": True, "cursor": cursor}
        rows = con.execute("SELECT rowid, native_id, path, in_scope, embed_done FROM documents "
                           "WHERE rowid > ? ORDER BY rowid LIMIT ?", (cursor, WINDOW)).fetchall()
        if not rows:
            break
        moves: dict[tuple[str, int, int], list[int]] = {}
        names = [None] * len(rows) if unwind else _hints(collection, rows, context, slice_hint, pooled)
        for r, name in zip(rows, names):
            if name is None and not unwind:
                counts["unknown"] += 1
            if slices is None or is_held(name, slices):
                counts["held"] += 1
                if widen and not r["in_scope"]:
                    moves.setdefault(("in_scope", 0, 1), []).append(r["rowid"])
            else:
                counts["not_held"] += 1
                if r["in_scope"]:
                    moves.setdefault(("in_scope", 1, 0), []).append(r["rowid"])
            if embed is None or is_held(name, embed):
                counts["embedded"] += 1
                if r["embed_done"] == DECLINED:
                    moves.setdefault(("embed_done", DECLINED, 0), []).append(r["rowid"])
            else:
                counts["not_embedded"] += 1
                if r["embed_done"] == 0:
                    moves.setdefault(("embed_done", 0, DECLINED), []).append(r["rowid"])
        last = rows[-1]["rowid"]
        con.execute("BEGIN IMMEDIATE")
        try:
            for (column, was, value), ids in moves.items():
                for i in range(0, len(ids), 900):
                    part = ids[i:i + 900]
                    # the column's value is tested again under the write lock:
                    # a stage that moved it since the read keeps its move.
                    # `+column`: the test is no index term (SQLite's unary +),
                    # so the rowids lead. Without it and without ANALYZE stats,
                    # `in_scope=?` took idx_documents_scope and every window
                    # walked every row of that value: wikidata's pass widening
                    # 33.9 M rows ran at ~430 rows/s, 49 h (München, 2026-10-31)
                    con.execute(f"UPDATE documents SET {column}=? WHERE rowid IN "
                                f"({','.join('?' * len(part))}) AND +{column}=?", (value, *part, was))
            state = {**state, "cursor": last, "done": False}
            db.set_control(con, KEY, json.dumps(state))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        cursor = last
        counts["rows"] += len(rows)
    if unwind:
        # nothing narrowed any more: the state goes, and `ready` is True again
        con.execute("DELETE FROM control WHERE key=?", (KEY,))
    else:
        db.set_control(con, KEY, json.dumps({**state, "cursor": cursor, "done": True}))
    if counts["rows"]:
        say(f"slice scope: {counts['held']} held, {counts['not_held']} not held; "
            f"{counts['embedded']} embedded here, {counts['not_embedded']} not "
            f"({counts['unknown']} whose slice is not known yet)")
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
