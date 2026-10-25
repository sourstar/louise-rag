"""Facet filters (05 §5.2, §5.7): a value given for one of the collection's
declared facets RESTRICTS which records the legs may return; the free-text
query then ranks within that narrowing. Nothing boosts. Migration step 11.

A filter is `{<facet field>: <value> | {"not": <value>}}`. It resolves,
per collection, to the set of records whose facet holds the value — a
flat facet by the value itself, a tree facet by the value or any path
under it — read from the postings the facet index keeps beside the
counts (`index/facets/postings.sqlite`, built by the maintainer per
snapshot for a collection the counts are built for). The legs take the
set as a prefilter (`book_ids`); a filter that matches nothing returns
nothing, which is an answer (05 §5.2). A filter over a facet the Codex
does not declare is refused; one over a collection whose postings are
not built yet is refused too, saying so; a set too large to hand the
legs is refused rather than silently cut.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.hold.codex import CodexError

POSTINGS = "postings.sqlite"
MAX_IDS = 20_000
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS postings (field TEXT NOT NULL, value TEXT NOT NULL, native_id INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_postings_value ON postings(field, value, native_id)",
    "CREATE TABLE IF NOT EXISTS built (field TEXT PRIMARY KEY, snapshot_id TEXT, documents INTEGER)",
)


#: 05 §5.2 (amended 2026-10-25): the identifier filter. Not a facet a Codex
#: declares: every collection has it, and admits under it the records tagged
#: with the identifier (the manifest's `doc_keys`), none where nothing is
KEYS = "keys"


class FilterError(CodexError):
    exit_code = 2


def _tagged_ids(tagged, value: str, negate: bool) -> set[int]:
    """The records tagged with identifier `value`, bounded as a facet
    value's postings are: more than MAX_IDS is refused, never cut.
    `tagged(value, limit)` gives (how many, up to `limit`; their ids)."""
    count, ids = tagged(value, MAX_IDS + 1) if tagged is not None else (0, [])
    if count > MAX_IDS:
        said = (f"excluding {KEYS!r} = {value!r} needs more than {MAX_IDS} records" if negate
                else f"{KEYS!r} = {value!r} admits more than {MAX_IDS} records")
        raise FilterError(f"{said}, the most the legs take as a prefilter; narrow it (05 §5.2)")
    return set(ids)


def connect(index_dir: Path) -> sqlite3.Connection:
    p = Path(index_dir) / "facets" / POSTINGS
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def write_postings(pcon, field: str, rows, *, snapshot_id: str | None, documents: int) -> int:
    """`rows`: (value, native_id) pairs, every value of every record; the
    field's postings replaced whole (delete-then-insert, the mark last)."""
    pcon.execute("BEGIN IMMEDIATE")
    try:
        pcon.execute("DELETE FROM postings WHERE field=?", (field,))
        n = 0
        batch = []
        for value, native_id in rows:
            batch.append((field, value, int(native_id)))
            if len(batch) >= 5000:
                pcon.executemany("INSERT INTO postings(field, value, native_id) VALUES(?,?,?)", batch)
                n += len(batch)
                batch.clear()
        if batch:
            pcon.executemany("INSERT INTO postings(field, value, native_id) VALUES(?,?,?)", batch)
            n += len(batch)
        pcon.execute("INSERT OR REPLACE INTO built(field, snapshot_id, documents) VALUES(?,?,?)",
                     (field, snapshot_id, documents))
        pcon.execute("COMMIT")
    except BaseException:
        pcon.execute("ROLLBACK")
        raise
    return n


def clear_postings(pcon, field: str) -> None:
    """The field's postings and its mark gone — what a build that could not
    write them leaves, so an older snapshot's rows never answer for it."""
    pcon.execute("BEGIN IMMEDIATE")
    try:
        pcon.execute("DELETE FROM postings WHERE field=?", (field,))
        pcon.execute("DELETE FROM built WHERE field=?", (field,))
        pcon.execute("COMMIT")
    except BaseException:
        pcon.execute("ROLLBACK")
        raise


def built_for(pcon, field: str) -> str | None:
    r = pcon.execute("SELECT snapshot_id FROM built WHERE field=?", (field,)).fetchone()
    return r["snapshot_id"] if r else None


