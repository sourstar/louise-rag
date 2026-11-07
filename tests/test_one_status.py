"""ONE STATUS — every surface reports a collection's stages the same way.

THE OPERATOR, 2026-11-07, verbatim: "Architecture: are the card display a
common code or are per collection? The interface with which a model status
is queried must me common, as the fetching, parsing, indexing and
embedding". The card was common code; the status it showed was computed
three times with three definitions — the card's scan (app/web.py),
`node show` / `node health` (app/health.py), `/api/status` (its own SQL) —
and a fourth time for what holds embedding (`node_state.embedding_blockers`).
On one manifest, before app/status.py (each one an assertion below):

  * a collection paused on its own: the card said "paused … the worker runs
    nothing for it"; `node show` said every stage was RUNNING, no reason;
  * the node in acquire-only: the card said 'The node is in "download/seeding
    only" mode …', `node show` said "the node is acquire-only; this runs when
    the Library's mode is set to running" — two sentences for one fact;
  * embed off: the Library's dialog said '… embed setting is "off": set it …',
    `node show` said "embed is OFF for this collection — set it in …";
  * a document done at an older EMBED_VERSION: the card counted it "awaiting
    the encoder" (the worker's version walk takes it), `node show` called
    the embed stage idle;
  * an earlier version still held and searched (operator, the same day: the
    grey segment): no surface counted it;
  * `hold collection show` carried no stage status at all.

The surfaces are read as their users read them: the card's shelf and its
rendered HTML, `node show`'s JSON, `node health`'s text, `/api/status`
over HTTP, `hold collection show --json` from the command line.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
from dataclasses import replace

import pytest

from app import collections as collections_mod, config, db, health, node_show, node_state, web

NAME = "gutenberg"


def _seed(con) -> None:
    """One manifest with every kind of document a stage can hold."""
    V, E = config.FTS_VERSION, config.EMBED_VERSION
    rows = (
        # (n, in_scope, rdf, fts, embed, fts_version, embed_version, chunks, error)
        (5, 1, 1, 1, 1, V, E, 2, None),            # done
        (3, 1, 0, 0, 0, 0, 0, 0, None),            # waiting to be parsed, never indexed
        (4, 1, 0, 0, 0, V, E, 3, None),            # re-parsed: the earlier version still held
        (2, 1, 1, 0, 0, 0, 0, 0, None),            # waiting to be indexed
        (2, 1, 1, 1, 0, V, 0, 2, None),            # waiting for the encoder
        (2, 1, 1, 0, 0, 0, 0, 0, "fts: boom"),     # blocked at fts
        (1, 1, 0, 0, 0, 0, 0, 0, "no txt file"),   # unindexable (the origin has no text)
        (3, 1, 1, 1, -1, V, 0, 2, None),           # declined: not embedded here, by choice
        (1, 1, 1, 1, 1, V, -1, 2, None),           # done at an older version: re-embedded
        (2, 0, 0, 0, 0, 0, 0, 0, None),            # out of scope
    )
    i = 0
    for n, scope, rdf, fts, emb, fv, ev, chunks, err in rows:
        for _ in range(n):
            i += 1
            con.execute(
                "INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,fts_done,"
                "embed_done,fts_version,embed_version,chunks,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{NAME}:{i}", NAME, str(i), f"/x/{i}", scope, rdf, fts, emb, fv, ev, chunks, err))


#: what the fixture holds, counted by hand
WAITING = {"rdf": 3 + 4, "fts": 2, "embed": 2}
ERRORED = {"rdf": 1, "fts": 2, "embed": 0}
SUPERSEDED = {"fts": 4, "embed": 4}
REEMBED = 1


@pytest.fixture
def node(isolated_config):
    c = collections_mod.by_name(NAME)
    con = db.connect(c)
    _seed(con)
    scope = db.get_scope(con)
    db.set_scope(con, {**scope, "embed_mode": "all"})
    con.close()
    node_state.set_worker_state("running")
    web.invalidate_counts()
    yield c
    web.invalidate_counts()


def _counted(c) -> None:
    """The card's scan, landed and stored, as the web process leaves it."""
    web.invalidate_counts()
    assert web._persist_counts(c, web._document_counts_query(c))


