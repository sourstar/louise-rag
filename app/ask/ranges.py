"""Range facets (05 §5.7; 07 §7.3's `FIELD=FROM..TO`): the values a
`kind: "range"` facet holds, ordered by their type, and the records a range
of them admits. A filter, so it restricts and never ranks (05 §5.2).

THE TYPE. The Codex declares a facet's kind and never its type (09 §9.3),
so the type is the field's own: `date` is fixed by 09 §9.5 as `YYYY`,
`YYYY-MM` or `YYYY-MM-DD`; any other field (`extra.<key>`) is typed by its
values — a JSON number, or a string written as a number, is a number; a
string in one of the three date forms is a date. A field whose values are
of both types is not filterable as a range, and the facet cache says so. A
value of neither (`c. 1850`, a boolean) is counted as `unparsed`, said, and
is in no range.

A DATE IS A SPAN. `1999` is the whole of 1999, `1999-06` the whole of June:
a value is stored as `[lo, hi]`, integers `YYYYMMDD`, the calendar's own
(`datetime.date`, `calendar.monthrange`; the reduced-precision dates of
ISO 8601 and of the Library of Congress's EDTF, whose interval reading this
is). A range `FROM..TO` is `[start of FROM, end of TO]`, both ends
included, and admits a record when one of its values lies WHOLLY within
it — PostgreSQL's `<@` ("is contained by") over `daterange`, the reading
this follows: a record is admitted when its date is certainly in the range,
so `1999-06..` does not admit a record dated only `1999`. Overlap (`&&`,
"may be in the range") is the alternative; adopting it would take a second
bound on the scan (a value's span is at most a year) and would admit records
the reader did not ask for. A number is the span `[v, v]`. Compared as
numbers, never as text.

WHERE THE VALUES COME FROM. The manifest carries no `date` and no `extra`
(app/ask/facets.py COLUMNS): the values live in the records (09 §9.3), so
the facet pass reads them out of the record parts this peer holds — one
part at a time, keyed on the part's path and its root as the relation index
is (app/ask/traverse.py `index_edges`): a part already read at that root is
skipped, a re-issued one replaced whole (delete-then-insert, the mark last,
invariant 2), a part the snapshot no longer lists dropped. A Parquet part
gives the one column (`records.iter_fields`); a frame part is read and cut.

THE RESOLVING QUERY reads `idx_range_lo (field, lo, hi, native_id)` — a
covering index, the scan bounded on `lo` by the ends given (`lo <= TO`
is implied by containment and stated for that) — and stops after `limit +
1` distinct records: its cost is the records the range admits up to that
bound, plus the values straddling TO, never the size of the field
(tests/ask/test_ranges.py checks the plan).
"""
from __future__ import annotations

import calendar
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

