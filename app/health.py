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

HELD IS THE FOURTH STATE, and it is the operator's. A node the Library
holds at acquire-only does not parse, index, embed or import — by choice
— and the first version of this called that work "running": `rdf 24,012
running` on a node where rdf stands down, the "caught up" lie in
reverse, on the night of the deploy. Held work is named with the mode
that holds it and never exits 1: resuming the node is the answer, and
nothing is wrong. A block that resuming would NOT clear — embed switched
off in the TOML, a license without an acknowledgement, a collection
refused at boot, a stopped archive — stays BLOCKED whatever the mode.

EVERY GATE IS THE WORKER'S OWN. Each verdict here asks the function the
worker asks: `licensing.state_for` (invariant 9), `spaces`' boot check
(invariant 1, read-only), `genesis.archive_stopped`, the node's mode from
state/node.sqlite. A report that decides by a rule of its own is a
second answer to the same question, and the review of this module found
four places where the second answer was "running" and the worker's was
"nothing runs".

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
#: A stage count stops here and says "over": COUNT(*) walks the waiting rows
#: along the covering index, 16.6 s for wikidata's 72.9 M during discovery
#: and 24 s for wikipedia-v8 mid-build, so `node show` took 20-33 s (München,
#: 2026-10-27). Exact below the cap, ~0.25 s at it. The standard bounded
#: count (a LIMIT under the COUNT); the stage's state does not depend on
#: the size, only on whether there is any.
WAITING_CAP = 1_000_000

IDLE, RUNNING, HELD, BLOCKED = "idle", "running", "held", "blocked"

#: What each node mode holds back — READ OFF THE ONE TABLE of what a mode
#: runs (app/node_state.py: MODE_RUNS; operator, 2026-09-19), never a second
#: list that could come to disagree with the worker. rdf is parsing; fts and
#: the archive's imports are indexing; embedding runs only where everything
#: does. `running` holds nothing, and a mode this does not know ('unknown')
#: holds nothing either: better to report work as running than to invent a
#: reason it is not.
_STAGE_NEEDS = {"rdf": "parse", "fts": "index", "embed": "embed", "archive": "index"}


def _held_by_mode(mode: str) -> tuple[str, ...]:
    from . import node_state
    if mode not in node_state.MODE_RUNS:
        return ()
    return tuple(stage for stage, needs in _STAGE_NEEDS.items()
                 if not node_state.runs(mode, needs))


def _held_reason(mode: str | None, stage: str) -> str:
    if mode and stage in _held_by_mode(mode):
        # no "held" here: a HELD row prints its state word first, and the
        # live report read "held — held — the node is…" (staged check)
        return (f"the node is {mode}; this runs when the Library's "
                f"mode is set to running")
    return ""


def _withheld(collection) -> str:
    """Invariant 9, from THE resolution (app/licensing.py): a yellow or red
    collection with no valid acknowledgement is not scanned, indexed,
    embedded or imported, in any mode (worker `_license_allows`). "" when
    nothing is withheld. A collection licensing cannot judge is WITHHELD —
    search and the worker fail closed too, and three answers to one fault
    was the disagreement this module exists to end (verification pass)."""
    try:
        from . import licensing
        if licensing.state_for(collection) != licensing.MISSING:
            return ""
        cls = licensing.license_class_for(collection)
    except Exception as e:  # noqa: BLE001 — a health check never raises
        return (f"withheld: its license state cannot be judged "
                f"({type(e).__name__}: {e}) — invariant 9")
    return (f"withheld: license_class={cls} needs an acknowledgement of the "
            f"terms — invariant 9")


def _copy_state(collection) -> tuple[str, str]:
    """(what blocks embedding and imports, what to say about the copy) —
    03 §3.6, from THE derivation (app/copy_state.py), the one the worker and
    the search read. While the node only REPORTS quarantine the first is ""
    and the second says what enforcing WOULD withhold, so the first of step
    14's two deploys shows the operator the day it would bite before it does.
    A health check never raises."""
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


@dataclass
class Stage:
    name: str
    waiting: int
    state: str
    reason: str = ""
    #: True when `waiting` is the cap (WAITING_CAP): there are more
    over: bool = False

    def line(self) -> str:
        mark = {IDLE: "  ", RUNNING: "->", HELD: "..", BLOCKED: "!!"}[self.state]
        n = f"over {self.waiting:,}" if self.over else f"{self.waiting:,}"
        got = f"{mark} {self.name:<7} {n:>17}  {self.state}"
        return f"{got}  — {self.reason}" if self.reason else got


