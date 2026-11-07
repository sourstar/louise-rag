"""A collection's status, stage by stage — THE ONE DEFINITION.

THE OPERATOR, 2026-11-07: "The interface with which a model status is
queried must me common, as the fetching, parsing, indexing and embedding".
The Library's card was common code, but what it SAID was computed three
times with three definitions: the card's scan (app/web.py), `node show` /
`node health`'s bounded counts and reasons (app/health.py), and
`/api/status`'s own SQL (app/api.py) — and `node_state.embedding_blockers`
was a fourth answer to "what holds embedding". The same manifest read
"paused" on the card and "running" in `node show`; a node in acquire-only
gave two different sentences; a re-embed the card counted as awaiting the
encoder was idle in `node show`. Every surface now reads this module.

WHAT A STAGE IS (rdf = parse, fts = index, embed; fetch from the fetch
ledger). For each, over the documents in scope:

  target       what the stage counts up to: in scope, less the documents
               the origin gave no text for (`unindexable`), and for embed
               less those not embedded here by choice (`declined`, 07 §7.8
               `embed_slices` / `embed_select`, embed_done = -1);
  done         the stage's mark set (embed_done = 1, whatever its version);
  waiting      cleared the stage before, not this one, no error recorded:
               what the worker's own selection takes now (`AT`, below);
  errored      at this stage with an error recorded — out of every
               selection until a person retries them, so NOT waiting;
  blocked      not done here and stopped by a stage failure, here or
               upstream (the card's "N blocked" and "clear errors & retry");
  superseded   not done here, but an EARLIER version of the document is
               still held — its text in the lexical index, its vectors in
               the table — and searched until the stage redoes it (operator,
               2026-11-07: shown grey on the bar, "present but not active");
  reembed      embed: done at an older EMBED_VERSION, which the worker's
               version walk re-embeds (counted done on the bar).

TWO COSTS, ONE DEFINITION. `full_scan_sql` is the card's one pass over the
manifest, cached with its TTL and stored row (web.py `_document_counts`):
exact for every number, at the cost of a full scan (pubmed's 40 M rows,
~56 s). `bounded` is what `node show` / `node health` / `hold collection
show` can afford live: index-prefix counts on `idx_documents_scope`, capped
at WAITING_CAP, with the error lookups made only below ERRORED_EXACT_BELOW
(the measurements are with the constants). Where both are exact they are
the same number (tests/test_one_status.py). What the index cannot see —
`embed_version` for a re-embed, the error column and the versions over the
bound — the bounded path takes from the last full count stored in the
manifest, with its age, and says so (`from_scan`); it never guesses.

WHAT HOLDS A STAGE is `holds()`: the node's mode, the collection's own
pause, the node profile, the embed setting, the license gate, the copy's
state, the vector store's floor — one list, one sentence each, in one
order, and `reason()` picks the one a single-line surface prints.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config

STAGES = ("fetch", "rdf", "fts", "embed")
DOC_STAGES = ("rdf", "fts", "embed")

IDLE, RUNNING, HELD, BLOCKED = "idle", "running", "held", "blocked"
#: a fact that could not be read: said where a surface lists what holds a
#: stage, never a reason to call the stage held ("never invent a reason")
UNKNOWN = "unknown"

# ---- the predicates ---------------------------------------------------------

#: Documents that have cleared the previous stage and not this one.
#:
#: EVERY COLUMN HERE IS A PREFIX OF `idx_documents_scope` (in_scope,
#: rdf_done, fts_done, embed_done), and `error IS NULL` is deliberately
#: NOT among them, because it is not in that index. MEASURED on the live
#: pubmed manifest, same answer both ways:
#:
#:     ... AND error IS NULL              4,577,101   10.27 s
#:     covering index, no error clause    4,577,101    0.45 s
#:
#: 23x, and the slow one's plan reads as indexed — "SEARCH documents
#: USING INDEX idx_documents_scope (in_scope=?)" — which is the exact
#: false positive CLAUDE.md warns about. One table lookup per counted
#: row, 4.58M of them. (Moved from app/health.py `_WAITING`, 2026-11-07.)
#:
#: A DECLINED document (embed_done = -1) is in none of them: `embed_done=0`
#: leaves it out, in the index, so the bounded embed count and the full
#: one agree without a lookup.
AT = {
    "rdf": "in_scope=1 AND rdf_done=0",
    "fts": "in_scope=1 AND rdf_done=1 AND fts_done=0",
    "embed": "in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=0",
}
INDEXED = "documents INDEXED BY idx_documents_scope"
#: A stage count stops here and says "over": COUNT(*) walks the waiting rows
#: along the covering index, 16.6 s for wikidata's 72.9 M during discovery
#: and 24 s for wikipedia-v8 mid-build, so `node show` took 20-33 s (München,
#: 2026-10-27). Exact below the cap, ~0.25 s at it. The standard bounded
#: count (a LIMIT under the COUNT); the stage's state does not depend on
#: the size, only on whether there is any.
WAITING_CAP = 1_000_000
#: Below this many rows the count is made exact: the errored rows among them
#: are read through the table (one lookup each, which is what the cheap count
#: avoids) and counted apart. gutenberg's 33 "waiting" rdf documents were all
#: errored since September — out of every selection — and read as stuck work
#: (München, 2026-10-30). Above it the errored few stay inside the count.
#: The same bound admits the `superseded` lookups (below).
ERRORED_EXACT_BELOW = 100_000

#: an error a STAGE wrote (worker._execute_step prefixes its stage); an
#: unprefixed one came from discovery and describes the corpus
STAGE_ERROR = "(" + " OR ".join(f"error LIKE '{st}: %'" for st in ("rdf", "fts", "embed")) + ")"
#: the origin has no text for it: out of every target, never retryable
UNINDEXABLE = f"(error IS NOT NULL AND NOT {STAGE_ERROR} AND retries<:max)"

#: NOT DONE HERE, AN EARLIER VERSION STILL HELD (operator, 2026-11-07).
#:
#: `<stage>_version` is written ONLY together with `<stage>_done = 1` — the
#: fts stage (worker `fts_version`), the embed stage, `embed_marks`, the
#: bundle importer and the archive import (`embed_version`) — and nothing
#: ever writes it back to 0. So a document whose mark is 0 and whose version
#: is not was completed once and has had its mark cleared since: by a
#: re-parse (discovery, the origin republished it), validity, a reset or a
#: reindex. None of those deletes what the stage wrote — the lexical
#: entries and the vectors stay, and stay searched, until the stage redoes
#: the document (delete-then-insert on its book id, invariant 2).
#:
#: `<> 0`, not `> 0`: REPLACED_VERSION (-1, `hold vectors reembed`) is a
#: document whose vectors are held and being replaced. 0 is never completed
#: — what it may hold is an interrupted embedding's slices (`may_reuse`),
#: not an earlier version. `chunks > 0`: a document with no chunk is marked
#: done with no vector or entry written. NOT UNINDEXABLE: `unindexable.
#: withdraw` deletes a document's entries and vectors before writing its
#: corpus error, and an unindexable document is in no target anyway.
#:
#: Known not-exact: `dev drop-rows` and `dev carry` delete a document's rows
#: and clear its mark, keeping the version, until it is embedded again — an
#: operator pass, minutes long; the grey part then overstates by those.
SUPERSEDED = {
    "fts": ("in_scope=1 AND fts_done=0 AND COALESCE(fts_version,0)<>0 "
            f"AND COALESCE(chunks,0)>0 AND NOT {UNINDEXABLE}"),
    "embed": ("in_scope=1 AND embed_done=0 AND COALESCE(embed_version,0)<>0 "
              f"AND COALESCE(chunks,0)>0 AND NOT {UNINDEXABLE}"),
}
#: the same, through the scope index: every candidate of the bounded
#: lookup, all four index columns equalities (rdf/fts take 0 or 1, never
#: anything else), so SQLite seeks it and reads only what it holds
_SUPERSEDED_SEEK = {
    "fts": "in_scope=1 AND rdf_done IN (0,1) AND fts_done=0",
    "embed": "in_scope=1 AND rdf_done IN (0,1) AND fts_done IN (0,1) AND embed_done=0",
}
#: the worker's version walk (worker `_REEMBED_WINDOWED`): done, at an older
#: EMBED_VERSION, selectable
REEMBED = ("in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=1 "
           "AND embed_version<:ver AND error IS NULL AND retries<:max")


def full_scan_sql(spans: list[tuple[int, int]]) -> tuple[str, dict]:
    """THE scan's SQL and parameters — every count the Library's card, the
    Stats page and `/api/status` read, in one pass. `spans` (an archive's
    declared id range, web.py `_archive_spans`) adds the range's SUMs;
    without them the SQL is the base scan byte for byte."""
    # blocked vs unindexable: stage failures are prefixed with their stage
    # (worker._execute_step), so an UNPREFIXED error came from discovery and
    # describes the CORPUS — no text in the mirror, retracted upstream.
    # "Clear errors & retry" cannot fix those (the next scan re-applies
    # them), so reporting them as failures promised an impossible remedy.
    stage_error = STAGE_ERROR
    # a stage failure or exhausted retries; `error IS NOT NULL` first, so the
    # three LIKEs run only on the few rows that carry an error (the same set:
    # a stage error is an error)
    failed = f"((error IS NOT NULL AND {stage_error}) OR retries>=:max)"
    # THE WORKER'S OWN TWO EMBED SELECTIONS (worker `_BACKFILL_SQL`, the new
    # documents, and `_REEMBED_WINDOWED`, the older versions): rdf_done=1 is
    # theirs, and was missing here (2026-11-07), so "awaiting the encoder"
    # is `waiting` + `reembed` exactly
    pending = (f"(embed_done=0 OR (embed_done=1 AND embed_version<:ver)) "
               f"AND in_scope=1 AND rdf_done=1 AND fts_done=1 AND error IS NULL AND retries<:max")
    params: dict = {"max": config.MAX_RETRIES, "ver": config.EMBED_VERSION}
    archive_sums = ""
    if spans:
        from .bundles.genesis import NUMERIC_ID_SQL, in_spans_sql
        spans_sql, span_params = in_spans_sql(spans, named=True)
        params.update(span_params, span_top=spans[-1][1])
        # a non-numeric id casts to 0: inside a range from 0 unless excluded
        in_range = f"({spans_sql} AND {NUMERIC_ID_SQL})"
        # THE ARCHIVE'S DECLARED RANGE, for the pre-embedded row: documents
        # that HOLD a vector inside it, over in-scope documents inside it.
        # Vectors held, not the import ledger (SUM(genesis_imports.docs)),
        # which is pass bookkeeping and read 80,654 short on pubmed. Four
        # more SUMs on the pass this already makes — deliberately NOT a
        # walk of idx_documents_native_int, which needs a row lookup per
        # document for in_scope/embed_done (40.6 s on live pubmed, health.py).
        # Measured on 4M synthetic rows (2026-09-14): base scan 2.13 s; the
        # SUMs 2.76 s (two or four cost the same); with the numeric-id check
        # that keeps a non-numeric id out of the range, 3.23 s — +52% over
        # the base, the TTL stretching with it (_counts_ttl_for).
        #
        # arch_in_range_waiting is the gap `louise node health` reports (its
        # embed `AT`, past indexing), so the card can say how much of its own
        # gap health will explain; arch_past_range is what is ABOVE the
        # range — the rest of what lies outside it (a gap between files, an
        # id below the first, an id that is not a number) is not "past".
        archive_sums = f""",
          COUNT(*) FILTER (WHERE in_scope=1 AND {in_range}) arch_in_range,
          COUNT(*) FILTER (WHERE in_scope=1 AND embed_done=1 AND {in_range}) arch_in_range_embedded,
          COUNT(*) FILTER (WHERE in_scope=1 AND embed_done=0 AND rdf_done=1 AND fts_done=1
                        AND {in_range}) arch_in_range_waiting,
          COUNT(*) FILTER (WHERE in_scope=1 AND CAST(native_id AS INTEGER) > :span_top
                        AND {NUMERIC_ID_SQL}) arch_past_range,
          -- Of what lies OUTSIDE the range, the documents with NO TEXT:
          -- `chunks` is written by the fts stage, so an indexed document
          -- with none has nothing for an encoder to embed. Wikipedia's
          -- redirects are that — a record of their target and no more —
          -- and the card said all 30.9 M pages past the range were the
          -- local encoder's work (2026-09-15). An UNINDEXED row is not
          -- counted: its chunk count is unknown, never zero.
          COUNT(*) FILTER (WHERE in_scope=1 AND NOT {in_range} AND fts_done=1
                        AND COALESCE(chunks, 0) = 0) arch_outside_empty,
          -- The local encoder's progress on THE REST: embedded, and not one
          -- of those. The embed stage marks a document with no chunk done
          -- without writing a vector, and the card, which derived this as
          -- embed_done minus the range's, counted 37 of pubmed's 53 empty
          -- records both as "nothing to embed" and as "done" (2026-09-18).
          COUNT(*) FILTER (WHERE in_scope=1 AND NOT {in_range} AND embed_done=1
                        AND NOT (fts_done=1 AND COALESCE(chunks, 0) = 0)) arch_outside_embedded"""
    # COUNT(*) FILTER (WHERE …), not SUM(CASE WHEN … THEN 1 ELSE 0 END)
    # (2026-11-07; SQLite 3.30's aggregate FILTER, the SQL:2003 standard): a
    # row the filter refuses never reaches the aggregate, and each filter
    # names its most selective column first. MEASURED on 4 M synthetic rows,
    # same numbers every way: the scan of 960bb09 3.12 s; with this module's
    # twelve counts as SUM(CASE) 5.45 s (+75 %); as FILTER 3.27 s; and each
    # filter ordered most-selective first 2.47 s — 21 % under 960bb09's scan
    # while counting twelve numbers more, its own numbers identical
    # (SQLite 3.45; the image's Debian SQLite is >= 3.40, FILTER is 3.30's).
    return f"""
        SELECT COUNT(*) total, SUM(in_scope) in_scope,
          COUNT(*) FILTER (WHERE in_scope=1 AND rdf_done=1) rdf_done,
          COUNT(*) FILTER (WHERE in_scope=1 AND fts_done=1) fts_done,
          COUNT(*) FILTER (WHERE in_scope=1 AND embed_done=1) embed_done,
          -- not embedded here by choice (07 §7.8 `embed_slices`): out of the
          -- dense stage's target, never counted as embedded
          COUNT(*) FILTER (WHERE in_scope=1 AND embed_done=-1) declined,
          COUNT(*) FILTER (WHERE in_scope=1 AND {failed}) blocked,
          -- PER STAGE, because "blocked" is collection-wide and says
          -- nothing about WHICH stage a document is stuck at. A document
          -- blocked at fts has already passed rdf, so counting it against
          -- rdf's remaining work makes a stage that is actively running
          -- look like it has nothing left to do. Three more counts on a scan
          -- that is already walking every row.
          COUNT(*) FILTER (WHERE rdf_done=0 AND in_scope=1 AND {failed}) blocked_rdf,
          COUNT(*) FILTER (WHERE fts_done=0 AND in_scope=1 AND {failed}) blocked_fts,
          COUNT(*) FILTER (WHERE embed_done=0 AND in_scope=1 AND {failed}) blocked_embed,
          COUNT(*) FILTER (WHERE error IS NOT NULL AND in_scope=1
                        AND NOT {stage_error} AND retries<:max) unindexable,
          COUNT(*) FILTER (WHERE {pending}) embed_ready,
          SUM(chunks) FILTER (WHERE {pending}) chunks_pending,
          -- Documents that still have to pass rdf/fts before they can be
          -- embedded. `chunks` is written BY the fts stage, so for these the
          -- chunk count is not zero — it is unknown, and a page that renders
          -- it as 0 says "nothing to do" about a collection with thousands of
          -- contigs still to index.
          COUNT(*) FILTER (WHERE fts_done=0 AND in_scope=1 AND error IS NULL
                        AND retries<:max) docs_unindexed,
          -- ONE STATUS (2026-11-07): what `node show` counts live, counted
          -- here too, so the two can be held to the same number (app/status.py)
          COUNT(*) FILTER (WHERE rdf_done=0 AND in_scope=1 AND error IS NULL) waiting_rdf,
          COUNT(*) FILTER (WHERE fts_done=0 AND rdf_done=1 AND in_scope=1 AND error IS NULL) waiting_fts,
          COUNT(*) FILTER (WHERE embed_done=0 AND fts_done=1 AND rdf_done=1 AND in_scope=1
                                  AND error IS NULL) waiting_embed,
          COUNT(*) FILTER (WHERE error IS NOT NULL AND {AT['rdf']}) errored_rdf,
          COUNT(*) FILTER (WHERE error IS NOT NULL AND {AT['fts']}) errored_fts,
          COUNT(*) FILTER (WHERE error IS NOT NULL AND {AT['embed']}) errored_embed,
          COUNT(*) FILTER (WHERE embed_version<:ver AND {REEMBED}) reembed,
          COUNT(*) FILTER (WHERE fts_done=0 AND {SUPERSEDED['fts']}) superseded_fts,
          COUNT(*) FILTER (WHERE embed_done=0 AND {SUPERSEDED['embed']}) superseded_embed,
          -- `/api/status`'s, from the same pass (its own scan until 2026-11-07)
          COUNT(*) FILTER (WHERE summary_done=1 AND in_scope=1) summaries,
          SUM(chunks) FILTER (WHERE in_scope=1) chunks,
          SUM(tokens) FILTER (WHERE in_scope=1) tokens{archive_sums}
        FROM documents""", params


#: the counts `full_scan_sql` added for this module (2026-11-07): a stored
#: row written before them lacks them, and they read as NOT COUNTED (None)
#: until the next scan — never as a confident zero
SCAN_KEYS = ("waiting_rdf", "waiting_fts", "waiting_embed", "errored_rdf", "errored_fts",
             "errored_embed", "reembed", "superseded_fts", "superseded_embed",
             "summaries", "chunks", "tokens")

#: the manifest's control row holding the last full count (web.py
#: `_persist_counts` writes it): {"at", "counts", "scan_s"}
STORED_KEY = "document_counts"


def read_stored(con) -> tuple[dict, float, float] | None:
    """(counts, at, scan_s) from the manifest's stored full count, or None.
    One indexed row, never a scan; a missing or malformed row is no answer."""
    try:
        row = con.execute("SELECT value FROM control WHERE key=?", (STORED_KEY,)).fetchone()
        if row is None or not row[0]:
            return None
        got = json.loads(row[0])
        counts, at = got.get("counts"), float(got.get("at") or 0)
        scan_s = float(got.get("scan_s") or 0)
    except Exception:      # noqa: BLE001 — no answer, never a broken surface
        return None
    if not isinstance(counts, dict) or not at:
        return None
    return {k: int(v or 0) for k, v in counts.items()}, at, scan_s


# ---- the numbers ------------------------------------------------------------

@dataclass
class StageStatus:
    """One stage, as one of the two costs counted it (see the module)."""
    name: str
    target: int | None = None
    done: int | None = None
    waiting: int | None = None
    errored: int | None = None
    blocked: int | None = None
    declined: int | None = None
    unindexable: int | None = None
    superseded: int | None = None
    reembed: int | None = None
    #: `waiting` is the cap: there are more
    over: bool = False
    #: waiting and errored are exact (always on the full scan; below
    #: ERRORED_EXACT_BELOW on the bounded path)
    exact: bool = True
    #: numbers the bounded path could not count live and took from the last
    #: full count, with that count's time ({name: at})
    from_scan: dict = field(default_factory=dict)
    state: str = ""
    reason: str = ""
    #: every hold on the stage, in order (`holds()`), as {kind, what, says}
    holds: list = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items()
                if v is not None and v != {} and v != [] and v != ""}


def _n(counts: dict, key: str) -> int | None:
    v = counts.get(key)
    return None if v is None else int(v or 0)


def from_counts(counts: dict | None, *, fetched: dict | None = None) -> dict[str, StageStatus]:
    """Every stage from the full scan's counts (`full_scan_sql`), or {} for
    no counts yet. A count the scan's row does not carry (a stored row from
    before it was added) is None, never 0."""
    if counts is None:
        return {}
    in_scope = _n(counts, "in_scope") or 0
    unindexable = _n(counts, "unindexable") or 0
    declined = _n(counts, "declined") or 0
    indexable = max(0, in_scope - unindexable)
    out: dict[str, StageStatus] = {}
    for k in DOC_STAGES:
        out[k] = StageStatus(
            k, target=indexable if k != "embed" else max(0, indexable - declined),
            done=_n(counts, f"{k}_done") or 0, waiting=_n(counts, f"waiting_{k}"),
            errored=_n(counts, f"errored_{k}"), blocked=_n(counts, f"blocked_{k}") or 0,
            unindexable=unindexable,
            declined=declined if k == "embed" else None,
            superseded=_n(counts, f"superseded_{k}") if k != "rdf" else None,
            reembed=_n(counts, "reembed") if k == "embed" else None)
    # FILES, always — the unit that moves when bytes move, and the one
    # `fetched["complete"]` is judged on (web.py `_collection_shelf`)
    out["fetch"] = StageStatus(
        "fetch", target=int((fetched or {}).get("remote_total") or 0) if fetched else 0,
        done=int((fetched or {}).get("done") or 0) if fetched else 0)
    return {k: out[k] for k in STAGES}


def _count(con, where: str, params: dict | None = None, *, cap: int | None = None) -> int:
    sql = (f"SELECT COUNT(*) FROM (SELECT 1 FROM {INDEXED} WHERE {where} LIMIT {cap + 1})"
           if cap is not None else f"SELECT COUNT(*) FROM {INDEXED} WHERE {where}")
    return int(con.execute(sql, params or {}).fetchone()[0])


def bounded(con, *, cap: int = WAITING_CAP, exact_below: int = ERRORED_EXACT_BELOW,
            stored: tuple[dict, float, float] | None | bool = True) -> dict[str, StageStatus | Exception]:
    """The live count `node show` can afford: per document stage, `waiting`
    along the covering index (capped), the errored among them made exact
    below `exact_below`, and `superseded` where its candidates are under the
    same bound. What it cannot count live (a re-embed, a superseded count
    over the bound) comes from the stored full count `stored` (True: read
    it from the manifest) with its time in `from_scan`. A stage that cannot
    be counted at all is the exception it raised — NAMED by the caller,
    never skipped (an absent `idx_documents_scope` raises outright)."""
    out: dict[str, StageStatus | Exception] = {}
    for name, where in AT.items():
        try:
            n = _count(con, where, cap=cap)
        except Exception as e:   # noqa: BLE001 — the caller names it
            out[name] = e
            continue
        st = StageStatus(name, exact=False)
        waiting = n
        # n is capped at cap + 1: past the cap it says only "more", so the
        # errored are not looked up (an uncapped count) and it stays "over"
        if 0 < n <= min(exact_below, cap):
            try:
                st.errored = _count(con, f"{where} AND error IS NOT NULL")
                waiting -= st.errored
                st.exact = n <= cap
            except Exception:   # noqa: BLE001 — the cheap count stands
                st.errored = None
        elif n == 0:
            st.errored, st.exact = 0, True
        st.waiting, st.over = min(waiting, cap), waiting > cap
        out[name] = st
    # SUPERSEDED, exact where its candidates are few: every document whose
    # mark is 0 at a stage is in one of the stage sets just counted (a
    # document not parsed is in rdf's, whatever its other marks), so their
    # sum bounds the lookups
    counted = [s for s in out.values() if isinstance(s, StageStatus)]
    small = (len(counted) == len(AT) and not any(s.over for s in counted)
             and sum(s.waiting + (s.errored or 0) for s in counted) <= exact_below)
    params = {"max": config.MAX_RETRIES, "ver": config.EMBED_VERSION}
    for name in ("fts", "embed"):
        st = out.get(name)
        if not isinstance(st, StageStatus) or not small:
            continue
        try:
            st.superseded = _count(con, f"{_SUPERSEDED_SEEK[name]} AND {SUPERSEDED[name]}", params)
        except Exception:   # noqa: BLE001 — a manifest without the columns: not counted
            st.superseded = None
    if stored is True:
        stored = read_stored(con)
    if stored:
        counts, at, _ = stored
        # OVER THE CAP, the stored full count: the live count only knows
        # "more than WAITING_CAP", the card shows the scan's exact number,
        # and node show printed the cap beside it (München, 2026-11-07:
        # wikidata's embed "1000000, over" against the card's 41,455,026).
        # Taken only when it agrees with the live count — a stored number
        # under the cap is older than what the index just counted, and
        # the cap ("over") is then the truer answer.
        for name, st in out.items():
            if not (isinstance(st, StageStatus) and st.over):
                continue
            w = counts.get(f"waiting_{name}")
            if w is None or int(w) <= cap:
                continue
            st.waiting, st.over = int(w), False
            st.from_scan["waiting"] = at
            if f"errored_{name}" in counts:
                st.errored = int(counts[f"errored_{name}"])
                st.from_scan["errored"] = at
        for name in ("fts", "embed"):
            st = out.get(name)
            if isinstance(st, StageStatus) and st.superseded is None and f"superseded_{name}" in counts:
                st.superseded = int(counts[f"superseded_{name}"])
                st.from_scan["superseded"] = at
        st = out.get("embed")
        if isinstance(st, StageStatus) and "reembed" in counts:
            # embed_version is in no index: a live count reads every embedded
            # document (57.8 s on pubmed, worker `_REEMBED_WINDOWED`)
            st.reembed = int(counts["reembed"])
            st.from_scan["reembed"] = at
    return out


# ---- what holds a stage: THE ONE REASON FUNCTION ------------------------------

@dataclass(frozen=True)
class Hold:
    kind: str       # HELD (the operator's choice) | BLOCKED (resuming would not clear it) | UNKNOWN
    what: str       # mode | store | pause | profile | off | license | quarantine | disk
    says: str

    def to_json(self) -> dict[str, str]:
        return asdict(self)


#: What each stage needs the node to run — READ OFF THE ONE TABLE of what a
#: mode runs (app/node_state.py: MODE_RUNS; operator, 2026-09-19), never a
#: second list that could come to disagree with the worker. rdf is parsing;
#: fts and the archive's imports are indexing; embedding runs only where
#: everything does.
STAGE_NEEDS = {"fetch": "fetch", "rdf": "parse", "fts": "index", "embed": "embed", "archive": "index"}
#: the stages the node PROFILE's cpu_cores = 0 pauses (worker._cpu_paused):
#: the parse pool's work; embedding is held by the profile's `mode`, not its cores
PROFILE_PAUSED = ("rdf", "fts", "archive")
#: of the BLOCKED holds, the one a single-line surface names first — the
#: order `node health` always had: the license, then the copy, then the setting
_BLOCKED_FIRST = ("license", "quarantine", "off", "disk")

#: `pause` not given: read it from the node's store, creating nothing
_READ = object()


def mode_says(state: str, stage: str = "embed") -> str:
    """The node's mode, held against a stage: what it is and what lifts it."""
    from . import node_state
    label = dict(node_state.MODE_LABELS).get(state, state)
    says = node_state.MODE_SAYS.get(state, "a mode this node does not know: nothing runs")
    return (f'The node is in "{label}" mode ({says}); this runs when it is set from {state} to '
            f'"run everything": at the top of the Library, or with `louise node resume`.')


