"""The Library's pre-embedded row counts VECTORS HELD over the archive's
declared id range — not the import ledger.

Live on pubmed 2026-09-14 the row read SUM(genesis_imports.docs) =
35,337,233 over in_scope = 39,994,988. The ledger is pass bookkeeping and
was 80,654 short of the vectors actually held (35,417,887), and the
denominator mixed in 4,538,777 documents past PMID 36,999,999 that the
archive cannot hold at all. The row now reads 35,417,887 / 35,456,211,
names the 38,324 inside the range without a vector, and says the rest is
past the archive, where only the local encoder can embed it.
"""
import json
import threading
import time
from dataclasses import replace

import pytest

from app import collections as collections_mod, db, health, web, worker
from app.bundles import genesis

HI = 36_999_999                  # MedCPT's declared range ends here
HI_TEXT = "36.999.999"           # as the `num` filter prints it


@pytest.fixture
def pubmed(isolated_config):
    c = replace(collections_mod.default_collection(), genesis_archive="medcpt-pubmed")
    con = db.connect(c)
    for nid, scope, embed in [
        (1, 1, 1), (2, 1, 1), (HI, 1, 1),       # inside the range, holding vectors
        (3, 1, 0), (4, 1, 0),                    # inside the range, none yet
        (5, 0, 0),                               # inside the range, out of scope
        (HI + 1, 1, 0),                          # the first id past it
        (40_000_000, 1, 1),                      # past it: the local encoder's
        (40_000_001, 1, 0),
    ]:
        con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,"
                    "rdf_done,fts_done,embed_done) VALUES(?,?,?,?,?,1,1,?)",
                    (f"pubmed:{nid}", "pubmed", str(nid), f"/x/{nid}", scope, embed))
    # the ledger, deliberately low: nothing on the row may read it
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) "
                "VALUES('medcpt-pubmed','0','final',1,0)")
    web.invalidate_counts()
    yield c
    web.invalidate_counts()


def _render(shelf):
    return web.TEMPLATES.env.get_template("library.html").render(
        request=None, active="library", shelves=[shelf], counts_age=0,
        plugins=[], plugin_profiles={}, plugin_extras={}, corpus_mounts=[],
        corpus_mounts_dir="", worker_state="running")


def _value(html):
    """The pre-embedded row's value, e.g. `3 / 5`."""
    return html.split(">pre-embedded (dense)</span>", 1)[1].split("</div>", 1)[0]


def test_the_scan_counts_vectors_held_inside_the_declared_range(pubmed):
    counts = web._document_counts_query(pubmed)
    assert (counts["arch_in_range"], counts["arch_in_range_embedded"],
            counts["arch_in_range_waiting"], counts["arch_past_range"],
            counts["arch_range_hi"]) == (5, 3, 2, 3, HI)
    g = web._genesis_state(db.connect(pubmed), pubmed, counts)
    assert g["docs"] == 1, "the ledger stays — the proof logic reads it"
    assert (g["in_range"], g["in_range_embedded"], g["in_range_missing"],
            g["in_range_waiting"], g["in_range_indexing"]) == (5, 3, 2, 2, 0)
    assert (g["past_range"], g["outside_range"], g["outside_embedded"],
            g["range_hi"], g["spans"]) == (3, 3, 1, HI, [(0, HI)])


@pytest.mark.parametrize("mode, tail", [("off", ", and embedding is off"),
                                        ("all", " (1 done)")])
def test_the_card_reads_held_vectors_not_the_ledger(pubmed, mode, tail):
    html = _render({**web._collection_shelf(pubmed, {}), "embed_mode": mode})
    value = _value(html)
    assert "3 / 5" in value and "1 / 8" not in value, value
    assert "2 inside the range still without a vector — " in html
    assert f"louise health {pubmed.name}</code> says whether the archive is still " \
           f"serving them" in html
    assert "declared id range (0 – 36.999.999)" in html
    assert (f"3 past id {HI_TEXT}: newer than the archive; only the local "
            f"encoder can embed them{tail}") in html