def _card(c, mode: str) -> tuple[dict, str]:
    shelf = web._collection_shelf(c, {}, mode)
    html = web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0, plugins=[],
        plugin_profiles={}, plugin_extras={}, corpus_mounts=[], corpus_mounts_dir="",
        worker_state=mode)
    return shelf, html


def _node_show() -> dict:
    (row,) = [r for r in node_show.show()["collections"] if r["name"] == NAME]
    return {s["stage"]: s for s in row["stages"]}


def _louise(*argv) -> tuple[int, str]:
    from app import louise
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = louise.main(list(argv))
    return code, out.getvalue()


def _collection_show(c) -> dict:
    code, out = _louise("hold", "collection", "show", NAME, "--root",
                        str(collections_mod.louise_root() / "collections"))
    assert code == 0, out
    got = json.loads(out)
    assert "status" in got, "`hold collection show` reports the copy's stages (07 §7.3)"
    return got["status"]["stages"]


def _api_status() -> dict:
    from fastapi.testclient import TestClient
    from app.api import app
    return TestClient(app).get("/api/status").json()


def test_every_surface_counts_the_same_stages(node):
    _counted(node)
    shelf, html = _card(node, "running")
    shown = _node_show()
    reports, _ = health.check([node], mode="running")
    by = {s.name: s for s in reports[0].stages}
    # first what every release could already show: the card's "awaiting the
    # encoder" against node show's embed stage
    assert shelf["embed_ready"] == WAITING["embed"] + REEMBED
    assert (shown["embed"]["waiting"], shown["embed"].get("reembed", 0)) == (WAITING["embed"], REEMBED), \
        "node show: the embed stage's work is what the card says awaits the encoder"
    held = _collection_show(node)
    assert shelf.get("status"), "the card carries the status it draws (app/status.py)"
    card = shelf["status"]
    for k in ("rdf", "fts", "embed"):
        # the full scan and the bounded count, where both are exact: one number
        assert card[k]["waiting"] == WAITING[k] == shown[k]["waiting"] == by[k].waiting \
            == held[k]["waiting"], k
        assert card[k]["errored"] == ERRORED[k] == shown[k].get("errored", 0) == by[k].errored \
            == held[k].get("errored", 0), k
        assert card[k]["state"] == shown[k]["state"] == by[k].state == held[k]["state"] == "running", k
    for k in ("fts", "embed"):
        assert card[k]["superseded"] == SUPERSEDED[k] == shown[k]["superseded"] \
            == by[k].superseded == held[k]["superseded"], k
    # a re-embed: the card's "awaiting the encoder" is the stage's waiting plus
    # the older versions the worker's walk takes — and every show says both
    assert held["embed"]["reembed"] == card["embed"]["reembed"] == REEMBED
    # the declined and the unindexable are in no target and nobody's waiting
    assert card["embed"]["declined"] == 3 and card["rdf"]["unindexable"] == 1
    assert shelf["stage_targets"]["embed"] == 23 - 1 - 3 and shelf["stage_targets"]["rdf"] == 22
    assert "3 blocked" not in html and shelf["blocked"] == 2 and shelf["unindexable"] == 1
    # /api/status: its shape as it was, its numbers the card's
    api = _api_status()["counts"]
    assert set(api) == {"total", "in_scope", "rdf_done", "fts_done", "embed_done", "summaries",
                        "errors", "chunks", "tokens"}, "the JSON shape is unchanged"
    assert (api["rdf_done"], api["fts_done"], api["embed_done"]) == \
        (shelf["rdf_done"], shelf["fts_done"], shelf["embed_done"])
    assert api["errors"] == shelf["blocked"] + shelf["unindexable"] == 3
    assert api["in_scope"] == shelf["in_scope"] == 23 and api["total"] == 25
    # and node health prints the numbers node show returns
    text = health.render(reports)
    assert re.search(r"rdf\s+7\s+running\s+\(1 errored, not waiting\)", text), text


