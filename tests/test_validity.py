"""One record per Expression: the version table, the index columns and the
search window (app/validity.py; docs/notes/normattiva-multivigenza-review.md
§3.2–§3.3; 05 §5.1 and 07 §7.3 as amended 2026-10-12). The version in force
at a date is chosen by key, never by similarity (invariant 4), and no two
versions of one Work ever meet in a leg."""
import zipfile

import pytest

from app import collections as collections_mod, db, discovery, embeddings, fts, retrieval, validity, worker
from app.plugins.normattiva import source as nsrc
from tests.test_normattiva import (BUILT, RD206_O, RD206_R, RD206_REPEALED, SARD, SARD_MEMBERS,
                                   _nid, _zip)

SARD_WORK = "http://www.normattiva.it/eli/id/1948/03/09/048C0003"
O_ID = _nid("1948-03-09", "048C0003", "1948-03-09", "ORIGINAL")
V11_ID = _nid("1948-03-09", "048C0003", "2001-02-01", "CONSOLIDATED/20010201")
V12_ID = _nid("1948-03-09", "048C0003", "2001-02-16", "CONSOLIDATED/20010216")
V15 = SARD + "VIGENZA_2014-01-01_V15.xml"
V15_ID = _nid("1948-03-09", "048C0003", "2014-01-01", "CONSOLIDATED/20140101")
RD_O_ID = _nid("1701-01-01", "6100206R", "1701-01-01", "ORIGINAL")
RD_R_ID = _nid("1701-01-01", "6100206R", "2025-05-09", "CONSOLIDATED/20250509")


@pytest.fixture
def versioned(tmp_path, isolated_config, monkeypatch):
    """A normattiva collection on a fake encoder: the Statuto Sardegna's
    three versions, and R.D. 206/1861 as its original (the repealed-acts
    zip) and its repeal state (Regi decreti's multivigente zip)."""
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/M/leggi-costituzionali.zip", dict(SARD_MEMBERS))
    _zip(root / "akn/M/regi-decreti.zip", {RD206_R: RD206_REPEALED})
    # the 1701 original with a 1701 entry into force: the template's 2020 note
    # would start its validity in 2020 (original_start)
    _zip(root / "akn/O/atti-normativi-abrogati-in-originale.zip",
         {RD206_O: BUILT.replace(b"25/01/2020", b"01/01/1701")})
    c = collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb",
        embed=collections_mod.EmbedRecipe(url="http://cpu-encoder:1", api="openai", model="m",
                                          dtype="f16", ctx=512, dim=8, concurrency=2))

    def fake(items, recipe=None, **kw):
        return [[1.0] + [0.0] * 7 for _ in items]
    monkeypatch.setattr(embeddings, "_embed_texts", fake)
    con = db.connect(c)
    scope = db.get_scope(con)
    scope["embed_mode"] = "all"
    db.set_scope(con, scope)
    discovery.sync_collection(con, c)
    return c, con, root


def _run(con, c, *stages):
    """Every pending document through the named stages, committing per step
    as `_execute_step` does."""
    flags = {"rdf": "rdf_done", "fts": "fts_done", "embed": "embed_done"}
    for stage in stages:
        while True:
            row = con.execute(f"SELECT * FROM documents WHERE {flags[stage]}=0 AND error IS NULL "
                              f"AND in_scope=1 LIMIT 1").fetchone()
            if row is None:
                break
            getattr(worker, f"stage_{stage}")(con, row, c)
            if con.in_transaction:
                con.execute("COMMIT")


def _table(con):
    return {int(r["native_id"]): dict(r) for r in con.execute("SELECT * FROM validity")}


# ---- the declaration and the window ---------------------------------------------

def test_the_plugin_declares_the_axis_and_other_collections_are_untouched(versioned):
    c, _, _ = versioned
    v = validity.declared(c)
    assert v == validity.Validity(from_key="extra.vigenza_from", work_key="extra.eli_work")
    assert validity.window(c, None) == {"shown": True}
    assert validity.window(c, 20010210) == {"day": 20010210}
    assert validity.lance_where({"shown": True}) == "shown = 1"
    assert validity.lance_where(validity.window(c, 20010210)) == \
        "valid_from <= 20010210 AND valid_to > 20010210"
    assert validity.day_int("2001-02-01") == 20010201 and validity.day_text(20010201) == "2001-02-01"
    assert validity.day_text(validity.OPEN) is None and validity.day_text(None) is None
    with pytest.raises(ValueError):
        validity.day_int("2001-2-1")
    other = collections_mod.Collection(
        name="gutenberg", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=c.corpus_dir, manifest_path=c.manifest_path, fts_dir=c.fts_dir, lance_dir=c.lance_dir)
    assert validity.declared(other) is None and validity.window(other, 20010210) is None
    assert validity.columns_for(None, other, [1]) == {}
    assert "valid_to" not in fts._schema("en").to_json() if hasattr(fts._schema("en"), "to_json") else True