@dataclass
class Report:
    collection: str
    stages: list[Stage] = field(default_factory=list)
    errors: int = 0
    note: str = ""
    #: the node's mode this report was judged against ("" = not given)
    mode: str = ""
    #: the origin's inconsistencies flagged, per code (app/issues.py; 06
    #: §6.1, amended 2026-10-25) — shown, never blocking: they are the
    #: origin's, and no stage waits on them
    issues: dict = field(default_factory=dict)

    def blocked(self) -> list[Stage]:
        return [s for s in self.stages if s.state == BLOCKED]


def _embed_reason(collection, scope: dict) -> str:
    """Why embed cannot start, or "" when it can."""
    mode = str(scope.get("embed_mode") or "").lower()
    if mode == "off":
        return (f"embed is OFF for this collection — set it in "
                f"config/collections/{collection.name}.toml")
    from . import disk_guard
    return disk_guard.vectors_short(collection) or ""


def inspect(con, collection, *, errors: bool = False, mode: str | None = None) -> Report:
    """One collection's stages, as they stand right now, judged against
    the node's `mode` when one is given (see HELD, above).

    `errors=False` by default because counting them is the single most
    expensive thing here: `error IS NOT NULL` is in no index, so proving
    there are NONE — the common case, and pubmed's — walks the whole
    in_scope range with a table lookup per row. MEASURED: 43.56 s on
    pubmed to return 0, against 0.45 s for a stage count. A check that
    takes a minute is a check nobody runs.
    """
    report = Report(collection=collection.name, mode=mode or "")
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
    try:
        from . import issues
        report.issues = issues.counts(con)      # the code index alone
    except Exception:       # noqa: BLE001 — a health check never raises
        report.issues = {}

    withheld = _withheld(collection)
    quarantined, said = _copy_state(collection)
    if said:
        report.note = (report.note + "   " if report.note else "") + said
    for name, where in _WAITING.items():
        try:
            waiting = con.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {_INDEXED} WHERE {where} "
                f"LIMIT {WAITING_CAP + 1})").fetchone()[0]
        except Exception as e:  # noqa: BLE001 — NAMED, never skipped. A stage
            # that silently vanished from the report would read as an
            # all-clear, which is the one failure a health check must not
            # have: it would be the "idle — pipeline is caught up" line
            # again, in a new place. `INDEXED BY` raises outright when the
            # index is absent, so this is reachable.
            report.stages.append(
                Stage(name, 0, BLOCKED, f"cannot be counted: {type(e).__name__}: {e}"))
            continue
        # a copy in quarantine is not embedded for; its records and its text
        # index are what its tests read, and go on (03 §3.6)
        reason = withheld or (
            (quarantined or _embed_reason(collection, scope)) if name == "embed" else "")
        held = _held_reason(mode, name)
        if not waiting:
            state = IDLE
            # A stage with nothing to do is FINE even when switched off:
            # "off and finished" is not a problem to report, and reporting
            # it would make the check cry wolf on every idle collection.
            reason = ""
        elif reason:
            state = BLOCKED         # resuming the node would not clear this
        elif held:
            state, reason = HELD, held
        else:
            state = RUNNING
        over = waiting > WAITING_CAP
        report.stages.append(Stage(name, min(waiting, WAITING_CAP), state, reason, over))
    return report