def test_api_status_follows_the_apis_own_actions(node, monkeypatch):
    """`/api/status` reads the card's cached counts now, where it scanned on
    every request: a reset through the API drops them, as the Library's
    actions do, so the next status is the reset's."""
    from fastapi.testclient import TestClient
    from app.api import app
    monkeypatch.setattr(config, "ADMIN_TOKEN", "sekrit")
    client = TestClient(app)
    before = client.get("/api/status").json()["counts"]
    assert before["fts_done"] == 5 + 2 + 3 + 1
    r = client.post("/api/reset", json={"stage": "fts"}, headers={"X-Admin-Token": "sekrit"})
    assert r.status_code == 200
    after = client.get("/api/status").json()["counts"]
    assert after["fts_done"] == 0 and after["embed_done"] == before["embed_done"], after
    # a row stored before the scan carried /api/status's counts is not read as 0
    c = node
    stored = {k: v for k, v in web._document_counts_query(c).items() if k not in ("chunks", "tokens")}
    web.invalidate_counts()
    assert web._persist_counts(c, stored)
    assert client.get("/api/status").json()["counts"]["chunks"] == after["chunks"] > 0


def _reasons(c, mode: str) -> tuple[dict, dict, dict, list[str]]:
    _counted(c)
    shelf, _ = _card(c, mode)
    shown = _node_show()
    reports, _ = health.check([c], mode=mode)
    return shelf, shown, {s.name: s for s in reports[0].stages}, shelf["embed_blocked"]


def test_a_collection_paused_on_its_own_is_held_everywhere_in_one_sentence(node):
    """The card said paused; `node show` said every stage was running."""
    node_state.set_paused(NAME, True, by="louise node pause gutenberg")
    shelf, shown, by, blocked = _reasons(node, "running")
    (said,) = blocked
    assert said.startswith(f'{NAME} is paused on its own, by "louise node pause gutenberg"'), said
    for k in ("rdf", "fts", "embed"):
        assert shown[k]["state"] == by[k].state == "held", (k, shown[k])
        assert shown[k]["reason"] == by[k].reason == said, (k, shown[k]["reason"])
    assert shelf["status"]["embed"]["reason"] == said
    assert _collection_show(node)["embed"]["reason"] == said


def test_the_nodes_mode_is_one_sentence_on_every_surface(node):
    """Two sentences for acquire-only: the card's and node health's."""
    node_state.set_worker_state("acquire-only")
    shelf, shown, by, blocked = _reasons(node, "acquire-only")
    (said,) = blocked
    assert said.startswith('The node is in "download/seeding only" mode'), said
    for k in ("rdf", "fts", "embed"):
        assert shown[k]["state"] == by[k].state == "held", k
        assert shown[k]["reason"] == by[k].reason == said, (k, shown[k]["reason"])
    assert shelf["status"]["rdf"]["reason"] == said
    assert _collection_show(node)["fts"]["reason"] == said


def test_embed_off_is_one_sentence_on_every_surface(node, monkeypatch):
    """The Library's dialog and node health said it two ways."""
    con = db.connect(node)
    db.set_scope(con, {**db.get_scope(con), "embed_mode": "off"})
    con.close()
    shelf, shown, by, blocked = _reasons(node, "running")
    assert blocked == [], "the card says off at its select, not again in a note"
    dialog = node_state.embedding_blockers(NAME, "off")      # the embed-mode switch's answer
    (said,) = dialog
    assert shown["embed"]["state"] == by["embed"].state == "blocked"
    assert shown["embed"]["reason"] == by["embed"].reason == said, shown["embed"]["reason"]
    assert shelf["status"]["embed"]["reason"] == said
    assert _collection_show(node)["embed"]["reason"] == said
    assert shown["rdf"]["state"] == "running", "only the embed stage is switched off"


def test_the_archive_gap_is_the_cards_number(isolated_config):
    """An archive collection: the card's pre-embedded row names the documents
    inside the declared range without a vector, and node health's archive
    stage counts the same ones."""
    c = replace(collections_mod.default_collection(), genesis_archive="medcpt-pubmed")
    con = db.connect(c)
    for nid, embed in ((1, 1), (2, 1), (3, 0), (4, 0), (40_000_000, 0)):
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,fts_done,"
                    "embed_done,chunks) VALUES(?,?,?,?,1,1,1,?,3)",
                    (f"pubmed:{nid}", "pubmed", str(nid), f"/x/{nid}", embed))
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) "
                "VALUES('medcpt-pubmed','0','final',2,0)")
    _counted(c)
    shelf, _ = _card(c, "running")
    reports, _ = health.check([c], mode="running")
    gap = next(s for s in reports[0].stages if s.name == "archive")
    assert shelf["genesis"]["in_range_waiting"] == gap.waiting == 2
    by = {s.name: s for s in reports[0].stages}
    assert shelf["status"]["embed"]["waiting"] == by["embed"].waiting == 3