def off_says(name: str) -> str:
    return (f'{name}\'s embed setting is "off": embed is OFF for this collection until it is set to '
            f"on-demand, all local or all p2p — in its Embed mode in the Library, or with "
            f"`louise hold config apply {name} --set embed=all`, which writes "
            f"config/collections/{name}.toml.")


def _store_says(name: str) -> str:
    from . import node_state
    return (f"The node's state ({node_state.path()}) could not be read, so whether the node's mode or a "
            f"pause of {name} stops its embedding is not known: `louise node show` reads the "
            f"same file.")


def profile_says(stage: str) -> str:
    """THE NODE PROFILE (app/cpu_limit.py, the operator's schedule): its
    mode suspends processing in the quiet windows (operator, 2026-11-02),
    and its cpu_cores = 0 pauses rdf, fts and the archive pass; the stage
    read "waiting" with no reason while the worker's log alone said
    "profile quiet: paused until 13:30" (operator, 2026-10-31). "" when the
    profile holds nothing. Never raises."""
    if stage not in STAGE_NEEDS or stage == "fetch":
        return ""
    try:
        from . import cpu_limit, node_state
        lim = cpu_limit.current()
        if lim.suspends and not node_state.mode_runs(lim.mode or "", STAGE_NEEDS[stage]):
            return f"{lim.says()} (the node's schedule, platform.toml)"
        if stage in PROFILE_PAUSED and lim.cores == 0:
            goes_on = "; embedding goes on" if node_state.profile_runs(node_state.EMBED) else ""
            return f"{lim.says()} (the node's schedule, platform.toml){goes_on}"
    except Exception:   # noqa: BLE001 — a status never raises
        pass
    return ""


