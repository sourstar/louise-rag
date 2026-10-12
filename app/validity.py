"""Validity — one record per Expression, and the version in force at a date
chosen BY KEY, never by similarity (docs/notes/normattiva-multivigenza-review.md
§3.2–§3.3; 05 §5.1 and 07 §7.3 as amended 2026-10-12; invariant 4).

A collection whose Codex `profile` declares a validity axis —

    {"validity": {"kind": "versions-per-work",
                  "from": "extra.vigenza_from", "work": "extra.eli_work"}}

— holds every version of every Work as its own record. The record carries
the START of its validity (origin information). Its END is not in the
record: no form of the origin states it, it is the next version's start,
and 02 §2.3 says a record carries nothing that can be computed from it. So
the end is DERIVED here, node-locally, into the manifest's `validity`
table and from there into three columns of the text index and the vector
table — `valid_from` (an integer YYYYMMDD, the record's own start),
`valid_to` (the same form, `OPEN` while in force) and `shown` (1 on the
Work's latest version that has text) — so that a search filters by a
column range and never by a list of ids (05 §5.2's filter takes at most
20,000; the law holds 190,000 acts).

What a search does for such a collection (05 §5.1, amended):
  * without `--as-of`: `shown = 1` — one record per Work, the latest text,
    so a repealed act is shown in the last text it had and labelled
    (eli-legal-plugins.md §5.7);
  * with `--as-of D`: `valid_from <= D < valid_to`, both bounds columns.
    The id's high digits are NOT the lower bound: they sort by the date
    the member NAME carries, which for an original is its publication,
    while its validity starts at its entry into force, up to fifteen days
    later (normattiva `original_start`; a query in that fortnight must
    not return it). Versions of one Work are contiguous, so exactly one
    is admitted per Work: no two versions ever meet in a leg or in RRF.
  * the snapshot chain's as-of (app/ask/asof.py) does not apply: the chain
    is transaction time, when the origin's state was held; validity is
    valid time. [R6]/[R7] of the review.

How the columns stay current: the rdf stage records a version and
recomputes its Work's chain (`record`); a neighbour whose (valid_to,
shown) moved gets `fts_done=0, embed_done=0`, so its own stages rewrite
its index rows (the fts stage re-adds its chunks with the new fields; the
embed stage reuses every vector and `sync_columns` updates the two
columns in place) — no new selection, no new stage. `synced = 0` marks a
row whose index columns may still lag, for a peer on which those stages
do not run (embed off, an import-only copy): the index maintainer drains
it (`sync_vectors`).

`OPEN` is an INDEX sentinel and never reaches a record: 09 §9.5's record
has no end-of-validity key at all.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

OPEN = 99_999_999          # "still in force", in the index columns only
_ID_SPACE = 10 ** 9        # the id's low digits (app/plugins/eli.py)
COLUMNS = ("valid_from", "valid_to", "shown")


def current_columns(native_id: int) -> dict[str, int]:
    """The three columns of a record the table does not know (none recorded
    yet, or imported vectors): "current", starting at the date in the id's
    high digits, until its own rdf stage or the maintainer says otherwise."""
    return {"valid_from": int(native_id) // _ID_SPACE, "valid_to": OPEN, "shown": 1}


@dataclass(frozen=True)
class Validity:
    from_key: str          # "extra.vigenza_from": the record field holding the start of validity
    work_key: str          # "extra.eli_work": the record field naming the Work
    kind: str = "versions-per-work"


def _validity_block(profile) -> Validity | None:
    if not isinstance(profile, dict):
        return None
    v = profile.get("validity")
    if not isinstance(v, dict) or not v.get("from") or not v.get("work"):
        return None
    return Validity(from_key=str(v["from"]), work_key=str(v["work"]),
                    kind=str(v.get("kind") or "versions-per-work"))


def from_plugin(plugin) -> Validity | None:
    """What the plugin declares as its default profile (the template copies
    it into the Codex at `create`)."""
    return _validity_block(getattr(plugin, "default_profile", None))


def declared(collection) -> Validity | None:
    """The Codex's declaration where there is a Codex; a collection without
    one (a test's, a pre-Codex manifest) falls back to its plugin's default,
    which is what `create` would have written."""
    if collection is None:
        return None
    codex = getattr(collection, "codex", None)
    if codex is not None:
        return _validity_block(getattr(codex, "profile", None))
    plugin = None
    get = getattr(collection, "plugin_instance", None)
    if callable(get):
        try:
            plugin = get()
        except Exception:   # noqa: BLE001 — no plugin, no declaration
            plugin = None
    return from_plugin(plugin) if plugin is not None else None


def day_int(text: str | date) -> int:
    """`2001-02-01` → 20010201, the form the id's high digits and the
    columns use (07 §7.3: a date is YYYY-MM-DD and nothing shorter)."""
    if isinstance(text, date):
        return text.year * 10000 + text.month * 100 + text.day
    s = str(text)
    if len(s) != 10 or s[4] != "-" or s[7] != "-" or not (s[:4] + s[5:7] + s[8:]).isdigit():
        raise ValueError(f"not a YYYY-MM-DD date: {text!r}")
    return int(s[:4] + s[5:7] + s[8:])


def day_text(n: int | None) -> str | None:
    if n is None or n == OPEN:
        return None
    s = f"{int(n):08d}"
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def _field(doc, key: str):
    if key.startswith("extra."):
        return (doc.extra or {}).get(key[len("extra."):])
    return getattr(doc, key, None)


def of_doc(doc, v: Validity) -> dict[str, Any] | None:
    """What the rdf stage records about one parsed document, or None when
    the document names no start of validity or no Work (then it is not a
    version of anything and takes no part in the filter)."""
    start, work = _field(doc, v.from_key), _field(doc, v.work_key)
    if not start or not work:
        return None
    try:
        start_i = day_int(str(start))
    except ValueError:
        return None
    in_force = (doc.extra or {}).get("in_force")
    return {"from": start_i, "work": str(work),
            "has_text": any((body or "").strip() for _, body in (doc.sections or [])),
            "in_force": None if in_force is None else int(bool(in_force))}


# ---- the manifest's version table -------------------------------------------

def record(con, uid: str, native_id: int, meta: dict[str, Any]) -> list[str]:
    """Upsert one version and recompute its Work's chain, inside the caller's
    transaction (the rdf stage's). Returns the uids of the OTHER versions
    whose (valid_to, shown) moved; their stage marks are reset here so
    their own stages rewrite their index rows.

    Cost: one indexed read of the Work's versions (idx_validity_work) and
    one update per version that moved — at most the versions of one act
    (the codes: about 400), typically one."""
    con.execute(
        "INSERT INTO validity(uid, native_id, work, valid_from, valid_to, has_text, in_force, shown, synced) "
        "VALUES(?,?,?,?,NULL,?,?,0,0) "
        "ON CONFLICT(uid) DO UPDATE SET native_id=excluded.native_id, work=excluded.work, "
        "valid_from=excluded.valid_from, has_text=excluded.has_text, in_force=excluded.in_force, synced=0",
        (uid, int(native_id), meta["work"], int(meta["from"]), int(bool(meta["has_text"])), meta.get("in_force")))
    return recompute(con, meta["work"], except_uid=uid)


def recompute(con, work: str, *, except_uid: str | None = None) -> list[str]:
    """The chain of one Work: versions in order of (valid_from, native_id);
    each ends where the next starts, the last is open; `shown` is the last
    version that has text."""
    rows = con.execute(
        "SELECT uid, native_id, valid_from, valid_to, has_text, shown FROM validity "
        "WHERE work=? ORDER BY valid_from, native_id", (work,)).fetchall()
    moved: list[str] = []
    last_text = max((i for i, r in enumerate(rows) if r["has_text"]), default=None)
    for i, r in enumerate(rows):
        to = int(rows[i + 1]["valid_from"]) if i + 1 < len(rows) else None
        shown = 1 if i == last_text else 0
        if r["valid_to"] == to and int(r["shown"]) == shown:
            continue
        con.execute("UPDATE validity SET valid_to=?, shown=?, synced=0 WHERE uid=?", (to, shown, r["uid"]))
        if r["uid"] != except_uid:
            # its index rows carry the old columns: its own stages rewrite them
            con.execute("UPDATE documents SET fts_done=0, embed_done=0 WHERE uid=? AND rdf_done=1",
                        (r["uid"],))
            moved.append(r["uid"])
    return moved


def index_columns(con, native_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """The three index columns per record, as the table has them now. A
    record the table does not know (none recorded yet) reads as current
    (`current_columns`) until its own rdf stage says otherwise."""
    ids = sorted({int(i) for i in native_ids})
    out = {i: current_columns(i) for i in ids}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        marks = ",".join("?" for _ in part)
        for r in con.execute(f"SELECT native_id, valid_from, valid_to, shown FROM validity "
                             f"WHERE native_id IN ({marks})", part):
            out[int(r["native_id"])] = {"valid_from": int(r["valid_from"]),
                                        "valid_to": OPEN if r["valid_to"] is None else int(r["valid_to"]),
                                        "shown": int(r["shown"])}
    return out


def columns_for(con, collection, native_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """`index_columns` for a validity collection, `{}` for any other."""
    if declared(collection) is None:
        return {}
    return index_columns(con, native_ids)


def mark_synced(con, native_ids: Iterable[int]) -> None:
    ids = [int(i) for i in native_ids]
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        con.execute(f"UPDATE validity SET synced=1 WHERE native_id IN ({','.join('?' for _ in part)})", part)


def sync_vectors(con, collection, *, limit: int = 500, lock_con=None) -> int:
    """The index maintainer's drain: the vector table's columns of up to
    `limit` rows whose columns moved since they were written, for
    a peer whose embed stage does not run (05 §5.7: the index is local and
    derived; the maintainer keeps it current per snapshot). Returns how
    many it updated; rows the table does not hold are marked synced too."""
    if declared(collection) is None:
        return 0
    from app import embeddings
    rows = con.execute("SELECT native_id, valid_from, valid_to, shown FROM validity WHERE synced=0 "
                       "ORDER BY native_id LIMIT ?", (int(limit),)).fetchall()
    done, n = [], 0
    for r in rows:
        cols = {"valid_from": int(r["valid_from"]),
                "valid_to": OPEN if r["valid_to"] is None else int(r["valid_to"]), "shown": int(r["shown"])}
        if embeddings.sync_columns(int(r["native_id"]), cols, collection, lock_con=lock_con):
            n += 1
        done.append(int(r["native_id"]))
    if done:
        mark_synced(con, done)
    return n


# ---- the search's window ------------------------------------------------------

def window(collection, as_of: int | None) -> dict[str, Any] | None:
    """What the legs filter on for this collection: None for a collection
    that declares no validity; `{"shown": True}` for the default view;
    `{"day": D}` as of D."""
    if declared(collection) is None:
        return None
    if as_of is None:
        return {"shown": True}
    return {"day": int(as_of)}


def lance_where(w: dict[str, Any]) -> str:
    if w.get("shown"):
        return "shown = 1"
    return f"valid_from <= {int(w['day'])} AND valid_to > {int(w['day'])}"


def fts_filters(schema, w: dict[str, Any]) -> list:
    """The tantivy queries ANDed to the text query (fast-field ranges)."""
    import tantivy
    if w.get("shown"):
        return [tantivy.Query.term_query(schema, "shown", 1)]
    return [tantivy.Query.range_query(schema, "valid_from", tantivy.FieldType.Integer,
                                      0, int(w["day"]), True, True),
            tantivy.Query.range_query(schema, "valid_to", tantivy.FieldType.Integer,
                                      int(w["day"]), OPEN, False, True)]


def admit(con, w: dict[str, Any], native_ids: Iterable[int]) -> set[int]:
    """Of `native_ids`, those the window admits — for the catalog leg, whose
    index carries no validity columns (bounded by the leg's hits)."""
    ids = sorted({int(i) for i in native_ids})
    out: set[int] = set()
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        marks = ",".join("?" for _ in part)
        if w.get("shown"):
            q = f"SELECT native_id FROM validity WHERE native_id IN ({marks}) AND shown=1"
            params: list = part
        else:
            q = (f"SELECT native_id FROM validity WHERE native_id IN ({marks}) AND valid_from<=? "
                 f"AND (valid_to IS NULL OR valid_to>?)")
            params = [*part, int(w["day"]), int(w["day"])]
        out |= {int(r[0]) for r in con.execute(q, params)}
    return out


def describe(con, native_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """05 §5.3's `validity` on a result: when the version came into force,
    when the next one did (null while in force), and the origin's force
    status where it stated one."""
    ids = sorted({int(i) for i in native_ids})
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(ids), 500):
        part = ids[start:start + 500]
        marks = ",".join("?" for _ in part)
        for r in con.execute(f"SELECT native_id, valid_from, valid_to, in_force, shown FROM validity "
                             f"WHERE native_id IN ({marks})", part):
            out[int(r["native_id"])] = {
                "from": day_text(r["valid_from"]), "to": day_text(r["valid_to"]),
                "in_force": None if r["in_force"] is None else bool(r["in_force"]),
                "current": bool(r["shown"])}
    return out