# ---- the earlier version, grey (operator, 2026-11-07) --------------------------

def test_the_dense_bar_shows_the_earlier_version_grey(node):
    """"when the embedding index goes down because a new version is present
    the delta in the bar shall be shown in a different color (something like
    grey, to symbolize that it's present but not active)". gutenberg's origin
    republished 11,253 books: re-parsed, marks cleared, the old vectors still
    in the table and still searched."""
    _counted(node)
    shelf, html = _card(node, "running")
    target = shelf["stage_targets"]["embed"]
    done = shelf["embed_done"]
    assert (target, done) == (19, 6)
    # the count says it in plain words; the share stays done / target
    assert (f"{done} / {target} · 31,6 % · <span data-superseded=\"embed\"") in html
    assert "4 earlier version (searched until re-embedded)</span>" in html
    # done in the stage's colour, then the earlier version grey, then empty
    bar = re.search(r'<span style="width:([\d.]+)%;background:var\(--lapis\)"></span>'
                    r'<span data-superseded-bar="embed" style="width:([\d.]+)%;'
                    r'background:var\(--superseded\)"></span></div>', html)
    assert bar, "the grey segment follows the done one"
    assert (float(bar.group(1)), float(bar.group(2))) == (round(100 * 6 / 19, 1), round(100 * 4 / 19, 1))
    # the lexical bar: the same, for text that changed
    assert "4 earlier version (searched until re-indexed)</span>" in html
    assert 'data-superseded-bar="fts"' in html
    from pathlib import Path
    css = (Path(web.__file__).parent / "static" / "admin.css").read_text(encoding="utf-8")
    assert css.count("--superseded:") == 4, "a token in every theme, the light one included"


def test_no_earlier_version_draws_no_grey(node):
    con = db.connect(node)
    con.execute("UPDATE documents SET fts_version=0, embed_version=0 WHERE embed_done=0")
    con.close()
    _counted(node)
    _, html = _card(node, "running")
    assert "data-superseded" not in html and "earlier version" not in html


def test_every_surface_reports_the_same_superseded_number(node):
    _counted(node)
    shelf, _ = _card(node, "running")
    shown = _node_show()
    held = _collection_show(node)
    for k in ("fts", "embed"):
        assert shelf["stage_superseded"][k] == shown[k]["superseded"] == held[k]["superseded"] \
            == SUPERSEDED[k], k


def test_superseded_over_the_bound_comes_from_the_stored_scan_and_says_when(node, monkeypatch):
    """Over ERRORED_EXACT_BELOW the bounded path does not read the versions
    (a table lookup a row): it takes the last full count, with its time."""
    from app import status
    _counted(node)
    con = db.connect(node)
    got = status.bounded(con, exact_below=3)
    assert got["embed"].superseded == SUPERSEDED["embed"] and "superseded" in got["embed"].from_scan
    assert not got["rdf"].exact, "rdf's 8 at the stage are over a bound of 3"
    live = status.bounded(con)
    assert live["embed"].superseded == SUPERSEDED["embed"] and "superseded" not in live["embed"].from_scan


