"""Is the pipeline stuck, and if so why — one command, one answer.

WHY THIS EXISTS. "pubmed is stuck" was reported three times in two days
and was a different thing each time:

  1. five archive shards `applied` with their files deleted, which the
     drain would not walk and the fetcher would not re-fetch;
  2. 38,324 documents inside a FINAL shard's range, stranded because
     `final` is decided from documents still upstream of fts;
  3. not stuck at all — 4,538,777 documents past the archive's coverage,
     waiting for an encoder that `embed = "off"` had switched off.

All three looked identical from outside, because the worker's own
summary of itself was the line "idle — pipeline is caught up". A
disabled stage and a finished one return no row from the same selection
query, and nothing distinguished them.

WHAT THIS REPORTS, and the distinction that matters: a stage with no
work is IDLE and fine. A stage with work it cannot start is BLOCKED, and
`blocked()` is non-empty exactly then — so this is usable as a check,
not just as a page to read. The reason is always named.

NOT A REPLACEMENT FOR THE LOG. This answers "what is true now"; the
worker log read as a time series answers "how fast", which is the first
benchmark (CLAUDE.md) and a different question.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import db

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
#: row, 4.58M of them.
#:
#: The cost of leaving it out is that an errored document counts as
#: waiting. That is a handful (33 on gutenberg, 0 on pubmed) against
#: millions, it is reported beside the count rather than hidden, and
#: unlike the genesis backlog query this feeds no scheduling decision —
#: nothing starves because a report is 33 high.
_WAITING = {
    "rdf": "in_scope=1 AND rdf_done=0",
    "fts": "in_scope=1 AND rdf_done=1 AND fts_done=0",
    "embed": "in_scope=1 AND rdf_done=1 AND fts_done=1 AND embed_done=0",
}
_INDEXED = "documents INDEXED BY idx_documents_scope"

IDLE, RUNNING, BLOCKED = "idle", "running", "blocked"


@dataclass
class Stage:
    name: str
    waiting: int
    state: str
    reason: str = ""

    def line(self) -> str:
        mark = {IDLE: "  ", RUNNING: "->", BLOCKED: "!!"}[self.state]
        got = f"{mark} {self.name:<7} {self.waiting:>12,}  {self.state}"
        return f"{got}  — {self.reason}" if self.reason else got


@dataclass
class Report:
    collection: str
    stages: list[Stage] = field(default_factory=list)
    errors: int = 0
    note: str = ""

    def blocked(self) -> list[Stage]:
        return [s for s in self.stages if s.state == BLOCKED]


def _embed_reason(collection, scope: dict) -> str:
    """Why embed cannot start, or "" when it can."""
    mode = str(scope.get("embed_mode") or "").lower()
    if mode == "off":
        return (f"embed is OFF for this collection — set it in "
                f"config/collections/{collection.name}.toml")
    return ""


def inspect(con, collection, *, errors: bool = False) -> Report:
    """One collection's stages, as they stand right now.

    `errors=False` by default because counting them is the single most
    expensive thing here: `error IS NOT NULL` is in no index, so proving
    there are NONE — the common case, and pubmed's — walks the whole
    in_scope range with a table lookup per row. MEASURED: 43.56 s on
    pubmed to return 0, against 0.45 s for a stage count. A check that
    takes a minute is a check nobody runs.
    """
    report = Report(collection=collection.name)
    # BOTH reads guarded, and the scope one is not an afterthought: it was
    # outside the try, so a manifest this could not read crashed the check
    # that exists to report unreadable manifests. Its own test found it.
    try:
        scope = db.get_scope(con)
        if errors:
            report.errors = con.execute(
                "SELECT COUNT(*) FROM documents WHERE in_scope=1 AND error IS NOT NULL"
            ).fetchone()[0]
    except Exception:       # noqa: BLE001 — a health check never raises
        report.note = "manifest unreadable"
        return report

    for name, where in _WAITING.items():
        try:
            waiting = con.execute(
                f"SELECT COUNT(*) FROM {_INDEXED} WHERE {where}").fetchone()[0]
        except Exception as e:  # noqa: BLE001 — NAMED, never skipped. A stage
            # that silently vanished from the report would read as an
            # all-clear, which is the one failure a health check must not
            # have: it would be the "idle — pipeline is caught up" line
            # again, in a new place. `INDEXED BY` raises outright when the
            # index is absent, so this is reachable.
            report.stages.append(
                Stage(name, 0, BLOCKED, f"cannot be counted: {type(e).__name__}: {e}"))
            continue
        reason = _embed_reason(collection, scope) if name == "embed" else ""
        if not waiting:
            state = IDLE
            # A stage with nothing to do is FINE even when switched off:
            # "off and finished" is not a problem to report, and reporting
            # it would make the check cry wolf on every idle collection.
            reason = ""
        elif reason:
            state = BLOCKED
        else:
            state = RUNNING
        report.stages.append(Stage(name, waiting, state, reason))
    return report


def archive_gap(con, collection) -> Stage | None:
    """Documents the declared archive could serve and has not.

    This is the one that hid twice, because it is invisible from the
    stage counts: those documents ARE waiting for embed, so they look
    like ordinary encoder backlog, while the vectors for them already
    exist and are free. Non-zero here means the archive owes work.
    """
    from .bundles import genesis
    archive = genesis.ARCHIVES.get(getattr(collection, "genesis_archive", "") or "")
    if archive is None or archive.local or not archive.ids_per_shard:
        return None
    try:
        rows = con.execute(
            "SELECT shard, state FROM genesis_imports WHERE archive=?",
            (archive.archive_id,)).fetchall()
    except Exception:       # noqa: BLE001
        return None
    if not rows:
        return None
    # MERGED RANGES, so medcpt-pubmed's 37 contiguous 1M shards collapse
    # to one. Worth saying plainly: merging bought almost NOTHING on its
    # own. I predicted "41 s against 1" and measured 41.2 s, because the
    # cost is per ROW, not per query — one 37M-id range costs what 37
    # one-million ranges cost.
    #
    # WALK THE SMALLER SIDE. That is where the win was. MEASURED on the
    # live pubmed manifest, all three returning the same 38,324:
    #
    #     over the 37M-id range (native_int index)     40.60 s
    #     over the 4.58M embed-waiting set (scope)      5.47 s
    #     existence only, LIMIT 1                       0.00 s
    #
    # So: ask whether ANY exists first — instant, and the answer once the
    # archive has been drained, which is the case an operator runs this
    # in most often — and pay the 5.5 s count only to say how many.
    spans = sorted(r for s in (x["shard"] for x in rows)
                   for r in (archive.id_range_of(s),) if r)
    merged: list[tuple[int, int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    in_range = ("CAST(native_id AS INTEGER) BETWEEN ? AND ?")
    waiting = (f"SELECT {{what}} FROM {_INDEXED} WHERE {_WAITING['embed']} "
               f"AND {in_range}")
    owed = 0
    try:
        for span in merged:
            if con.execute(waiting.format(what="1") + " LIMIT 1",
                           span).fetchone() is None:
                continue                # nothing here: never pay the count
            owed += int(con.execute(
                waiting.format(what="COUNT(*)"), span).fetchone()[0])
    except Exception:       # noqa: BLE001
        return None
    if not owed:
        return Stage("archive", 0, IDLE)
    finals = sum(1 for r in rows if r["state"] == "final")
    return Stage(
        "archive", owed, BLOCKED,
        f"{owed:,} document(s) inside the archive's range have no vector, "
        f"and {finals}/{len(rows)} shard(s) are final — run "
        f"`louise genesis recheck {collection.name}`")


def check(collections, connect=db.connect, *, errors: bool = False) -> tuple[list[Report], list[str]]:
    """Every collection's report, and the blocking lines across all of
    them. An empty second list is the all-clear."""
    reports, blocking = [], []
    for collection in collections:
        try:
            con = connect(collection=collection)
        except Exception as e:      # noqa: BLE001
            reports.append(Report(collection=collection.name,
                                   note=f"cannot open: {type(e).__name__}"))
            continue
        report = inspect(con, collection, errors=errors)
        gap = archive_gap(con, collection)
        if gap is not None:
            report.stages.append(gap)
        reports.append(report)
        for stage in report.blocked():
            blocking.append(f"{collection.name}/{stage.name}: {stage.reason}")
    return reports, blocking


def render(reports: list[Report]) -> str:
    out = []
    for r in reports:
        head = f"{r.collection}"
        if r.errors:
            head += f"   ({r.errors:,} errored document(s) — a human must clear these)"
        if r.note:
            head += f"   {r.note}"
        out.append(head)
        for s in r.stages:
            out.append(s.line())
        out.append("")
    return "\n".join(out).rstrip() + "\n"