#: the fields whose type 09 §9.5 fixes; every other range facet is typed by its values
DATE_FIELDS = frozenset({"date"})
DATE, NUMBER = "date", "number"
_DATE = re.compile(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
#: what a value for a range facet may look like at all, before its type is
#: known: a value given to a flat or tree facet in this shape is refused as
#: a range rather than looked up as a literal
_RANGE_SHAPE = re.compile(r"\s*([0-9+\-.eE]*?)\s*\.\.\s*([0-9+\-.eE]*)\s*")

_SCHEMA = (
    ("CREATE TABLE IF NOT EXISTS range_postings (field TEXT NOT NULL, lo NUMERIC NOT NULL, "
     "hi NUMERIC NOT NULL, native_id INTEGER NOT NULL, part INTEGER NOT NULL)"),
    "CREATE INDEX IF NOT EXISTS idx_range_lo ON range_postings(field, lo, hi, native_id)",
    "CREATE INDEX IF NOT EXISTS idx_range_part ON range_postings(part)",
    # one row per (field, part) read: its root, and what it held
    ("CREATE TABLE IF NOT EXISTS range_parts (id INTEGER PRIMARY KEY, field TEXT NOT NULL, part TEXT NOT NULL, "
     "root TEXT NOT NULL, records INTEGER NOT NULL, rows INTEGER NOT NULL, unparsed INTEGER NOT NULL, "
     "type TEXT, lo_min NUMERIC, lo_text TEXT, hi_max NUMERIC, hi_text TEXT, UNIQUE(field, part))"),
)

ACCEPTS = ("a range facet takes FROM..TO, both ends included, either one omitted for open (2000.., ..1999), "
           "FIELD=!FROM..TO excluding it; a date as YYYY, YYYY-MM or YYYY-MM-DD, a number as a number")


class RangeError(ValueError):
    """A value or a range that cannot be read: the caller says which facet."""


@dataclass(frozen=True)
class Range:
    lo: int | float | None     # None: open below
    hi: int | float | None     # None: open above
    text: str                  # as given


def ensure_schema(pcon) -> None:
    for stmt in _SCHEMA:
        pcon.execute(stmt)


# ---- values ------------------------------------------------------------------------------

def date_span(text: str) -> tuple[int, int] | None:
    """`YYYY` → its first and last day, `YYYY-MM` → the month's, `YYYY-MM-DD`
    → that day twice, as integers YYYYMMDD; None when not a calendar date."""
    m = _DATE.fullmatch(text.strip())
    if m is None:
        return None
    y, mo, d = int(m[1]), m[2], m[3]
    try:
        if mo is None:
            return y * 10_000 + 101, y * 10_000 + 1231
        mo_i = int(mo)
        if d is None:
            last = calendar.monthrange(y, mo_i)[1]
            return y * 10_000 + mo_i * 100 + 1, y * 10_000 + mo_i * 100 + last
        day = date(y, mo_i, int(d))
    except ValueError:
        return None
    n = day.year * 10_000 + day.month * 100 + day.day
    return n, n


def number(v: Any) -> int | float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, str) and _NUMBER.fullmatch(v.strip()):
        s = v.strip()
        try:
            return int(s)
        except ValueError:
            f = float(s)
            return f if math.isfinite(f) else None
    return None


def type_of(field: str, v: Any) -> str | None:
    """A value's type: `date` for 09 §9.5's `date` field, otherwise read off
    the value — a date form first (`1999` as a string is a year), then a
    number. None: neither."""
    if field in DATE_FIELDS:
        return DATE if isinstance(v, str) and date_span(v) is not None else None
    if isinstance(v, str) and date_span(v) is not None:
        return DATE
    return NUMBER if number(v) is not None else None


def span(v: Any, type_: str) -> tuple[int | float, int | float] | None:
    if type_ == DATE:
        return date_span(v) if isinstance(v, str) else None
    n = number(v)
    return None if n is None else (n, n)


def declared_type(field: str) -> str | None:
    """The type known before any record is read; None when the values decide."""
    return DATE if field in DATE_FIELDS else None


# ---- ranges ------------------------------------------------------------------------------

def looks_like_range(text: str) -> bool:
    return isinstance(text, str) and _RANGE_SHAPE.fullmatch(text) is not None


def split(text: Any) -> tuple[str, str]:
    """`FROM..TO` → the two ends as written, either possibly empty; refuses
    anything else, and a range with both ends open (it restricts nothing)."""
    if not isinstance(text, str) or text.count("..") != 1:
        raise RangeError(f"{text!r} is not FROM..TO")
    a, _, b = text.partition("..")
    a, b = a.strip(), b.strip()
    if not a and not b:
        raise RangeError(f"{text!r} names neither end")
    return a, b


def parse(text: Any, type_: str) -> Range:
    a, b = split(text)
    lo = hi = None
    if a:
        s = span(a, type_)
        if s is None:
            raise RangeError(f"FROM {a!r} is not a {type_}")
        lo = s[0]
    if b:
        s = span(b, type_)
        if s is None:
            raise RangeError(f"TO {b!r} is not a {type_}")
        hi = s[1]
    if lo is not None and hi is not None and lo > hi:
        raise RangeError(f"{text!r}: FROM is after TO")
    return Range(lo, hi, str(text))