def test_the_range_sums_ride_the_one_scan(pubmed):
    """No index answers them, on purpose: the plan stays the sequential
    pass the counts already pay, with or without the browse index."""
    con = db.connect(pubmed)
    sql, params = web._document_counts_sql(web._archive_spans(pubmed))
    assert "arch_in_range_embedded" in sql and "INDEXED BY" not in sql
    before = dict(con.execute(sql, params).fetchone())
    for build_index in (False, True):
        if build_index:
            db.ensure_browse_index(con)
        plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql, params)]
        assert plan == ["SCAN documents"], plan
        assert not any("TEMP B-TREE" in p or "LIST SUBQUERY" in p for p in plan)
    assert dict(con.execute(sql, params).fetchone()) == before


def test_no_declared_range_leaves_the_scan_and_the_row_as_they_were(isolated_config,
                                                                    monkeypatch):
    base_sql, _ = web._document_counts_sql([])
    assert "arch_" not in base_sql
    plain = collections_mod.default_collection()
    monkeypatch.setitem(genesis.ARCHIVES, "undeclared", genesis.GenesisArchive(
        archive_id="undeclared", base_url="http://x", adapter="medcpt-npy",
        shards=("0", "1"), uid_prefix="pubmed:"))
    remote = replace(plain, genesis_archive="undeclared")
    local = replace(plain, genesis_archive="upstash-wikipedia-2024-06")
    # the local archive declares its language blocks (wikipedia's layer 1)
    assert web._archive_spans(local) == [(0, 11 * 10 ** 10 - 1)]
    for c in (plain, remote):
        assert web._archive_spans(c) == []
        assert web._document_counts_sql(web._archive_spans(c))[0] == base_sql
        assert not any(k.startswith("arch_") for k in web._document_counts_query(c))
    # a remote archive that declares no range keeps the ledger's row
    con = db.connect(remote)
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                "fts_done,embed_done) VALUES('pubmed:1','pubmed','1','/x',1,1,1,1)")
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) "
                "VALUES('undeclared','0','applied',7,0)")
    web.invalidate_counts()
    shelf = web._collection_shelf(remote, {})
    assert shelf["genesis"]["in_range"] is None and shelf["genesis"]["range_hi"] is None
    assert "7 / 1" in _value(_render(shelf))
    web.invalidate_counts()


def test_the_range_counts_survive_a_restart_as_ints(pubmed):
    assert web._persist_counts(pubmed, web._document_counts_query(pubmed))
    stored, _ = web._stored_counts(pubmed)
    assert (stored["arch_in_range"], stored["arch_in_range_embedded"],
            stored["arch_range_hi"]) == (5, 3, HI)
    assert all(type(v) is int for v in stored.values())


def test_counts_without_the_range_say_counting_never_the_ledger(pubmed):
    """The first cold process after the deploy reads a stored row written
    before these keys existed; a row counted over another range is the
    same case. Either way: counting — not the ledger's 1, not a zero."""
    con = db.connect(pubmed)
    shelf = web._collection_shelf(pubmed, {})
    fresh = web._document_counts_query(pubmed)
    old = {k: v for k, v in fresh.items() if not k.startswith("arch_")}
    moved = {**fresh, "arch_range_hi": 9_999_999}
    for counts in (old, moved):
        g = web._genesis_state(con, pubmed, counts)
        assert g["in_range"] is None and g["range_hi"] == HI
        value = _value(_render({**shelf, "genesis": g}))
        assert "counting…" in value and " / " not in value, value


def test_a_cached_count_over_another_range_refreshes_now(pubmed):
    """A TOML edit that declares (or moves) the range must not leave the
    row on "counting…" for a whole TTL — ~9 minutes on pubmed."""
    old = {k: v for k, v in web._document_counts_query(pubmed).items()
           if not k.startswith("arch_")}
    with web._counts_lock:
        web._counts_cache[web._counts_key(pubmed)] = (time.time(), old)
    web._document_counts(pubmed)                  # fresh by age: serves, and refreshes
    deadline = time.time() + 5
    while time.time() < deadline:
        with web._counts_lock:
            counts = web._counts_cache[web._counts_key(pubmed)][1]
        if counts.get("arch_range_hi") == HI:
            break
        time.sleep(0.05)
    assert counts.get("arch_range_hi") == HI and counts["arch_in_range"] == 5