def archive_gap(con, collection, mode: str | None = None) -> Stage | None:
    """Documents the declared archive could serve and has not.

    This is the one that hid twice, because it is invisible from the
    stage counts: those documents ARE waiting for embed, so they look
    like ordinary encoder backlog.

    "COULD" IS THE HONEST WORD, and the earlier wording here said the
    vectors "already exist and are free". Neither half was measured. A
    shard finalises when nothing in its range is still UPSTREAM of fts —
    not when everything in its range received a vector — so being inside
    the range does not prove the archive holds one; and finalising
    DELETES the shard files, so reopening costs a re-fetch rather than
    nothing.

    What the numbers do support, on pubmed 2026-09-13: the archive
    served 35,417,887 of 35,456,211 in-range documents (99.89%), and the
    residue is timing-shaped rather than a coverage hole — 35,858 of the
    38,324 fall in shards 27-36, the ones fts was crossing as the archive
    drained, against 2,466 across shards 0-26. Likely, and worth the
    re-fetch to find out. Not certain, and this must not say it is.

    WHERE THE DOCUMENT SITS DECIDES THE ANSWER. Only a FINAL shard never
    revisits its range, so only documents under one are told to run
    `genesis recheck`. The first version said so for every shard, and on
    the night of the deploy — `recheck` had just reopened 30 shards and
    all 38,324 documents — it sent the operator back to the command they
    had just run. A REJECTED shard is blocked for its own reason. A STOPPED
    archive — a shard rejected before any shard proved the archive — is
    blocked whole: nothing more is fetched or imported and its ranges go
    to the local encoder (`genesis.archive_stopped`, the worker's own
    test). Only then is an OPEN shard "being served" — or held by the
    node's mode (acquire-only fetches shards and does not import them).
    """
    from .bundles import genesis
    archive = genesis.ARCHIVES.get(getattr(collection, "genesis_archive", "") or "")
    if archive is None or archive.local or not archive.ids_per_shard:
        return None
    try:
        # SELECT *, so `row_proven` sees `proven_at` and `docs` where the
        # ledger has them — `archive_stopped` is the worker's decision and
        # must be taken on the worker's evidence
        rows = con.execute(
            "SELECT * FROM genesis_imports WHERE archive=?",
            (archive.archive_id,)).fetchall()
    except Exception:       # noqa: BLE001
        return None
    # EVERY SHARD THE ARCHIVE DECLARES, not only those with a ledger row:
    # the worker keeps the encoder out of a declared shard's range until the
    # archive delivers it (`_covered_ranges`), so documents under a shard not
    # fetched yet wait for the archive, not for the encoder (verification
    # pass, 2026-09-13)
    by_shard = {r["shard"]: r["state"] for r in rows}
    for s in getattr(archive, "shards", ()) or ():
        by_shard.setdefault(s, "unfetched")
    if not by_shard:
        return None
    spans: dict[str, list[tuple[int, int]]] = {"final": [], "rejected": [], "open": []}
    open_shards = 0
    for shard, state in by_shard.items():
        span = archive.id_range_of(shard)
        if not span:
            continue
        kind = state if state in ("final", "rejected") else "open"
        spans[kind].append(span)
        open_shards += kind == "open"
    # MERGED RANGES, within each kind, so medcpt-pubmed's 37 contiguous 1M
    # shards collapse to a few. Worth saying plainly: merging bought almost
    # NOTHING on its own. I predicted "41 s against 1" and measured 41.2 s,
    # because the cost is per ROW, not per query — one 37M-id range costs
    # what 37 one-million ranges cost; splitting by kind costs nothing more.
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
    # a non-numeric id casts to 0 and would count inside a range from 0;
    # it belongs to no range (genesis.NUMERIC_ID_SQL) — and the check is
    # on the row this already reads for the CAST
    in_range = f"CAST(native_id AS INTEGER) BETWEEN ? AND ? AND {genesis.NUMERIC_ID_SQL}"
    waiting = (f"SELECT {{what}} FROM {_INDEXED} WHERE {_WAITING['embed']} "
               f"AND {in_range}")
    owed = {kind: 0 for kind in spans}
    try:
        for kind, kind_spans in spans.items():
            for span in genesis.merge_spans(kind_spans):
                if con.execute(waiting.format(what="1") + " LIMIT 1",
                               span).fetchone() is None:
                    continue            # nothing here: never pay the count
                owed[kind] += int(con.execute(
                    waiting.format(what="COUNT(*)"), span).fetchone()[0])
    except Exception:       # noqa: BLE001
        return None
    total = sum(owed.values())
    if not total:
        return Stage("archive", 0, IDLE)
    name = collection.name
    withheld = _withheld(collection) or _copy_state(collection)[0]
    if withheld:
        return Stage("archive", total, BLOCKED, withheld)
    try:
        stopped = genesis.archive_stopped(rows)
    except Exception as e:  # noqa: BLE001 — never raise, and never guess "served"
        return Stage("archive", total, BLOCKED,
                     f"the archive's state cannot be judged: {type(e).__name__}: {e}")
    held = _held_reason(mode, "archive")
    if stopped:
        return Stage(
            "archive", total, BLOCKED,
            f"the archive is STOPPED: a shard was rejected before any shard "
            f"proved it, so nothing more is fetched or imported and its ranges "
            f"go to the local encoder — the Library's pre-embedded data panel says "
            f"why, `louise hold vectors import {name} --archive {collection.genesis_archive} --retry` "
            f"puts it back in line")
    if owed["final"] or owed["rejected"]:
        parts = []
        if owed["final"]:
            parts.append(f"{owed['final']:,} under FINAL shard(s), which never "
                         f"revisit them — run `louise dev genesis recheck {name}`")
        if owed["rejected"]:
            parts.append(f"{owed['rejected']:,} under REJECTED shard(s) — "
                         f"the Library's pre-embedded data panel says why; `louise hold vectors "
                         f"import {name} --archive {collection.genesis_archive} --retry` retries them")
        if owed["open"]:
            # the open part follows the node's mode like any other: acquire-
            # only fetches those shards and imports nothing (verification pass)
            parts.append(f"{owed['open']:,} more under open shard(s), "
                         + (f"held — {held}" if held else "being served"))
        return Stage("archive", total, BLOCKED,
                     "document(s) inside the archive's range have no vector: "
                     + "; ".join(parts))
    reason = (f"{owed['open']:,} document(s) wait for the archive's "
              f"{open_shards} open shard(s) to be fetched and imported")
    held = _held_reason(mode, "archive")
    if held:
        return Stage("archive", total, HELD, f"{reason} — {held}")
    return Stage("archive", total, RUNNING, reason)