def license_says(collection) -> str:
    """Invariant 9, from THE resolution (app/licensing.py): a yellow or red
    collection with no valid acknowledgement is not scanned, indexed,
    embedded or imported, in any mode (worker `_license_allows`). "" when
    nothing is withheld. A collection licensing cannot judge is WITHHELD —
    search and the worker fail closed too, and three answers to one fault
    was the disagreement this exists to end (verification pass)."""
    try:
        from . import licensing
        if licensing.state_for(collection) != licensing.MISSING:
            return ""
        cls = licensing.license_class_for(collection)
    except Exception as e:  # noqa: BLE001 — a status never raises
        return (f"withheld: its license state cannot be judged "
                f"({type(e).__name__}: {e}) — invariant 9")
    return (f"withheld: license_class={cls} needs an acknowledgement of the "
            f"terms — invariant 9")


def copy_says(collection) -> tuple[str, str]:
    """(what blocks embedding and imports, what to say about the copy) —
    03 §3.6, from THE derivation (app/copy_state.py), the one the worker and
    the search read. While the node only REPORTS quarantine the first is ""
    and the second says what enforcing WOULD withhold, so the first of step
    14's two deploys shows the operator the day it would bite before it does.
    Never raises."""
    try:
        from . import copy_state
        state = copy_state.of(collection)
        if state.in_use:
            return "", ""
        why = f"{state.state}: " + "; ".join(state.reasons)
        if copy_state.enforced():
            return f"withheld: {why} — 03 §3.6", f"in {state.state}"
        return "", f"in {state.state}, REPORTED ONLY (nothing is withheld): {why}"
    except Exception as e:  # noqa: BLE001
        return "", f"its state cannot be derived ({type(e).__name__}: {e})"