def _where(r: Range) -> tuple[str, list]:
    """Containment: lo >= FROM, hi <= TO — and lo <= TO too (implied, since
    lo <= hi), stated so that the index scan is bounded on both sides."""
    sql, args = ["field=?"], []
    if r.lo is not None:
        sql.append("lo >= ?")
        args.append(r.lo)
    if r.hi is not None:
        sql += ["lo <= ?", "hi <= ?"]
        args += [r.hi, r.hi]
    return " AND ".join(sql), args


def query(field: str, r: Range) -> tuple[str, list]:
    """The resolving statement and its parameters (the plan test reads it)."""
    where, args = _where(r)
    return f"SELECT native_id FROM range_postings WHERE {where}", [field, *args]


class TooBroad(Exception):
    pass


class Stopped(Exception):
    """The node is stopping: the pass resumes from the parts not yet read."""


def ids(pcon, field: str, r: Range, *, limit: int) -> set[int]:
    """The records with a value wholly inside `r`; TooBroad past `limit`
    records, read no further."""
    sql, args = query(field, r)
    out: set[int] = set()
    for (nid,) in pcon.execute(sql, args):
        out.add(int(nid))
        if len(out) > limit:
            raise TooBroad()
    return out


# ---- the pass over the parts held ---------------------------------------------------------

def _value_of(record: dict, field: str) -> Any:
    if field.startswith("extra."):
        extra = record.get("extra")
        return extra.get(field[6:]) if isinstance(extra, dict) else None
    return record.get(field)


def _read_part(path: Path, field: str) -> dict[str, Any]:
    from app.pack import records as pack_records
    column = "extra" if field.startswith("extra.") else field
    rows: list[tuple[Any, Any, int]] = []
    types: set[str] = set()
    unparsed = records = 0
    lo_min = hi_max = lo_text = hi_text = None
    for rec in pack_records.iter_fields(path, ("native_id", column)):
        records += 1
        try:
            nid = int(rec.get("native_id"))
        except (TypeError, ValueError):
            continue        # as the manifest's facet pass: the legs take numeric ids only
        v = _value_of(rec, field)
        for one in (v if isinstance(v, list) else [v]):
            if one is None or one == "":
                continue
            t = type_of(field, one)
            s = span(one, t) if t else None
            if s is None:
                unparsed += 1
                continue
            types.add(t)
            rows.append((s[0], s[1], nid))
            if lo_min is None or s[0] < lo_min:
                lo_min, lo_text = s[0], str(one)
            if hi_max is None or s[1] > hi_max:
                hi_max, hi_text = s[1], str(one)
    return {"rows": rows, "records": records, "unparsed": unparsed,
            "type": (types.pop() if len(types) == 1 else ("mixed" if types else None)),
            "lo_min": lo_min, "lo_text": lo_text, "hi_max": hi_max, "hi_text": hi_text}


def clear(pcon, field: str) -> None:
    ensure_schema(pcon)
    pcon.execute("BEGIN IMMEDIATE")
    try:
        pcon.execute("DELETE FROM range_postings WHERE part IN (SELECT id FROM range_parts WHERE field=?)", (field,))
        pcon.execute("DELETE FROM range_parts WHERE field=?", (field,))
        pcon.execute("DELETE FROM built WHERE field=?", (field,))
        pcon.execute("COMMIT")
    except BaseException:
        pcon.execute("ROLLBACK")
        raise


def _drop_part(pcon, part_id: int) -> None:
    pcon.execute("DELETE FROM range_postings WHERE part=?", (part_id,))
    pcon.execute("DELETE FROM range_parts WHERE id=?", (part_id,))


