"""The Library card's ETA slot: one invariant, over every state it reaches.

The operator asked for a hand-run check to join the test pool. The hand-run
version rendered two live collections and read the four stage rows off the
screen:

    gutenberg: fetch (-) "one window so far" | rdf (~39min) "+30.291/h"
               fts   (~39min) "+30.842/h"    | embed (-) "no rate yet"
    pubmed:    all four, nothing shown

Two collections on one afternoon is an eyeball, not a test. What follows is
the property that eyeball was looking for, asserted over every state the
slot can reach — and writing it out found four defects first, three of them
introduced the same day the slot was rewritten:

  * `workable` subtracted the COLLECTION-WIDE blocked count from all three
    document stages, so two documents blocked at fts silenced the parsed
    row while the worker was actively parsing two others.
  * the stall reason was inferred from the PREVIOUS record's sample count,
    which the stall itself zeroes — so it was true on the render that
    noticed the stall and false on every one after it.
  * a stage slower than one document an hour rounded to a rate of 0, which
    is falsy, so it said "no rate yet" forever while demonstrably moving.
  * the fetch row's counter is in documents and its target came from the
    origin listing's directory count, which includes directories holding
    nothing readable — 79,287 against 79,389 on the live card, an eternal
    dash on a finished fetch.

Anchoring note, paid for twice: the slot must be matched as the brass span
WITH a title. The rows legitimately carry other parenthesised spans — the
fetch row prints an untitled "(78.900/79.389)" and, with extras, a titled
"(+ 1/9)" — so counting parens per row is invalid. And the tooltips carry
literal EM DASHES: Jinja escapes only & < > " ', so a hyphen-spelled
assertion silently matches nothing.
"""
import re
import time

import pytest

from app import collections as collections_mod, db, web

_STAGES = ("fetch", "rdf", "fts", "embed")

_SLOT = re.compile(
    r'<span style="color:var\(--brass\)" title="([^"]*)">\(([^)]*)\)</span>')


def _slots(html: str):
    """[(tooltip, text)] for every ETA slot on a rendered Library page."""
    return _SLOT.findall(html)


def _render(shelf, worker_state="running"):
    return web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0,
        plugins=[], plugin_profiles={}, plugin_extras={}, corpus_mounts=[],
        corpus_mounts_dir="", worker_state=worker_state)


def _assert_slot_invariant(shelf, html):
    """ONE object, ONE reason, and both exist exactly when the stage has
    work it can take on its own.

    The first clause is the load-bearing one: library.html renders
    title="{{ c.stage_eta_why.X }}" INSIDE {% if c.stage_etas.X %}, so an
    eta set without a why puts a literal title="None" on the card. Jinja
    stringifies None and escapes only & < > " ', so nothing else catches it.
    """
    for k in _STAGES:
        eta, why = shelf["stage_etas"][k], shelf["stage_eta_why"][k]
        assert (eta is None) == (why is None), (
            f"{k}: the slot and its reason are set and cleared together, "
            f"got eta={eta!r} why={why!r}")
        if eta is None:
            continue
        assert eta and why and why.strip(), f"{k}: an empty slot or reason"
        assert (eta == web.ETA_UNKNOWN or eta.startswith("~")
                or eta == "waiting for the index"), (
            f"{k}: {eta!r} is not one of the three families")
        if eta == "waiting for the index":
            assert k == "rdf", "only parsing waits for the index"
        if eta.startswith("~"):
            assert why.startswith("+") and why.endswith("/h"), (
                f"{k}: a duration must be backed by the rate it came from, "
                f"got {why!r}")

    # and over the BYTES, where the dict-level clauses cannot see
    for tooltip, text in _slots(html):
        assert text, "no empty parens"
        assert tooltip.strip(), f"an ETA slot with no reason: ({text})"
        assert "None" not in tooltip, f"a stringified None: {tooltip!r}"
        assert not re.search(r"[0-9],[0-9]{3}", tooltip), (
            f"comma-grouped digits read as a decimal to the operator, and "
            f"one did: {tooltip!r}")
    for jargon in ("TBD", "after fts", "+/h", "+None", "_next_book",
                   "rdf_done", "fts_done", "embed_done", "workable"):
        assert jargon not in html, f"{jargon!r} is developer shorthand"


def _seed(con, rows):
    for i, (in_scope, rdf, fts, emb, err) in enumerate(rows):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done,chunks,error) "
                    "VALUES(?,'g',?,?,?,?,?,?,1,?)",
                    (f"g:{i}", str(i), f"/x{i}", in_scope, rdf, fts, emb, err))


#: (name, document rows, persisted sample record or None, worker_state).
#: Offsets in the record are relative to now, applied below.
_CASES = [
    ("empty", [], None, "running"),
    ("fresh", [(1, 0, 0, 0, None)] * 4, None, "running"),
    ("done", [(1, 1, 1, 1, None)] * 4, None, "running"),
    ("done_but_unindexable",
     [(1, 1, 1, 1, None)] * 3 + [(1, 0, 0, 0, "no txt file")], None, "running"),
    ("blocked_at_fts",
     [(1, 1, 1, 1, None)] * 2 + [(1, 1, 0, 0, "fts: boom")] * 2, None, "running"),
    ("measured", [(1, 1, 1, 0, None)] * 10,
     {"embed_ph": 120, "embed_n": 2, "ts": -600}, "running"),
    ("one_window", [(1, 1, 1, 0, None)] * 10,
     {"embed_ph": 120, "embed_n": 1, "ts": -60}, "running"),
    ("stalled", [(1, 1, 1, 0, None)] * 10,
     {"embed_ph": 120, "embed_n": 3, "embed_pts": -4000, "embed_pc": 0,
      "ts": -600}, "running"),
    ("ahead", [(1, 1, 0, 0, None)] * 300, None, "running"),
    ("ahead_but_paused", [(1, 1, 0, 0, None)] * 300, None, "paused"),
    ("acquire_only", [(1, 1, 0, 0, None)] * 300, None, "acquire-only"),
    ("crawling", [(1, 1, 1, 0, None)] * 10,
     {"embed_n": 1, "embed_pts": -1500, "embed_pc": 0, "ts": -600}, "running"),
]