def disk_says(collection) -> str:
    try:
        from . import disk_guard
        return disk_guard.vectors_short(collection) or ""
    except Exception:   # noqa: BLE001
        return ""


def holds(name: str, stage: str, *, collection=None, mode: str | None = None, pause: Any = _READ,
          embed_mode: str | None = None, node_con=None) -> list[Hold]:
    """EVERYTHING that holds `stage` of collection `name`, one plain sentence
    each saying what holds it and what lifts it; [] when nothing does.

    Read where the worker reads it: the node's mode (`mode_runs`; `mode`
    None reads it from state/node.sqlite — through `node_con` when the
    caller holds one, else creating nothing — and "unknown" is a store that
    could not be read), this collection's own pause (`pause`: the
    `node_state.pause_of` entry, None for none; not given, it is read with
    the mode), the node profile, and for the embed stage its setting
    (`embed_mode` "off"). With `collection`, the gates that need the
    collection itself: the license (every stage), and for embed the copy's
    state and the vector store's floor. The order is fixed — mode, pause,
    profile, setting, license, copy, disk — and `reason()` picks the one
    sentence a single-line surface prints. NEVER RAISES."""
    from . import node_state
    out: list[Hold] = []
    if mode is None:
        def read(c):
            from . import db
            return db.worker_state(c), node_state._pause_entry(c, name)
        try:
            facts = read(node_con) if node_con is not None else node_state.peek(
                read, ("paused", None), error=None)
        except Exception:   # noqa: BLE001 — said as unreadable
            facts = None
        if facts is None:
            mode, read_pause = "unknown", None
        else:
            mode, read_pause = facts
        if pause is _READ:
            pause = read_pause
    elif pause is _READ:
        pause = None if mode == "unknown" else node_state.pause_of(name)
    activity = STAGE_NEEDS.get(stage, stage)
    if mode == "unknown":
        out.append(Hold(UNKNOWN, "store", _store_says(name)))
    elif not node_state.mode_runs(mode, activity):
        out.append(Hold(HELD, "mode", mode_says(mode, stage)))
    if pause is not None:
        out.append(Hold(HELD, "pause", node_state._pause_says(name, pause)))
    said = profile_says(stage)
    if said:
        out.append(Hold(HELD, "profile", said))
    if stage == "embed" and str(embed_mode or "").lower() == "off":
        out.append(Hold(BLOCKED, "off", off_says(name)))
    if collection is not None:
        said = license_says(collection)
        if said:
            out.append(Hold(BLOCKED, "license", said))
        if stage in ("embed", "archive"):
            said = copy_says(collection)[0]
            if said:
                out.append(Hold(BLOCKED, "quarantine", said))
        if stage == "embed":
            said = disk_says(collection)
            if said:
                out.append(Hold(BLOCKED, "disk", said))
    return out