def test_the_gap_on_the_card_is_the_gap_health_reports(pubmed):
    """Every unembedded in-range document here is past fts, which is when
    the two must agree; the card points to `louise health` for why."""
    con = db.connect(pubmed)
    g = web._genesis_state(con, pubmed, web._document_counts_query(pubmed))
    assert g["in_range_missing"] == health.archive_gap(con, pubmed).waiting == 2


def _add(con, nid, *, fts=1, embed=0):
    con.execute("INSERT INTO documents(uid,source,native_id,path,in_scope,rdf_done,"
                "fts_done,embed_done) VALUES(?,?,?,?,1,1,?,?)",
                (f"pubmed:{nid}", "pubmed", str(nid), f"/x/{nid}", fts, embed))
    web.invalidate_counts()


def test_a_gap_still_being_indexed_is_named_apart_from_health_s(pubmed):
    """health counts only documents past fts; the card's gap also holds
    documents still being indexed. It says which part health explains."""
    con = db.connect(pubmed)
    _add(con, 6, fts=0)
    g = web._genesis_state(con, pubmed, web._document_counts_query(pubmed))
    assert (g["in_range_missing"], g["in_range_indexing"], g["in_range_waiting"]) == (3, 1, 2)
    assert g["in_range_waiting"] == health.archive_gap(con, pubmed).waiting
    html = _render({**web._collection_shelf(pubmed, {}), "embed_mode": "off"})
    assert (f"3 inside the range still without a vector (1 still being indexed) — "
            f"<code>louise health {pubmed.name}</code> says whether the archive is still "
            f"serving the other 2") in html


def test_an_id_that_is_not_a_number_is_outside_every_range(pubmed):
    """SQLite casts it to 0 — inside a range from 0. It belongs to none
    (genesis.reopen_finals_for_range): not in the row, not in health's gap,
    and not "past" the range either."""
    con = db.connect(pubmed)
    assert tuple(con.execute("SELECT CAST('pubmed25n0001.xml.gz' AS INTEGER), "
                             "CAST('12abc' AS INTEGER)").fetchone()) == (0, 12)
    _add(con, "pubmed25n0001.xml.gz")
    _add(con, "12abc")
    counts = web._document_counts_query(pubmed)
    assert (counts["arch_in_range"], counts["arch_in_range_waiting"],
            counts["arch_past_range"]) == (5, 2, 3)
    assert health.archive_gap(con, pubmed).waiting == 2
    html = _render({**web._collection_shelf(pubmed, {}), "embed_mode": "off"})
    assert "3 / 5" in _value(html)
    assert (f"5 outside the declared range (3 past id {HI_TEXT}): the archive has no "
            f"vectors for them; only the local encoder can embed them, and embedding "
            f"is off") in html


def test_past_the_range_means_above_its_end(isolated_config, monkeypatch):
    """Declared files with a gap between them: an id in the gap is outside
    the range, not past it — and the title names both spans."""
    monkeypatch.setitem(genesis.ARCHIVES, "gapped", genesis.GenesisArchive(
        archive_id="gapped", base_url="http://x", adapter="medcpt-npy",
        shards=("0", "2"), uid_prefix="pubmed:", ids_per_shard=10))
    c = replace(collections_mod.default_collection(), genesis_archive="gapped")
    con = db.connect(c)
    for nid, embed in ((1, 1), (25, 0), (15, 1), (35, 0)):
        _add(con, nid, embed=embed)
    g = web._genesis_state(con, c, web._document_counts_query(c))
    assert g["spans"] == [(0, 9), (20, 29)]
    assert (g["in_range"], g["past_range"], g["outside_range"], g["outside_embedded"]) \
        == (2, 1, 2, 1)
    html = _render({**web._collection_shelf(c, {}), "embed_mode": "all"})
    assert "declared id ranges (0 – 9, 20 – 29)" in html
    assert ("2 outside the declared range (1 past id 29): the archive has no vectors "
            "for them; only the local encoder can embed them (1 done)") in html
    web.invalidate_counts()