# ---- the version table -----------------------------------------------------------

def test_the_rdf_stage_chains_each_work(versioned):
    """review §3.2: `valid_to` is the next version's start, open on the last;
    `shown` is the latest version with text; a repeal state (no text) closes
    its predecessor and is never shown."""
    c, con, _ = versioned
    _run(con, c, "rdf")
    t = _table(con)
    assert {t[i]["work"] for i in (O_ID, V11_ID, V12_ID)} == {SARD_WORK}
    assert [t[i]["valid_from"] for i in (O_ID, V11_ID, V12_ID)] == [19480310, 20010201, 20010216], "the original starts at its Expression date, the day after its publication (original_start)"
    assert [t[i]["valid_to"] for i in (O_ID, V11_ID, V12_ID)] == [20010201, 20010216, None]
    assert [t[i]["shown"] for i in (O_ID, V11_ID, V12_ID)] == [0, 0, 1]
    assert all(t[i]["has_text"] == 1 for i in (O_ID, V11_ID, V12_ID))
    assert t[O_ID]["in_force"] is None, "the origin does not say"
    # R.D. 206/1861: the original (repealed-acts zip) and the 2025 repeal state
    assert (t[RD_O_ID]["valid_from"], t[RD_O_ID]["valid_to"], t[RD_O_ID]["shown"]) == (17010101, 20250509, 1)
    assert (t[RD_R_ID]["valid_to"], t[RD_R_ID]["has_text"], t[RD_R_ID]["shown"]) == (None, 0, 0)
    assert t[RD_R_ID]["in_force"] == 0 and t[RD_O_ID]["in_force"] == 0
    cols = validity.index_columns(con, [O_ID, V12_ID, RD_O_ID, 42])
    assert cols[O_ID] == {"valid_from": 19480310, "valid_to": 20010201, "shown": 0}
    assert cols[V12_ID] == {"valid_from": 20010216, "valid_to": validity.OPEN, "shown": 1}
    assert cols[42] == {"valid_from": 15000101, "valid_to": validity.OPEN, "shown": 1}, "a record not yet recorded reads as current"
    desc = validity.describe(con, [V11_ID, V12_ID, RD_R_ID])
    assert desc[V11_ID] == {"from": "2001-02-01", "to": "2001-02-16", "in_force": None, "current": False}
    assert desc[V12_ID]["to"] is None and desc[V12_ID]["current"] is True
    assert desc[RD_R_ID]["in_force"] is False


def test_recording_is_idempotent_and_order_free(versioned):
    c, con, _ = versioned
    rows = con.execute("SELECT * FROM documents ORDER BY CAST(native_id AS INTEGER) DESC").fetchall()
    for row in rows:                       # newest first: the chain is recomputed on every arrival
        worker.stage_rdf(con, row, c)
        if con.in_transaction:
            con.execute("COMMIT")
    before = _table(con)
    for row in rows:
        con.execute("UPDATE documents SET rdf_done=0 WHERE uid=?", (row["uid"],))
    _run(con, c, "rdf")
    after = _table(con)
    for i in before:
        assert {k: before[i][k] for k in ("valid_from", "valid_to", "shown", "has_text")} == \
            {k: after[i][k] for k in ("valid_from", "valid_to", "shown", "has_text")}


def test_the_chain_query_is_indexed(versioned):
    """The one query the rdf stage adds per document walks the Work's
    versions by idx_validity_work: no scan, no temporary b-tree."""
    c, con, _ = versioned
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT uid, native_id, valid_from, valid_to, has_text, shown FROM validity "
        "WHERE work=? ORDER BY valid_from, native_id", (SARD_WORK,)))
    assert "idx_validity_work" in plan and "TEMP B-TREE" not in plan and "SCAN" not in plan
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT native_id, valid_to, shown FROM validity WHERE synced=0 "
        "ORDER BY native_id LIMIT 500"))
    assert "idx_validity_unsynced" in plan and "TEMP B-TREE" not in plan