def reason(found: list[Hold]) -> tuple[str, str]:
    """(state, sentence) the holds put a stage with work in: BLOCKED by the
    first blocking hold in `_BLOCKED_FIRST`'s order (resuming the node
    would not clear it), else HELD by the first hold the operator's choice
    makes, else RUNNING. An UNKNOWN fact holds nothing."""
    blocked = {h.what: h for h in found if h.kind == BLOCKED}
    for what in _BLOCKED_FIRST:
        if what in blocked:
            return BLOCKED, blocked[what].says
    held = next((h for h in found if h.kind == HELD), None)
    if held is not None:
        return HELD, held.says
    return RUNNING, ""


def judge(work: int, found: list[Hold]) -> tuple[str, str]:
    """A stage's state and the reason named with it. A stage with nothing
    to do is IDLE and FINE even when switched off: "off and finished" is
    not a problem to report, and reporting it would make the check cry wolf
    on every idle collection."""
    if not work:
        return IDLE, ""
    return reason(found)


def held_reason(mode: str | None, stage: str) -> str:
    """What holds `stage` by the node alone — its mode, then its profile —
    or "" (app/health.py's `_held_reason`, which every reason went through)."""
    if not mode:
        return profile_says(stage)
    st, said = reason([h for h in holds("", stage, mode=mode, pause=None)
                       if h.what in ("mode", "profile")])
    return said if st == HELD else ""