def _ids(pcon, field: str, value: str, *, tree_sep: str | None) -> set[int]:
    out = {r[0] for r in pcon.execute("SELECT native_id FROM postings WHERE field=? AND value=?", (field, value))}
    if tree_sep:
        prefix = value + tree_sep
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        out |= {r[0] for r in pcon.execute(
            "SELECT native_id FROM postings WHERE field=? AND value LIKE ? ESCAPE '\\'", (field, like))}
    return out


def book_ids_for(codex, index_dir: Path, filters: dict[str, Any] | None, *, snapshot_id: str | None,
                 all_ids=None, tagged=None) -> set[int] | None:
    """The records the filters admit — None for no filter, a set (possibly
    empty) otherwise. `all_ids` is a callable giving every record's
    native_id, needed for a `not` alone. `tagged(identifier, limit)` gives
    how many records an identifier tags (up to `limit`) and their ids, for
    the `keys` filter. A value's COUNT, from the facet
    cache, is read before its postings: the postings hold only values of
    at most MAX_IDS records (app/ask/facets.py), so a broader value is
    refused here as too broad, and the postings read are complete."""
    if not filters:
        return None
    from app.ask import facets
    declared = {f["field"]: f for f in facets.declared(codex)}
    keep: set[int] | None = None
    drop: set[int] = set()
    pcon = connect(index_dir)
    try:
        for field, spec in filters.items():
            if field == KEYS:
                negate = isinstance(spec, dict) and "not" in spec
                ids = _tagged_ids(tagged, str(spec["not"]) if negate else str(spec), negate)
                if negate:
                    drop |= ids
                else:
                    keep = ids if keep is None else keep & ids
                continue
            f = declared.get(field)
            if f is None:
                raise FilterError(f"{field!r} is not a facet of this collection (05 §5.7); it declares "
                                  f"{', '.join(declared) or 'none'}")
            if f["kind"] == "range":
                raise FilterError(f"{field!r}: a range facet filters by bounds, which is not built yet")
            cache = facets.cached(index_dir, field, snapshot_id=snapshot_id)
            built = built_for(pcon, field)
            if built is None or (snapshot_id is not None and built != snapshot_id) or cache is None:
                why = ""
                if cache is not None and cache.get("values") is None:
                    why = f": {cache.get('why')}"
                elif cache is not None and (cache.get("postings") or {}).get("why"):
                    why = f": {cache['postings']['why']}"
                raise FilterError(f"{field!r}: its values are not indexed on this peer for snapshot "
                                  f"{snapshot_id}{why or ' yet; the node’s index maintainer builds them'}")
            negate = isinstance(spec, dict) and "not" in spec
            value = str(spec["not"]) if negate else str(spec)
            sep = f.get("separator") if f["kind"] == "tree" else None
            depth = cache.get("depth")
            if depth is not None and sep and len(value.split(sep)) > depth:
                raise FilterError(f"{field!r}: counted to depth {depth} on this peer ({cache.get('note')}); "
                                  f"{value!r} is deeper — filter by {sep.join(value.split(sep)[:depth])!r}")
            total = (cache.get("values") or {}).get(value, 0)
            if total > MAX_IDS:
                # an exclusion names the value it removes: "'lang' = 'en' admits" read, for
                # {"not": "en"}, as if the filter had asked for English (query campaign, 2026-10-20)
                said = (f"excluding {field!r} = {value!r} needs its {total} records" if negate
                        else f"{field!r} = {value!r} admits {total} records")
                raise FilterError(f"{said}, more than the {MAX_IDS} the legs take as a prefilter; "
                                  f"narrow it (05 §5.2)")
            ids = _ids(pcon, field, value, tree_sep=sep)
            if negate:
                drop |= ids
            else:
                keep = ids if keep is None else keep & ids
    finally:
        pcon.close()
    if keep is None:
        if all_ids is None:
            raise FilterError("a filter made only of exclusions needs every record's id to subtract from")
        keep = set(all_ids())
    keep -= drop
    if len(keep) > MAX_IDS:
        raise FilterError(f"the filter admits {len(keep)} records, more than the {MAX_IDS} the legs take as a "
                          f"prefilter; narrow it (05 §5.2)")
    return keep