def _node_mode() -> str:
    """The node's mode, read without creating anything (a check must not)."""
    try:
        from . import node_state
        return node_state.peek_worker_state()
    except Exception:       # noqa: BLE001 — a health check never raises
        return "unknown"


def _boot_refusals(collections) -> dict[str, str]:
    """{name: reason} for every collection the worker's boot check refuses
    — `spaces.report_boot_status`, the same check, read-only
    (pin_if_missing=False). Nothing runs for a refused collection, so its
    backlog is moot and it is reported as the refusal (invariant 1: the
    reason is named)."""
    try:
        from . import spaces
        # a row the check could not judge (`checked: False`) is no refusal:
        # the collection is reported through check()'s own connect instead
        return {r["collection"]: r["message"]
                for r in spaces.report_boot_status(collections)
                if not r["ok"] and r.get("checked", True)}
    except Exception:       # noqa: BLE001 — a health check never raises
        return {}


def check(collections, connect=db.connect, *, errors: bool = False,
          mode: str | None = None) -> tuple[list[Report], list[str]]:
    """Every collection's report, and the blocking lines across all of
    them. An empty second list is the all-clear. `mode` is the node's
    (read from state/node.sqlite when not given)."""
    if mode is None:
        mode = _node_mode()
    refused = _boot_refusals(collections)
    reports, blocking = [], []
    for collection in collections:
        if collection.name in refused:
            report = Report(collection=collection.name, mode=mode)
            # "FAILS the boot check", not "was refused": the worker checks at
            # boot and when a collection is added, so one that changed
            # under a running worker (the encoder repointed) is still being
            # served — and is refused at its next boot (verification pass)
            report.stages.append(Stage(
                "boot", 0, BLOCKED,
                f"fails the boot check — {refused[collection.name]}; the next "
                f"boot refuses it: not served, shared or searched (invariant 1)"))
            reports.append(report)
            blocking += [f"{collection.name}/{s.name}: {s.reason}" for s in report.stages]
            continue
        try:
            con = connect(collection=collection)
        except Exception as e:      # noqa: BLE001
            reports.append(Report(collection=collection.name, mode=mode,
                                   note=f"cannot open: {type(e).__name__}"))
            continue
        report = inspect(con, collection, errors=errors, mode=mode)
        gap = archive_gap(con, collection, mode)
        if gap is not None:
            report.stages.append(gap)
        reports.append(report)
        for stage in report.blocked():
            blocking.append(f"{collection.name}/{stage.name}: {stage.reason}")
    return reports, blocking


def render(reports: list[Report]) -> str:
    out = []
    mode = next((r.mode for r in reports if r.mode), "")
    if mode:
        out += [f"node mode: {mode}", ""]
    for r in reports:
        head = f"{r.collection}"
        if r.errors:
            head += f"   ({r.errors:,} errored document(s) — a human must clear these)"
        if r.note:
            head += f"   {r.note}"
        out.append(head)
        for s in r.stages:
            out.append(s.line())
        if r.issues:
            out.append("   origin inconsistencies: "
                       + ", ".join(f"{c} {n:,}" for c, n in sorted(r.issues.items()))
                       + f" — `louise hold issues list {r.collection}`")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