# ---- rates and ETAs, from the persisted samples ------------------------------

#: What the ETA slot shows when work REMAINS and there is no rate to stand
#: behind. A dash, not "TBD" (operator, 2026-09-12): the card is read to
#: see whether anything is moving, and a word the reader has to look up is
#: worse than an honest blank. "TBD" also stood for four different states —
#: no history yet, one window so far, stalled, and not scheduled — none of
#: which it distinguished. The dash carries its reason in the tooltip.
#:
#: Nothing at all is shown once a stage is DONE. That half was already the
#: intent (`human_eta` returns None "so the card shows NOTHING rather than
#: a placeholder") and it was unreachable on the dense row, which completed
#: against a denominator no stage could ever reach — see `progress`.
ETA_UNKNOWN = "-"

#: Stage-rate samples: memory-first, keyed on the manifest path. GIL-atomic
#: dict ops; a rare lost update costs one sample. (web.py `_stage_rate_memo`
#: is this dict.)
STAGE_RATE_MEMO: dict[str, dict] = {}
STAGE_RATE_KEY = "stage_rate"


def human_eta(seconds: float | None) -> str | None:
    """Compact ETA for the Library bars — None when it cannot be known
    (no rate, nothing left), so the card shows NOTHING rather than a
    placeholder. Deliberately coarse: a rolling-sample ETA is accurate to
    a factor, not a minute."""
    if not seconds or seconds <= 0:
        return None
    if seconds < 90:
        return f"~{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"~{int(minutes)}min"
    hours = minutes / 60
    if hours < 48:
        return f"~{hours:.0f}h"
    days = hours / 24
    if days < 400:
        return f"~{days:.0f}d"
    return f"~{days / 365:.0f}y"


def progress(stages: dict[str, StageStatus], *, fetched: dict | None, embed_mode: str | None
             ) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """(current, target, remaining, workable) per stage, from `from_counts`.

    THE TARGET EACH STAGE IS MEASURED AGAINST, computed once so the bar,
    the "complete" colour and the ETA cannot disagree about what done
    means. They did: the template derived `indexable = in_scope -
    unindexable` for rdf and fts and used raw `in_scope` for the dense row,
    while the card computed `remaining` from `in_scope` for all three. On
    gutenberg — 79,320 in scope, 33 unindexable — that made the dense
    row's target 33 documents no stage will ever finish, so its ETA slot
    could never go quiet."""
    cur = {k: int(stages[k].done or 0) for k in STAGES}
    target = {k: int(stages[k].target or 0) for k in STAGES}
    remaining = {k: max(0, target[k] - cur[k]) for k in STAGES}
    # WHAT A STAGE CAN STILL TAKE. A blocked document waits for a human
    # ("clear errors & retry"), so while the only thing left is blocked
    # the stage is as finished as it can be on its own — and a dash
    # that never resolves is the noise this removes. Unlike `unindexable`
    # it is NOT taken out of the target: retrying can fix it, so the bar
    # legitimately stops short and the badge says why.
    #
    # PER STAGE. This subtracted the COLLECTION-WIDE `blocked` from all
    # three document stages, and `blocked` counts a document stuck at ANY
    # stage. A document blocked at fts has already passed rdf — so two of
    # them were enough to make rdf's slot go silent while the worker was
    # actively parsing two other documents.
    workable = {k: max(0, remaining[k] - int(stages[k].blocked or 0)) for k in STAGES}
    # THE FETCH ROW IS JUDGED THE WAY ITS OWN LABEL IS: `fetched["complete"]`
    # (no pass running and every listed file landed) — the origin listing's
    # directory count includes directories holding nothing readable, and the
    # counter topped out below it: an eternal dash on a finished fetch.
    if fetched and fetched.get("complete"):
        workable["fetch"] = 0
    # A STAGE THE NODE HAS BEEN TOLD NOT TO RUN HAS NOTHING TO SAY. With
    # embedding off, the dense row reported "no progress in the last 30
    # minutes" — true, and read as a fault on a collection doing exactly
    # what it was configured to do.
    if embed_mode == "off":
        workable["embed"] = 0
    return cur, target, remaining, workable