def test_over_the_cap_node_show_says_the_cards_number_and_when(node, monkeypatch):
    """München, 2026-11-07: wikidata's embed read "1000000, over" in `node
    show` against the card's 41,455,026. Over the cap, the waiting count is
    the stored full count the card shows, with its time; a stored count
    under the cap is older than the live one and the cap stands."""
    from app import status
    _counted(node)
    con = db.connect(node)
    got = status.bounded(con, cap=3)
    assert got["rdf"].waiting == WAITING["rdf"] and not got["rdf"].over
    assert got["rdf"].errored == ERRORED["rdf"]
    assert "waiting" in got["rdf"].from_scan and "errored" in got["rdf"].from_scan
    assert "waiting" not in got["embed"].from_scan, "2 under a cap of 3: counted live"
    monkeypatch.setattr(health, "WAITING_CAP", 3)
    shown = _node_show()
    shelf, _ = _card(node, "running")
    assert shown["rdf"]["waiting"] == WAITING["rdf"] == shelf["status"]["rdf"]["waiting"]
    assert "over" not in shown["rdf"] and shown["rdf"]["counted_at"] > 0
    reports, _ = health.check([node], mode="running")
    (rdf,) = [s for s in reports[0].stages if s.name == "rdf"]
    assert "full count" in rdf.line() and "over" not in rdf.line()
    # a stored count UNDER the cap is stale against the live count: the cap stays
    counts, at, scan_s = status.read_stored(con)
    stale = status.bounded(con, cap=3, stored=({**counts, "waiting_rdf": 2}, at, scan_s))
    assert stale["rdf"].over and stale["rdf"].waiting == 3 and "waiting" not in stale["rdf"].from_scan


def test_the_superseded_predicate_is_what_the_writers_leave(node):
    """`<stage>_version` is written only with its mark; a mark cleared keeps
    it (discovery's re-parse, a reset). A withdrawn document — vectors and
    entries deleted, its corpus error written — is not an earlier version."""
    from app import status
    con = db.connect(node)
    before = status.bounded(con, stored=None)["embed"].superseded
    con.execute("UPDATE documents SET error='no text any more' WHERE uid=(SELECT uid FROM documents "
                "WHERE in_scope=1 AND embed_done=0 AND embed_version<>0 AND chunks>0 LIMIT 1)")
    assert status.bounded(con, stored=None)["embed"].superseded == before - 1
    # a reset of the lexical stage (db.reset_stage's fts): both marks cleared,
    # both versions kept — what was indexed and embedded is still held
    con.execute("UPDATE documents SET fts_done=0, embed_done=0 WHERE embed_done=1 "
                "AND embed_version=? AND rowid=(SELECT MIN(rowid) FROM documents WHERE embed_done=1)",
                (config.EMBED_VERSION,))
    assert status.bounded(con, stored=None)["embed"].superseded == before


def test_the_bounded_counts_keep_their_measured_plans(node):
    """The covering index, no `error IS NULL` in the waiting count, and the
    superseded lookups seek the index on all four columns (CLAUDE.md: plan
    checks, no TEMP B-TREE, no LIST SUBQUERY)."""
    from app import status
    con = db.connect(node)
    for where in status.AT.values():
        plan = " ".join(r[3] for r in con.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM (SELECT 1 FROM {status.INDEXED} "
            f"WHERE {where} LIMIT 10)"))
        assert "COVERING INDEX idx_documents_scope" in plan, plan
    for k in ("fts", "embed"):
        plan = " ".join(r[3] for r in con.execute(
            f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM {status.INDEXED} "
            f"WHERE {status._SUPERSEDED_SEEK[k]} AND {status.SUPERSEDED[k]}",
            {"max": config.MAX_RETRIES}))
        assert "USING INDEX idx_documents_scope (in_scope=? AND rdf_done=?" in plan, plan
        assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan
    sql, params = web._document_counts_sql([])
    assert [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params)] == ["SCAN documents"]


def test_every_command_a_status_sentence_names_parses():
    """As node health's own test does for its text: a reason that names a
    command names one the CLI parses."""
    import shlex
    from pathlib import Path
    from app import louise, status
    text = Path(status.__file__).read_text(encoding="utf-8")
    text = re.sub(r'"\s*\n\s*f?"', "", text)
    named = set(re.findall(r"`louise ([a-z][^`]*)`", text))
    assert {"node resume", "node show"} <= {re.sub(r"\{[^}]*\}", "x", n).replace(" x", "") for n in named}
    parser = louise.build_parser()
    for cmd in named:
        argv = shlex.split(re.sub(r"\{[^}]*\}", "x", cmd))
        try:
            parser.parse_args(argv)
        except SystemExit as e:
            raise AssertionError(f"`louise {cmd}` does not parse") from e