def index(pcon, field: str, *, shard_dir: Path, snapshot, snapshot_id: str | None, rows_bound: int,
          stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Bring `field`'s range postings to `snapshot`'s parts held here, and
    mark them built for `snapshot_id`: `{postings, documents, type, bounds}`
    for the facet cache. Stopped between parts by `stop` (Stopped)."""
    ensure_schema(pcon)
    held = [s for s in snapshot.shards if (Path(shard_dir) / s.path).is_file()]
    documents = sum(int(s.records) for s in held)
    if documents > rows_bound:
        clear(pcon, field)
        return {"documents": documents, "type": None, "bounds": [None, None],
                "postings": {"built": False, "why": f"{documents} records hold its values, more than the "
                                                    f"{rows_bound} rows a field's index may reach on this peer"}}
    r = pcon.execute("SELECT snapshot_id FROM built WHERE field=?", (field,)).fetchone()
    if r is not None and r[0] != snapshot_id:
        # rows of two snapshots must never answer as either: unmarked until done
        pcon.execute("DELETE FROM built WHERE field=?", (field,))
    done = {row[1]: (row[0], row[2]) for row in pcon.execute(
        "SELECT id, part, root FROM range_parts WHERE field=?", (field,))}
    paths = {s.path for s in held}
    for part, (pid, _root) in done.items():
        if part not in paths:
            pcon.execute("BEGIN IMMEDIATE")
            try:
                _drop_part(pcon, pid)
                pcon.execute("COMMIT")
            except BaseException:
                pcon.execute("ROLLBACK")
                raise
    for s in held:
        if done.get(s.path, (None, None))[1] == s.root:
            continue
        if stop is not None and stop():
            raise Stopped(f"{field!r}: the pass was stopped before it finished")
        got = _read_part(Path(shard_dir) / s.path, field)
        pcon.execute("BEGIN IMMEDIATE")
        try:
            old = pcon.execute("SELECT id FROM range_parts WHERE field=? AND part=?", (field, s.path)).fetchone()
            if old is not None:
                _drop_part(pcon, old[0])
            pid = pcon.execute(
                "INSERT INTO range_parts(field, part, root, records, rows, unparsed, type, lo_min, lo_text, "
                "hi_max, hi_text) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (field, s.path, s.root, got["records"], len(got["rows"]), got["unparsed"], got["type"],
                 got["lo_min"], got["lo_text"], got["hi_max"], got["hi_text"])).lastrowid
            pcon.executemany("INSERT INTO range_postings(field, lo, hi, native_id, part) VALUES(?,?,?,?,?)",
                             [(field, lo, hi, nid, pid) for lo, hi, nid in got["rows"]])
            pcon.execute("COMMIT")
        except BaseException:
            pcon.execute("ROLLBACK")
            raise
    return _finish(pcon, field, snapshot_id=snapshot_id, documents=documents, rows_bound=rows_bound)


def _finish(pcon, field: str, *, snapshot_id: str | None, documents: int, rows_bound: int) -> dict[str, Any]:
    parts = pcon.execute("SELECT * FROM range_parts WHERE field=?", (field,)).fetchall()
    types = {p["type"] for p in parts if p["type"]}
    rows = sum(p["rows"] for p in parts)
    unparsed = sum(p["unparsed"] for p in parts)
    lo = min((p for p in parts if p["lo_min"] is not None), key=lambda p: p["lo_min"], default=None)
    hi = max((p for p in parts if p["hi_max"] is not None), key=lambda p: p["hi_max"], default=None)
    bounds = [lo["lo_text"] if lo is not None else None, hi["hi_text"] if hi is not None else None]
    if len(types) > 1 or "mixed" in types:
        clear(pcon, field)
        return {"documents": documents, "type": None, "bounds": bounds,
                "postings": {"built": False, "why": "its values are dates in some records and numbers in "
                                                    "others: no one order is the field's"}}
    if rows > rows_bound:
        clear(pcon, field)
        return {"documents": documents, "type": None, "bounds": bounds,
                "postings": {"built": False, "why": f"{rows} values, more than the {rows_bound} rows a "
                                                    f"field's index may reach on this peer"}}
    type_ = types.pop() if types else declared_type(field)
    pcon.execute("INSERT OR REPLACE INTO built(field, snapshot_id, documents) VALUES(?,?,?)",
                 (field, snapshot_id, documents))
    return {"documents": documents, "type": type_, "bounds": bounds,
            "postings": {"built": True, "rows": rows, "parts": len(parts), "unparsed": unparsed}}