def rates(con, memo_key: str, cur: dict[str, int], remaining: dict[str, int],
          workable: dict[str, int], *, counts_age: float, sample_write
          ) -> tuple[dict, dict, dict, dict]:
    """(rate per hour, ETA, the ETA's reason, samples) per stage, from the
    samples persisted for this manifest (`STAGE_RATE_KEY`, memory first).

    PROOF OF MOTION and synthetic ETAs, one mechanism for EVERY bar: each
    stage's advance is measured across page loads (persisted per
    collection) and turned into a compact ETA. A 40M-document collection
    advancing at a healthy rate moves its bar fractions of a percent per
    hour, which reads as STUCK (operator report, verified live against the
    events table). A rate that measures zero over the window yields
    NOTHING — no placeholder, and never a stale number beside a genuinely
    stalled stage.

    Samples live in PROCESS MEMORY first (all page loads share the web
    process); `sample_write(con, key, payload)` is best-effort persistence
    for the next restart only. Memory is not optional: on a manifest under
    continuous pipeline writes the 200 ms write loses the lock race nearly
    every time (observed live on pubmed after the throughput fix — the
    baseline could not land for an hour), and a sampler that needs a WRITE
    to function turns a reader into a lock contender."""
    from . import db
    stage_rates: dict = {k: None for k in STAGES}
    stage_etas: dict = {k: None for k in STAGES}
    stage_eta_why: dict = {k: None for k in STAGES}
    now_i = int(time.time())
    # WHEN EACH NUMBER WAS TRUE (2026-11-07). The document counts are a
    # scan that lands every TTL (5-6 min on wikidata) and is served from
    # cache between, so a rate measured against the RENDER time divided
    # a delta counted over one span by another: the card said
    # +184.021/h while the worker embedded ~120k documents an hour, and
    # the first window after a restart — baselined on stored counts
    # minutes old — read 1.5x high. A stage's sample carries the time its
    # count was taken; the fetch counter is read live, at now.
    counted_at = now_i - int(counts_age or 0)
    seen_at = {"rdf": counted_at, "fts": counted_at, "embed": counted_at, "fetch": now_i}
    rec_sr = STAGE_RATE_MEMO.get(memo_key)
    if rec_sr is None:
        # warm start after a web restart: the last persisted sample
        raw_sr = db.get_control(con, STAGE_RATE_KEY)
        if raw_sr:
            try:
                rec_sr = json.loads(raw_sr)
            except (ValueError, TypeError):
                rec_sr = None
    samples = {k: 0 for k in STAGES}
    if rec_sr is None or any(k not in rec_sr for k in STAGES):
        # first sight or a pre-upgrade record: fresh baseline
        baseline = {**cur, "ts": now_i,
                    **{k + "_pts": seen_at[k] for k in STAGES},
                    **{k + "_pc": cur[k] for k in STAGES}}
        STAGE_RATE_MEMO[memo_key] = baseline
        sample_write(con, STAGE_RATE_KEY, json.dumps(baseline))
        for k in STAGES:
            if workable[k] > 0:
                stage_etas[k] = ETA_UNKNOWN  # work remains, no history yet
                stage_eta_why[k] = "measuring — no rate yet"
        return stage_rates, stage_etas, stage_eta_why, samples
    # A COUNT THAT SHRANK RE-BASELINES ITS OWN STAGE, not all four. This
    # used to be `any(cur[k] < rec_sr[k] ...)` over every field, so a single
    # dip — a reset, a re-mint, a corpus whose fetch total was revised
    # down — discarded the accumulated rate and sample count of the other
    # three stages as well, and every one of them went back to a
    # placeholder for the next two windows. Each stage's history is its own.
    for k in STAGES:
        if cur[k] < int(rec_sr.get(k) or 0):
            # `_stalled` clears with the rest of it: a stage whose history
            # was just thrown away has not been observed standing still,
            # and a sticky flag made it claim so.
            rec_sr = {**rec_sr, k: cur[k], k + "_ph": None,
                      k + "_n": 0, k + "_stalled": False,
                      k + "_pts": seen_at[k], k + "_pc": cur[k]}
    # LOW-PASS FILTERED rate: an exponential moving average over the
    # per-window rates (alpha 0.3 -> a new window carries 30 %), and an ETA
    # is shown only once TWO windows have contributed. Operator report: raw
    # window-to-window rates swung the ETA from wildly optimistic (a
    # 1000-document batch landing just inside the window) to wildly
    # pessimistic (the window catching the quiet tail of a batch). A
    # zero-progress window drops the rate entirely rather than decaying a
    # stale number into an ever-growing ETA.
    ema_alpha = 0.3
    min_windows = 2
    # A stage that commits in BURSTS (a 1,000-chunk book every six minutes)
    # shows no progress in many 5-minute windows. Such a window is not
    # evidence of a zero rate: the measurement window STRETCHES until the
    # stage moves again — the rate is progress over the time since the LAST
    # progress — and only a stall longer than this drops the rate.
    stall_s = 1800
    samples = {k: int(rec_sr.get(k + "_n") or 0) for k in STAGES}
    stalled = {k: bool(rec_sr.get(k + "_stalled")) for k in STAGES}
    for k in STAGES:
        stage_rates[k] = rec_sr.get(k + "_ph")
    elapsed = now_i - int(rec_sr.get("ts") or now_i)
    if elapsed >= 300:
        progress_ts, progress_count = {}, {}
        for k in STAGES:
            # since the last PROGRESS-bearing sample (older records: the
            # last sample)
            p_ts = int(rec_sr.get(k + "_pts") or rec_sr.get("ts") or now_i)
            p_count = int(rec_sr.get(k + "_pc") if rec_sr.get(k + "_pc") is not None
                          else (rec_sr.get(k) or 0))
            delta = cur[k] - p_count
            span = max(1, seen_at[k] - p_ts)
            prev = rec_sr.get(k + "_ph")
            if delta > 0:
                raw = delta * 3600.0 / span
                ema = raw if not prev else ema_alpha * raw + (1 - ema_alpha) * prev
                # A STAGE SLOWER THAN ONE DOCUMENT AN HOUR still has a rate.
                # `round(ema)` gave 0 for anything under 0.5/h, and 0 is
                # falsy — so the slot said "measuring — no rate yet" forever
                # on a stage that was demonstrably moving.
                stage_rates[k] = round(ema) if ema >= 1 else round(ema, 2)
                samples[k] += 1
                stalled[k] = False
                p_ts, p_count = seen_at[k], cur[k]
            elif span >= stall_s:
                stage_rates[k] = None       # stalled: no number to stand behind
                samples[k] = 0
                stalled[k] = True
            progress_ts[k], progress_count[k] = p_ts, p_count
        updated = {**cur, "ts": now_i,
                   **{k + "_ph": stage_rates[k] for k in STAGES},
                   **{k + "_n": samples[k] for k in STAGES},
                   # PERSISTED, because the stall has to outlive the render
                   # that noticed it. The reason used to be inferred from the
                   # PREVIOUS record's sample count being non-zero — true on
                   # the detecting render and false on every one after it,
                   # since the stall itself writes that count to zero.
                   **{k + "_stalled": stalled[k] for k in STAGES},
                   **{k + "_pts": progress_ts[k] for k in STAGES},
                   **{k + "_pc": progress_count[k] for k in STAGES}}
        STAGE_RATE_MEMO[memo_key] = updated
        sample_write(con, STAGE_RATE_KEY, json.dumps(updated))
    for k in STAGES:
        if workable[k] <= 0:
            continue
        if stage_rates[k] and samples[k] >= min_windows:
            stage_etas[k] = human_eta(remaining[k] * 3600.0 / stage_rates[k])
            stage_eta_why[k] = f"+{stage_rates[k]:,}/h".replace(",", ".")
        else:
            # work remains and the rate is not yet trustworthy: say so,
            # never a number that will be revised 10x. WHICH of the reasons
            # it is goes in the tooltip — the one token used to cover all.
            stage_etas[k] = ETA_UNKNOWN
            stage_eta_why[k] = (
                "no progress in the last 30 minutes"
                if stalled[k]
                else "measuring — one window so far, waiting for a second"
                if stage_rates[k]
                else "measuring — no rate yet")
    return stage_rates, stage_etas, stage_eta_why, samples