def test_past_the_range_says_what_this_archive_means_by_it(isolated_config, monkeypatch):
    """"Newer than the archive" is pubmed's meaning: PMIDs only grow. Past
    wikipedia's language blocks lie the pages read from the dumps, and the
    card called 38 M of them newer (2026-09-15). Display only: the note is
    never part of the credit hashed into a collection's id."""
    assert genesis.MEDCPT_PUBMED.past_range == "newer than the archive"
    assert "newer" not in genesis.UPSTASH_WIKIPEDIA.past_range
    for archive in genesis.ARCHIVES.values():
        assert archive.past_range not in json.dumps(archive.credit())
    monkeypatch.setitem(genesis.ARCHIVES, "blocks", genesis.GenesisArchive(
        archive_id="blocks", base_url="http://x", adapter="medcpt-npy",
        shards=("0", "1"), uid_prefix="pubmed:", ids_per_shard=10,
        past_range=genesis.UPSTASH_WIKIPEDIA.past_range))
    c = replace(collections_mod.default_collection(), genesis_archive="blocks")
    con = db.connect(c)
    for nid, embed in ((1, 1), (12, 0), (1000, 0), (1001, 0)):
        _add(con, nid, embed=embed)
    web.invalidate_counts()
    html = _render({**web._collection_shelf(c, {}), "embed_mode": "off"})
    web.invalidate_counts()
    assert ("2 past id 19: pages read from the dumps, which the archive does not hold; "
            "only the local encoder can embed them, and embedding is off") in html


def test_the_first_cold_render_after_the_deploy_says_counting(pubmed, monkeypatch):
    """The real path: a stored counts row written before the range SUMs
    existed, an empty cache, and a scan that cannot land inside the cold
    wait. The stored row answers — and the pre-embedded row says counting,
    never the ledger's number."""
    fresh = web._document_counts_query(pubmed)
    assert web._persist_counts(pubmed, {k: v for k, v in fresh.items()
                                        if not k.startswith("arch_")})
    web.invalidate_counts()
    release = threading.Event()
    real = web._document_counts_query

    def slow(c):
        release.wait(10)
        return real(c)
    monkeypatch.setattr(web, "_document_counts_query", slow)
    monkeypatch.setattr(web, "_COUNTS_COLD_WAIT_S", 0.1)
    key = web._counts_key(pubmed)
    with web._counts_lock:
        web._counts_refreshing.pop(key, None)
    try:
        shelf = web._collection_shelf(pubmed, {})
        assert not shelf["counts_pending"] and shelf["in_scope"] == 8, \
            "the stored row answered the cold render"
        value = _value(_render(shelf))
        assert "counting…" in value and " / " not in value, value
    finally:
        release.set()
        deadline = time.time() + 10
        while time.time() < deadline:
            with web._counts_lock:
                if key not in web._counts_refreshing:
                    break
            time.sleep(0.05)


def test_the_card_the_worker_and_health_share_one_range(pubmed):
    con = db.connect(pubmed)
    con.execute("DELETE FROM genesis_imports")
    assert genesis.declared_spans(genesis.MEDCPT_PUBMED) \
        == worker._covered_ranges(con, pubmed) == [(0, HI)]
    con.execute("INSERT INTO genesis_imports(archive,shard,state,docs,updated_at) "
                "VALUES('medcpt-pubmed','3','final',5,0)")
    assert worker._covered_ranges(con, pubmed) \
        == genesis.declared_spans(genesis.MEDCPT_PUBMED, exclude={"3"}) \
        == [(0, 2_999_999), (4_000_000, HI)]
    assert genesis.declared_spans(genesis.UPSTASH_WIKIPEDIA) == [(0, 11 * 10 ** 10 - 1)]
    sql, params = genesis.in_spans_sql([(0, 9), (20, 29)])
    assert sql == ("(CAST(native_id AS INTEGER) BETWEEN ? AND ? OR "
                   "CAST(native_id AS INTEGER) BETWEEN ? AND ?)")
    assert params == [0, 9, 20, 29]
    named_sql, named = genesis.in_spans_sql([(0, 9)], named=True)
    assert named_sql == "(CAST(native_id AS INTEGER) BETWEEN :span_lo0 AND :span_hi0)"
    assert named == {"span_lo0": 0, "span_hi0": 9}
