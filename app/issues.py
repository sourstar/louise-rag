"""Inconsistencies in the ORIGIN, logged and flagged (06 §6.1, amended
2026-10-25, operator: "Make the parsers such that on a regular basis any
inconsistency is logged and flagged").

A parse reports what it met as `Issue`s beside the record, never in it
(app/plugins/base.py). Whoever runs the parse — `hold records build` for
every record it parses, the worker's rdf stage for every document it
extracts — hands them here, and they land in the collection's manifest,
table `issues`, keyed (uid, code):

  * per document, the codes the parse CHECKED are replaced (delete, then
    insert): a re-parse of a fixed origin clears them, and a parse that
    checks fewer codes (gutenberg's rdf stage reads only the catalogue
    entry) leaves the others alone;
  * an issue is logged ONCE, when it is first recorded, with the
    collection's name (db.log_event); its clearing is logged once too;
  * the counts per code are flagged in `hold collection show`, `node
    health` and the Library's card, and `hold issues list` lists them.

Local state (invariant 6): never travels, in no record, moves no hash.

The cost is the work's, never the corpus's: one primary-key read for a
whole batch of documents (`uid IN (…)`), and writes only for a document
that has, or had, an issue.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable, Iterator

from .plugins.base import Issue

#: Codes the PLATFORM reports, for every plugin whose collection has the
#: condition — `hold records build` checks them, not a plugin's parse.
PLATFORM: dict[str, str] = {
    "cutoff-undated": ("the record has no date its collection's cutoff rule reads (06 §6.2): "
                       "a dated snapshot cannot place it and excludes it"),
}

#: The platform's codes are checked by the build alone: a parse elsewhere
#: never clears them.
BUILD_ONLY = frozenset(PLATFORM)

DETAIL_MAX = 500
#: SQLite's bound on host parameters is 32,766 since 3.32; a batch stays far below
_IN_CHUNK = 900

LIST_SQL = ("SELECT uid, code, detail, first_seen, last_seen FROM issues "
            "ORDER BY uid, code")
LIST_BY_CODE_SQL = ("SELECT uid, code, detail, first_seen, last_seen FROM issues "
                    "WHERE code=? ORDER BY uid")
COUNTS_SQL = "SELECT code, COUNT(*) FROM issues GROUP BY code ORDER BY code"


def declared(plugin) -> dict[str, str]:
    """Every code a collection of this plugin may carry, with its meaning."""
    return {**PLATFORM, **dict(getattr(plugin, "ISSUES", None) or {})}


def pairs(found: Iterable) -> list[tuple[str, str]]:
    """`Issue`s (or `(code, detail)` pairs) as `(code, detail)` pairs."""
    out = []
    for i in found or ():
        if isinstance(i, Issue):
            out.append((i.code, i.detail))
        else:
            code, detail = i
            out.append((str(code), str(detail or "")))
    return out


def _merge(found: Iterable[tuple[str, str]]) -> dict[str, str]:
    """{code: detail}, one per code: a code met twice keeps both details."""
    out: dict[str, str] = {}
    for code, detail in found:
        detail = " ".join(str(detail or "").split())
        out[code] = (f"{out[code]}; {detail}" if code in out and detail else out.get(code) or detail)
    return {c: (d if len(d) <= DETAIL_MAX else d[:DETAIL_MAX - 1] + "…") for c, d in out.items()}


def _held(con, uids: list[str]) -> dict[str, dict[str, tuple[str, int]]]:
    """{uid: {code: (detail, first_seen)}} for the uids that have any."""
    out: dict[str, dict[str, tuple[str, int]]] = {}
    for k in range(0, len(uids), _IN_CHUNK):
        part = uids[k:k + _IN_CHUNK]
        marks = ",".join("?" * len(part))
        for uid, code, detail, first in con.execute(
                f"SELECT uid, code, detail, first_seen FROM issues WHERE uid IN ({marks})", part):
            out.setdefault(uid, {})[code] = (detail, first)
    return out


def record_many(con, items: Iterable[tuple[str, Iterable]], *, codes: dict[str, str],
                checked: Iterable[str] | None = None, now: int | None = None) -> dict:
    """Record what one parse of each document met.

    `items`: `(uid, issues)` for every document PARSED — one with no issue
    too, since its empty list is what clears a fixed origin's rows.
    `codes`: the declared codes (`declared(plugin)`); an issue outside them
    is a plugin defect and raises before anything is written.
    `checked`: the codes this parse examined — its issues are replaced, any
    other code a document holds is left alone; None means every code.

    Runs inside the caller's transaction when there is one (the rdf stage's,
    which commits it with the marks), else in one `BEGIN IMMEDIATE` of its
    own. Returns {"new": n, "cleared": n, "documents": n}."""
    now = int(time.time()) if now is None else int(now)
    scope = None if checked is None else frozenset(checked)
    batch: list[tuple[str, dict[str, str]]] = []
    for uid, found in items:
        merged = _merge(pairs(found))
        for code in merged:
            if code not in codes:
                raise ValueError(f"{uid}: issue code {code!r} is not declared by its plugin "
                                 f"({', '.join(sorted(codes)) or 'none'}) — 06 §6.1")
            if scope is not None and code not in scope:
                raise ValueError(f"{uid}: issue code {code!r} reported by a parse that "
                                 f"checks only {sorted(scope)}")
        batch.append((str(uid), merged))
    if not batch:
        return {"new": 0, "cleared": 0, "documents": 0}
    own = not con.in_transaction
    if own:
        con.execute("BEGIN IMMEDIATE")
    try:
        try:
            held = _held(con, [uid for uid, _ in batch])
        except sqlite3.OperationalError as e:
            if "no such table" not in str(e):
                raise
            raise RuntimeError("this manifest has no `issues` table; open it once with "
                               "db.connect (an additive table, 07 §7.3)") from e
        new = cleared = touched = 0
        from . import db
        for uid, merged in batch:
            old = held.get(uid, {})
            if not old and not merged:
                continue
            touched += 1
            gone = [c for c in old if (scope is None or c in scope)]
            if gone:
                con.executemany("DELETE FROM issues WHERE uid=? AND code=?",
                                [(uid, c) for c in gone])
            if merged:
                con.executemany(
                    "INSERT INTO issues(uid, code, detail, first_seen, last_seen) VALUES(?,?,?,?,?)",
                    [(uid, c, d, old[c][1] if c in old else now, now) for c, d in merged.items()])
            for c, d in merged.items():
                if c not in old:
                    new += 1
                    db.log_event(con, "warn", "issue", f"origin inconsistency {c}: {d}", uid)
            for c in gone:
                if c not in merged:
                    cleared += 1
                    db.log_event(con, "info", "issue", f"origin inconsistency {c} cleared: "
                                 f"the last parse no longer meets it", uid)
        if own:
            con.execute("COMMIT")
    except BaseException:
        if own and con.in_transaction:
            con.execute("ROLLBACK")
        raise
    return {"new": new, "cleared": cleared, "documents": touched}


def counts(con) -> dict[str, int]:
    """{code: documents}, from the code index alone — a cost in issues, not
    in documents. {} on a manifest that has no table yet (a reader never
    creates one)."""
    try:
        return {code: int(n) for code, n in con.execute(COUNTS_SQL)}
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return {}
        raise


def listing(con, *, code: str | None = None, limit: int | None = None) -> Iterator[dict]:
    """The recorded issues, by uid — one code's through its index."""
    sql, params = (LIST_BY_CODE_SQL, (code,)) if code else (LIST_SQL, ())
    if limit is not None:
        sql, params = sql + " LIMIT ?", (*params, int(limit))
    try:
        rows = con.execute(sql, params)
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return
        raise
    for uid, c, detail, first, last in rows:
        yield {"uid": uid, "code": c, "detail": detail, "first_seen": first, "last_seen": last}


def open_read(manifest_path) -> sqlite3.Connection | None:
    """A read-only connection to a manifest, or None when there is none —
    `show`, `list` and `health` never take the lock and never create."""
    from pathlib import Path
    p = Path(manifest_path)
    if not p.is_file():
        return None
    return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def summary_at(manifest_path, plugin=None) -> dict:
    """`summary` of the manifest at a path; the empty summary without one."""
    con = open_read(manifest_path)
    if con is None:
        return {"total": 0, "by_code": {}}
    try:
        return summary(con, plugin)
    finally:
        con.close()


def summary(con, plugin=None) -> dict:
    """What `hold collection show` prints: the total, the documents per
    code, and each code's meaning where the plugin declares it."""
    got = counts(con)
    meaning = declared(plugin) if plugin is not None else dict(PLATFORM)
    # `total` counts (document, code) pairs: a document flagged twice is two
    return {"total": sum(got.values()),
            "by_code": {c: {"documents": n, "meaning": meaning.get(c, "")}
                        for c, n in got.items()}}