# ---- the views ----------------------------------------------------------------

def judged(stages: dict[str, StageStatus], name: str, *, collection=None, mode: str | None = None,
           pause: Any = _READ, embed_mode: str | None = None) -> dict[str, StageStatus]:
    """`stages` with each document stage's holds, state and reason set by
    `holds` / `judge` — the work a stage has is its waiting documents and,
    for embed, its re-embeds (the worker's version walk takes them)."""
    for k, st in stages.items():
        if k == "fetch" or not isinstance(st, StageStatus):
            continue
        found = holds(name, k, collection=collection, mode=mode, pause=pause, embed_mode=embed_mode)
        st.holds = [h.to_json() for h in found]
        work = int(st.waiting or 0) + (int(st.reembed or 0) if k == "embed" else 0)
        st.state, st.reason = judge(work, found)
    return stages


def view(collection, con=None, *, mode: str | None = None, pause: Any = _READ,
         embed_mode: str | None = None, gates: bool = True) -> dict[str, Any]:
    """The bounded status of one collection as a show prints it
    (`hold collection show`'s `status`, 07 §7.3): {stage: StageStatus.to_json()}
    for the three document stages, judged, with `counted_at` (now) and, where
    a number came from the last full count, that count's time per stage.
    Read-only: `con` is the caller's, or the manifest opened read-only.
    `gates=False`: a manifest the node does not hold as a collection — its
    license, copy and disk gates are not judged (there is no collection to
    judge them on), the node's mode and the pause are."""
    import sqlite3
    from contextlib import closing
    own = None
    if con is None:
        p = getattr(collection, "manifest_path", None)
        if p is None or not p.is_file():
            return {"stages": {}, "note": "no manifest: nothing counted"}
        own = con = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    try:
        with closing(own) if own is not None else _nullcontext():
            if embed_mode is None:
                try:
                    from . import db
                    embed_mode = db.get_scope(con).get("embed_mode")
                except Exception:   # noqa: BLE001
                    embed_mode = None
            got = bounded(con)
            stages = {k: v for k, v in got.items() if isinstance(v, StageStatus)}
            judged(stages, collection.name, collection=collection if gates else None, mode=mode,
                   pause=pause, embed_mode=embed_mode)
            out = {k: stages[k].to_json() for k in DOC_STAGES if k in stages}
            for k, e in got.items():
                if not isinstance(e, StageStatus):
                    out[k] = {"name": k, "state": BLOCKED,
                              "reason": f"cannot be counted: {type(e).__name__}: {e}"}
            return {"counted_at": time.time(), "stages": out}
    except Exception as e:   # noqa: BLE001 — a show never raises for its status
        return {"stages": {}, "note": f"cannot be counted: {type(e).__name__}: {e}"}


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def api_counts(counts: dict) -> dict[str, int]:
    """`/api/status`'s `counts` object, its shape as it always was, its
    numbers from the full scan: `errors` is every in-scope document out of
    the selections — blocked plus unindexable (the worker writes the error
    and the retry together, so that is its `error IS NOT NULL` of before)."""
    g = lambda k: int(counts.get(k) or 0)   # noqa: E731
    return {"total": g("total"), "in_scope": g("in_scope"), "rdf_done": g("rdf_done"),
            "fts_done": g("fts_done"), "embed_done": g("embed_done"),
            "summaries": g("summaries"), "errors": g("blocked") + g("unindexable"),
            "chunks": g("chunks"), "tokens": g("tokens")}