# ---- the legs ----------------------------------------------------------------------

def test_the_text_leg_admits_one_version_per_work(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    def books(day):
        hits = fts.search("it", "Sardegna", limit=50, collection=c, validity=validity.window(c, day))
        return {int(h["book_id"]) for h in hits}
    assert books(None) == {V12_ID}, "the default view: the latest text, once"
    assert books(20010210) == {V11_ID}, "in force between the two 2001 versions"
    assert books(20010216) == {V12_ID}, "the boundary day belongs to the new version"
    assert books(20010215) == {V11_ID}
    assert books(19990101) == {O_ID}
    assert books(19480309) == set(), "published 1948-03-09, in force 1948-03-10: the vacatio returns nothing"
    assert books(19480310) == {O_ID}, "the original from its Expression date (original_start)"
    assert books(19470101) == set(), "before the act existed"
    unfiltered = {int(h["book_id"]) for h in fts.search("it", "Sardegna", limit=50, collection=c)}
    assert unfiltered == {O_ID, V11_ID, V12_ID}, "without a window every version is in the index"


def test_a_repealed_act_is_shown_in_its_last_text_and_excluded_after_its_repeal(versioned):
    """eli-legal-plugins.md §5.7 and the operator's rule in one: retrievable
    by default, labelled; absent from any date after the repeal."""
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    def books(day):
        return {int(h["book_id"]) for h in
                fts.search("it", "Fuori capo", limit=50, collection=c, validity=validity.window(c, day))}
    assert RD_O_ID in books(None) and RD_R_ID not in books(None)
    assert RD_O_ID in books(19000101) and RD_O_ID in books(20250508)
    assert RD_O_ID not in books(20250509) and RD_O_ID not in books(20260101)
    assert RD_R_ID not in books(20260101), "a repeal state has no chunk to return"
    assert validity.describe(con, [RD_O_ID])[RD_O_ID] == \
        {"from": "1701-01-01", "to": "2025-05-09", "in_force": False, "current": True}


def test_the_vector_leg_admits_one_version_per_work(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts", "embed")
    tbl = embeddings._conn(c).open_table(embeddings.TABLE)
    assert set(embeddings.VALIDITY_COLUMNS) <= set(tbl.schema.names)
    def books(day):
        return {int(h["book_id"]) for h in
                embeddings.search("Sardegna", k=200, collection=c, validity=validity.window(c, day))}
    assert books(None) == {V12_ID, RD_O_ID}, "every vector is equal under the fake encoder: the filter decides"
    assert books(20010210) == {V11_ID, RD_O_ID}
    assert books(19990101) == {O_ID, RD_O_ID}
    assert books(19480309) == {RD_O_ID}, "the original's day between publication and entry into force"
    assert books(19480310) == {O_ID, RD_O_ID}
    assert books(20260101) == {V12_ID}
    assert books(16000101) == set()


def test_a_new_version_moves_its_predecessor_through_its_own_stages(versioned):
    """review §3.4: a new version arrives; the version before it gets its end
    and loses `shown`, its index rows follow through its own fts and embed
    stages (every vector reused, the two columns updated in place)."""
    c, con, root = versioned
    _run(con, c, "rdf", "fts", "embed")
    nsrc._zip_cache.clear()
    with zipfile.ZipFile(root / "akn/M/leggi-costituzionali.zip", "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr(V15, BUILT)
    discovery.sync_collection(con, c)
    row = con.execute("SELECT * FROM documents WHERE native_id=?", (str(V15_ID),)).fetchone()
    assert row is not None and row["rdf_done"] == 0
    worker.stage_rdf(con, row, c)
    if con.in_transaction:
        con.execute("COMMIT")
    t = _table(con)
    assert (t[V12_ID]["valid_to"], t[V12_ID]["shown"], t[V12_ID]["synced"]) == (20140101, 0, 0)
    assert (t[V15_ID]["valid_to"], t[V15_ID]["shown"]) == (None, 1)
    moved = con.execute("SELECT fts_done, embed_done FROM documents WHERE native_id=?", (str(V12_ID),)).fetchone()
    assert (moved["fts_done"], moved["embed_done"]) == (0, 0), "its index rows are stale: its stages re-run"
    calls = []
    orig = embeddings._embed_texts
    def counting(items, recipe=None, **kw):
        calls.extend(items)
        return orig(items, recipe=recipe, **kw)
    embeddings._embed_texts = counting
    try:
        _run(con, c, "fts", "embed")
    finally:
        embeddings._embed_texts = orig
    assert len(calls) == 0, ("nothing reached the encoder: the moved version reused everything, and the new "
                             "version's text is R.D. 206's original, whose vectors it carries (06 §6.3, amended)")
    assert embeddings._conn(c).open_table(embeddings.TABLE).count_rows(f"book_id = {V15_ID}") == len(worker._load_chunks(row, c))
    t = _table(con)
    assert t[V12_ID]["synced"] == 1
    def text_books(day):
        return {int(h["book_id"]) for h in
                fts.search("it", "Sardegna", limit=50, collection=c, validity=validity.window(c, day))}
    assert text_books(None) == set(), "BUILT holds no 'Sardegna': the latest version is the one shown"
    assert text_books(20100101) == {V12_ID}
    def vec_books(day):
        return {int(h["book_id"]) for h in
                embeddings.search("x", k=200, collection=c, validity=validity.window(c, day))}
    assert vec_books(None) == {V15_ID, RD_O_ID}
    assert vec_books(20100101) == {V12_ID, RD_O_ID}
    rows = embeddings._conn(c).open_table(embeddings.TABLE).search().where(
        f"book_id = {V12_ID}", prefilter=True).limit(500).to_list()
    assert rows and all(r["valid_to"] == 20140101 and r["shown"] == 0 for r in rows), "updated in place"


def test_the_maintainers_drain_syncs_what_no_stage_rewrote(versioned):
    c, con, _ = versioned
    _run(con, c, "rdf", "fts", "embed")
    con.execute("UPDATE validity SET valid_to=20300101, shown=0, synced=0 WHERE native_id=?", (str(V12_ID),))
    assert validity.sync_vectors(con, c, lock_con=con) == 1
    assert _table(con)[V12_ID]["synced"] == 1
    rows = embeddings._conn(c).open_table(embeddings.TABLE).search().where(
        f"book_id = {V12_ID}", prefilter=True).limit(500).to_list()
    assert rows and all(r["valid_to"] == 20300101 and r["shown"] == 0 for r in rows)
    assert validity.sync_vectors(con, c, lock_con=con) == 0, "nothing left to drain"


def test_retrieval_applies_the_window_per_collection(versioned):
    """The search path (app/retrieval.py): `as_of` reaches the legs as a
    window on this collection and the catalog leg is admitted by the table;
    a collection without validity gets no window at all."""
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    got = retrieval.search(con, "Sardegna", k=10, mode="fts", translate=False, collection=c, as_of=20010210)
    assert {int(h["book_id"]) for h in got["hits"]} == {V11_ID}
    got = retrieval.search(con, "Sardegna", k=10, mode="fts", translate=False, collection=c)
    assert {int(h["book_id"]) for h in got["hits"]} == {V12_ID}
    got = retrieval.search(con, "Sardegna", k=10, mode="fts", translate=False, collection=c, as_of=19470101)
    assert got["hits"] == []


def test_a_later_version_carries_the_vectors_of_the_articles_it_shares(versioned, monkeypatch):
    """06 §6.3 (amended 2026-10-12, RULE 1): the same cleaned text in one
    space has one vector, so a version embeds only the articles it changed;
    every version still holds every vector."""
    c, con, _ = versioned
    _run(con, c, "rdf", "fts")
    calls: list = []

    def counting(items, recipe=None, **kw):
        calls.extend(items)
        return [[1.0] + [0.0] * 7 for _ in items]
    monkeypatch.setattr(embeddings, "_embed_texts", counting)
    rows = {int(r["native_id"]): r for r in con.execute("SELECT * FROM documents")}
    texts = {nid: {ch["text"] for ch in worker._load_chunks(rows[nid], c)} for nid in (O_ID, V11_ID, V12_ID)}
    assert 0 < len(texts[V11_ID] - texts[O_ID]) < len(texts[V11_ID]) // 2, "most articles are shared"

    def embed(nid):
        before = len(calls)
        worker.stage_embed(con, rows[nid], c)
        if con.in_transaction:
            con.execute("COMMIT")
        return len(calls) - before
    assert embed(O_ID) == len(texts[O_ID])
    assert embed(V11_ID) == len(texts[V11_ID] - texts[O_ID]), \
        "only the articles the 2001 version changed reached the encoder"
    assert embed(V12_ID) == len(texts[V12_ID] - texts[V11_ID] - texts[O_ID])
    tbl = embeddings._conn(c).open_table(embeddings.TABLE)
    for nid in (O_ID, V11_ID, V12_ID):
        assert tbl.count_rows(f"book_id = {nid}") == len(worker._load_chunks(rows[nid], c)), \
            "a carried vector is a row of the new version like any other"
    assert con.execute("SELECT COUNT(*) FROM chunk_hashes").fetchone()[0] >= len(texts[V12_ID])
    last = con.execute("SELECT message FROM events WHERE stage='embed' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "from other versions" in last


# ---- closing a chain from outside it (review §3.2, case 2) ----------------------

REPEALING_M = "LEGGE_20250110_9/2025-01-10_25G00009_ORIGINALE_V0.xml"
REPEALING_ID = _nid("2025-01-10", "25G00009", "2025-01-10", "ORIGINAL")
BUILT_1701 = BUILT.replace(b"25/01/2020", b"01/01/1701")
AKN_RD = "akn:it/act/decreto/stato/2020-01-02/3"        # the template's own identity, kept by RD206_O
#: a 2025 law stating, as the archive does: one whole-act repeal by AKN path
#: with the day it applies from, one repeal of an article, one insertion whose
#: text says "abrogato", and one whole-act repeal by URN without a date
_ANALYSIS = (b'<analysis source="#x"><activeModifications>'
             b'<textualMod type="repeal" eId="amod_0"><source href="#art1"/>'
             b'<destination href="/akn/it/act/decreto/stato/2020-01-02/3/ita@2025-05-09/!main"/></textualMod>'
             b'<textualMod type="repeal" eId="amod_1"><source href="#art1"/>'
             b'<destination href="urn:nir:stato:legge:1999-01-01;1#art5"/></textualMod>'
             b'<textualMod type="insertion" eId="amod_2"><source href="#art1"/>'
             b'<destination href="urn:nir:stato:legge:1999-01-01;2"/><new>PROVVEDIMENTO ABROGATO</new></textualMod>'
             b'<textualMod type="repeal" eId="amod_3"><source href="#art1"/>'
             b'<destination href="urn:nir:stato:legge:1999-01-01;7"/></textualMod>'
             b'</activeModifications></analysis>')
REPEALING = (BUILT.replace(b"2020-01-02;3", b"2025-01-10;9").replace(b"/2020-01-02/3", b"/2025-01-10/9")
             .replace(b"25/01/2020", b"25/01/2025").replace(b"</meta>", _ANALYSIS + b"</meta>"))


@pytest.fixture
def closing(tmp_path, isolated_config, monkeypatch):
    """R.D. 206/1861's original alone in the repealed-acts zip — no repeal
    state anywhere — and a 2025 law whose modifications repeal it in full
    from 2025-05-09."""
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/M/leggi.zip", {REPEALING_M: REPEALING})
    _zip(root / "akn/O/atti-normativi-abrogati-in-originale.zip", {RD206_O: BUILT_1701})
    c = collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def _rdf(con, c, native_id):
    row = con.execute("SELECT * FROM documents WHERE uid=?", (f"normattiva:{native_id}",)).fetchone()
    worker.stage_rdf(con, row, c)
    if con.in_transaction:
        con.execute("COMMIT")


@pytest.mark.parametrize("first", ["repealing", "repealed"])
def test_a_whole_act_repeal_stated_by_another_act_closes_the_chain(closing, first):
    """review §3.2 case 2, in either order of arrival: the repealed original
    ends the day the repealing act's modification applies from. A repeal of
    an article, an insertion that says "abrogato" and a repeal of an act not
    held close nothing."""
    c, con = closing
    for nid in ([REPEALING_ID, RD_O_ID] if first == "repealing" else [RD_O_ID, REPEALING_ID]):
        _rdf(con, c, nid)
    t = _table(con)
    assert (t[RD_O_ID]["valid_from"], t[RD_O_ID]["valid_to"], t[RD_O_ID]["shown"]) == (17010101, 20250509, 1)
    assert t[REPEALING_ID]["valid_to"] is None and t[REPEALING_ID]["valid_from"] == 20250125
    closures = {(r["key"], r["closed_at"]) for r in con.execute("SELECT key, closed_at FROM validity_closures")}
    assert closures == {(AKN_RD, 20250509), ("urn:nir:stato:legge:1999-01-01;7", 20250125)}, \
        "the dated AKN target; the undated URN one from the stating version's own start"
    assert validity.describe(con, [RD_O_ID])[RD_O_ID] == \
        {"from": "1701-01-01", "to": "2025-05-09", "in_force": False, "current": True}
    assert validity.admit(con, validity.window(c, 20250508), [RD_O_ID]) == {RD_O_ID}
    assert validity.admit(con, validity.window(c, 20250509), [RD_O_ID]) == set()
    assert validity.index_columns(con, [RD_O_ID])[RD_O_ID] == {"valid_from": 17010101, "valid_to": 20250509, "shown": 1}
    keys = {r["key"] for r in con.execute("SELECT key FROM validity_works WHERE work=?", (t[RD_O_ID]["work"],))}
    assert keys == {AKN_RD, "urn:nir:stato:decreto:2020-01-02;3", "eli:it:id/1701/01/01/6100206R"}, \
        "every canonical id of the Work finds its chain"


def test_a_closure_is_withdrawn_with_its_statement_and_recorded_once(closing):
    """Delete-then-insert on the stating uid (invariant 2): the same
    statement again moves nothing; a re-parse that no longer states the
    repeal reopens the act, and its index rows follow through its own
    stages."""
    c, con = closing
    _run(con, c, "rdf", "fts")
    before = _table(con)
    assert before[RD_O_ID]["valid_to"] == 20250509
    row = con.execute("SELECT * FROM validity WHERE native_id=?", (REPEALING_ID,)).fetchone()
    meta = {"from": row["valid_from"], "work": row["work"], "has_text": 1, "in_force": None, "keys": [],
            "closes": [(AKN_RD, 20250509), ("urn:nir:stato:legge:1999-01-01;7", 20250125)]}
    assert validity.record(con, row["uid"], REPEALING_ID, meta) == []
    assert _table(con) == before, "the same statement again moves nothing"
    assert con.execute("SELECT fts_done FROM documents WHERE uid=?", (f"normattiva:{RD_O_ID}",)).fetchone()[0] == 1
    moved = validity.record(con, row["uid"], REPEALING_ID, {**meta, "closes": []})
    assert moved == [f"normattiva:{RD_O_ID}"]
    t = _table(con)
    assert t[RD_O_ID]["valid_to"] is None and t[RD_O_ID]["synced"] == 0
    assert con.execute("SELECT COUNT(*) FROM validity_closures").fetchone()[0] == 0
    assert con.execute("SELECT fts_done, embed_done FROM documents WHERE uid=?",
                       (f"normattiva:{RD_O_ID}",)).fetchone()[:2] == (0, 0), "its index rows carry the old end"


def test_a_closure_before_the_last_version_is_contradicted_by_it(closing):
    """A version the origin published after the stated repeal is the better
    evidence: the chain stays open (and labelled by `in_force`)."""
    c, con = closing
    for nid in (RD_O_ID, REPEALING_ID):
        _rdf(con, c, nid)
    work = _table(con)[RD_O_ID]["work"]
    con.execute("UPDATE validity_closures SET closed_at=17000101 WHERE key=?", (AKN_RD,))
    assert validity.recompute(con, work) == [f"normattiva:{RD_O_ID}"]
    assert _table(con)[RD_O_ID]["valid_to"] is None


def test_the_closure_lookups_are_indexed(closing):
    """Every query the closures add is answered from an index: no scan, no
    temporary b-tree (CLAUDE.md's engineering rules)."""
    c, con = closing
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT MIN(c.closed_at) FROM validity_works k "
        "JOIN validity_closures c ON c.key = k.key WHERE k.work=?", ("w",)))
    assert "idx_validity_works_work" in plan and "SCAN" not in plan and "TEMP B-TREE" not in plan
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT work FROM validity_works WHERE key IN (?, ?)", ("a", "b")))
    assert "SCAN" not in plan and "TEMP B-TREE" not in plan
    plan = " ".join(r[-1] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT key FROM validity_closures WHERE by_uid=?", ("u",)))
    assert "idx_validity_closures_by" in plan and "SCAN" not in plan