@pytest.mark.parametrize("name,rows,memo,ws", _CASES,
                          ids=[c[0] for c in _CASES])
def test_the_eta_slot_holds_its_invariant_in_every_state(
        isolated_config, name, rows, memo, ws):
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, rows)
    key = str(c.manifest_path)
    if memo is None:
        web._stage_rate_memo.pop(key, None)
    else:
        now = int(time.time())
        base = {k: 0 for k in _STAGES}
        rec = {**base, **memo}
        for field in ("ts", "embed_pts", "rdf_pts", "fts_pts", "fetch_pts"):
            if field in rec:
                rec[field] = now + rec[field]
        web._stage_rate_memo[key] = rec
    web.invalidate_counts()

    shelf = web._collection_shelf(c, {}, ws)
    _assert_slot_invariant(shelf, _render(shelf, ws))


def test_a_document_blocked_at_one_stage_does_not_silence_another(isolated_config):
    """The counterexample that had to be fixed before the invariant could be
    pinned at all. `workable` subtracted the COLLECTION-WIDE blocked count
    from every document stage, and `blocked` counts a document stuck at ANY
    stage — so two documents blocked at fts were enough to make the parsed
    row go quiet while the worker was actively parsing two others."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, [(1, 1, 1, 1, None)] * 6
               + [(1, 1, 0, 0, "fts: boom")] * 2
               + [(1, 0, 0, 0, None)] * 2)
    web._stage_rate_memo.pop(str(c.manifest_path), None)
    web.invalidate_counts()
    shelf = web._collection_shelf(c, {}, "running")

    assert shelf["blocked"] == 2, "collection-wide, as the badge reports it"
    # The two blocked documents are stuck at FTS — they passed rdf — so
    # they are no part of what parsing has left to do. Under the old
    # arithmetic remaining[rdf] (2) minus collection-wide blocked (2) was
    # zero and this row went silent.
    assert shelf["stage_etas"]["rdf"] is not None, \
        "two documents wait to be PARSED: that row must not go quiet"
    # and fts is not silenced either: the same two unparsed documents will
    # need indexing once parsing reaches them, so its workable count is 2
    # (remaining 4, of which 2 are its own blocked)
    assert shelf["stage_etas"]["fts"] is not None
    _assert_slot_invariant(shelf, _render(shelf))

    # the quiet case, for contrast: every document parsed, and the only
    # thing fts has left IS its blocked pair
    con.execute("UPDATE documents SET rdf_done=1, fts_done=1, embed_done=1 "
                "WHERE error IS NULL")
    web.invalidate_counts()
    quiet = web._collection_shelf(c, {}, "running")
    assert quiet["stage_etas"]["rdf"] is None
    assert quiet["stage_etas"]["fts"] is None, \
        "nothing left but what waits for a human: no placeholder"
    _assert_slot_invariant(quiet, _render(quiet))


def test_a_stall_still_reads_as_a_stall_on_the_next_render(isolated_config):
    """The reason was inferred from the PREVIOUS record's sample count being
    non-zero — true on the render that noticed the stall, false on every one
    after it, because the stall itself writes that count to zero. A stage
    dead for hours went back to saying "measuring"."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, [(1, 1, 1, 0, None)] * 10)
    now = int(time.time())
    web._stage_rate_memo[str(c.manifest_path)] = {
        "rdf": 0, "fts": 0, "embed": 0, "fetch": 0,
        "embed_ph": 120, "embed_n": 3,
        "embed_pts": now - 4000, "embed_pc": 0, "ts": now - 600}
    web.invalidate_counts()

    first = web._collection_shelf(c, {}, "running")
    assert "30 minutes" in first["stage_eta_why"]["embed"]

    web.invalidate_counts()
    again = web._collection_shelf(c, {}, "running")
    assert again["stage_etas"]["embed"] == web.ETA_UNKNOWN
    assert "30 minutes" in again["stage_eta_why"]["embed"], \
        "a stage dead for hours does not go back to 'measuring'"


def test_a_stage_slower_than_one_document_an_hour_still_has_a_rate(isolated_config):
    """`round(ema)` gave 0 for anything under 0.5/h, and 0 is falsy — so the
    slot said "no rate yet" forever on a stage that was demonstrably moving,
    and could never reach the stall reason either (that needs a prior
    window). Rates below 1 keep two decimals."""
    c = collections_mod.default_collection()
    con = db.connect(c)
    _seed(con, [(1, 1, 1, 1, None)] + [(1, 1, 1, 0, None)] * 9)
    now = int(time.time())
    # one document embedded over four hours: 0.25/h
    web._stage_rate_memo[str(c.manifest_path)] = {
        "rdf": 0, "fts": 0, "embed": 0, "fetch": 0,
        "embed_n": 1, "embed_pts": now - 14400, "embed_pc": 0,
        "ts": now - 600}
    web.invalidate_counts()
    shelf = web._collection_shelf(c, {}, "running")

    assert shelf["stage_rates"]["embed"] == 0.25, \
        "a rate under one an hour is a rate, not an absence"
    assert shelf["stage_etas"]["embed"].startswith("~")
    assert shelf["stage_eta_why"]["embed"] == "+0.25/h"
    _assert_slot_invariant(shelf, _render(shelf))
