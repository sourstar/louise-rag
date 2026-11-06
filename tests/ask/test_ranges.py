"""tests/ask/test_ranges.py — the `range` facet filter (05 §5.2, §5.7; 07
§7.3, amended 2026-10-25): `FIELD=FROM..TO`, either end open, negated with
`!`, read in the order its type demands (dates as spans, numbers as
numbers, never text); the values read out of the record parts held, part by
part on their roots; the resolving query on a covering index, its plan
checked and its reads bounded; a malformed range, or a range on a facet that
is not one, refused (exit 2, HTTP 409) naming what is accepted; and a
search on a fixture collection restricted by it end to end."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import api, db, fts, louise, query_service
from app import collections as collections_mod
from app.ask import facets, filters, ranges
from app.hold import collection as col
from app.hold import shard as shard_mod
from app.hold import snapshot as sn
from app.hold.codex import Facet
from app.pack import records as pack_records
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import pubmed

# ---- parsing --------------------------------------------------------------------------------

def test_a_date_is_the_span_its_precision_names():
    assert ranges.date_span("1999") == (19990101, 19991231)
    assert ranges.date_span("2024-02") == (20240201, 20240229), "the calendar's own month: a leap year"
    assert ranges.date_span("2023-02") == (20230201, 20230228)
    assert ranges.date_span("2003-07-15") == (20030715, 20030715)
    for bad in ("2023-02-30", "2023-13", "99", "1999-6", "c. 1850", "", "1999-06-01T00:00"):
        assert ranges.date_span(bad) is None, bad


def test_a_value_s_type_dates_first_then_numbers_never_text():
    assert ranges.type_of("date", "1999") == "date" and ranges.type_of("date", 1999) is None, \
        "09 §9.5's `date` is a string in one of three forms"
    assert ranges.type_of("extra.pages", 9) == "number" and ranges.type_of("extra.pages", "12.5") == "number"
    assert ranges.type_of("extra.year", "1999") == "date", "a four-digit string is a year"
    assert ranges.type_of("extra.x", True) is None and ranges.type_of("extra.x", "n/a") is None
    assert ranges.number("1e3") == 1000.0 and ranges.number(float("nan")) is None


def test_a_range_is_read_from_and_to_both_included_either_open():
    assert ranges.parse("2000..2005", "date") == ranges.Range(20000101, 20051231, "2000..2005"), \
        "FROM's first day to TO's last"
    assert ranges.parse("2000..", "date") == ranges.Range(20000101, None, "2000..")
    assert ranges.parse("..1999-06", "date") == ranges.Range(None, 19990630, "..1999-06")
    assert ranges.parse(" 2003-07 .. 2003-07 ", "date").lo == 20030701
    assert ranges.parse("-5..2.5", "number") == ranges.Range(-5, 2.5, "-5..2.5")
    assert ranges.parse("9..10", "number") == ranges.Range(9, 10, "9..10")
    for bad, why in (("2000", "not FROM..TO"), ("..", "neither end"), ("2005..2000", "after"),
                     ("2000..2005..2010", "not FROM..TO"), ("abc..2000", "not a date"),
                     ("2000..2023-02-30", "not a date"), ("2000...2005", "not a date")):
        with pytest.raises(ranges.RangeError, match=why):
            ranges.parse(bad, "date")
    assert ranges.looks_like_range("2000..2005") and ranges.looks_like_range("..1999")
    assert not ranges.looks_like_range("Wait.. what") and not ranges.looks_like_range("Neoplasms")


# ---- a fixture collection: records in two parts, an index, a manifest -----------------------

DATES = {1: "1998", 2: "2000-03-15", 3: "2003-07", 4: "2005-12-31", 5: "2010", 6: None, 7: "c. 1850",
         8: "2001-01-01"}
PAGES = {1: 9, 2: 10, 3: 100, 4: "12.5", 5: None, 6: 2, 7: None, 8: None}


def a_codex(*extra):
    return pubmed(facets=tuple(sorted((Facet("date", "range"), Facet("lang", "flat"), *extra),
                                      key=lambda f: f.field)))


def record(nid: int) -> dict:
    return {"uid": f"pubmed:{nid}", "native_id": str(nid), "lang": "en", "title": f"Whale {nid}",
            "creators": [], "date": DATES[nid], "subjects": [], "keys": [], "sections": [["", "the white whale"]],
            "extra": {"pages": PAGES[nid]}, "origin": f"x#{nid}", "content_hash": "0" * 32,
            "origin_hash": "1" * 32}


def write_part(shard_dir: Path, slice_: str, n: int, nids) -> sn.Shard:
    rel = shard_mod.shard_path(slice_, n)
    info = pack_records.write_shard(shard_dir / rel, [record(i) for i in nids])
    data = (shard_dir / rel).read_bytes()
    return sn.Shard(slice=slice_, n=n, root=shard_mod.pieces_root(data), records_root="sha256:" + "11" * 32,
                    first_id=str(min(nids)), last_id=str(max(nids)), records=info.records, chunks=1,
                    chunks_root="sha256:" + "22" * 32, tokens=512, bytes=len(data))


def a_snapshot(codex, shards, sid="2026-08") -> sn.Snapshot:
    return sn.Snapshot(collection_id=codex.collection_id, snapshot_id=sid, previous=None, shards=tuple(shards),
                       delta=sn.Delta((), (), ()), files={})


@pytest.fixture
def held(node, monkeypatch):  # noqa: F811
    """A held collection `pm`: its record parts and snapshot in `shard/`, a
    manifest and a text index over every record, the facet pass run."""
    codex = a_codex(Facet("extra.pages", "range"))
    name = "pm"
    d = col.shard_dir_of(node, name)
    shards = [write_part(d, "year=1998-2003", 1, [1, 2, 3, 6, 7]), write_part(d, "year=2004-2010", 1, [4, 5, 8])]
    snap = a_snapshot(codex, shards)
    (d / "snapshots").mkdir(parents=True)
    (d / "snapshots" / "2026-08.json").write_bytes(snap.canonical())
    c = collections_mod.Collection(
        name=name, plugin="pubmed", profile=None, embed_mode_default=None, corpus_dir=node / name / "corpus",
        manifest_path=node / name / "state" / "manifest.sqlite", fts_dir=node / name / "fts",
        lance_dir=node / name / "lancedb", codex=codex)
    con = db.connect(c)
    for nid in DATES:
        con.execute("INSERT INTO documents(uid,source,native_id,path,lang,rdf_done,fts_done,updated_at) "
                    "VALUES(?,?,?,?,?,1,1,0)", (f"pubmed:{nid}", "pubmed", str(nid), f"/x/{nid}", "en"))
        fts.index_book("en", nid, [{"id": f"pubmed:{nid}:0:1", "book_id": nid, "seq": 0, "chapter": "",
                                    "text": "the white whale", "tokens": 4}], c)
    con.commit()
    assert facets.build(con, codex, node / name / "index", snapshot_id="2026-08", shard_dir=d,
                        snapshot=snap) == ["date", "extra.pages", "lang"]
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [c])
    monkeypatch.setattr(query_service, "withheld_from", lambda names, **kw: [])
    yield c, codex, con, d, snap
    con.close()
    fts.close_all()


def found(filters_):
    out = query_service.run_search(["pm"], "whale", 20, "fts", False, filters=filters_)
    return {int(h["book_id"]) for h in out["hits"]}


def test_a_search_is_restricted_to_a_range_open_at_either_end_and_negated(held):
    assert found(None) == set(DATES), "no filter: every record"
    assert found({"date": "2000..2005"}) == {2, 3, 4, 8}
    assert found({"date": "..1999"}) == {1}, "open below"
    assert found({"date": "2005-12.."}) == {4, 5}, "open above"
    assert found({"date": {"not": "2000..2005"}}) == {1, 5, 6, 7}, \
        "negated: every other record, those without a date or with one that is not a date included"
    assert found({"date": "1999-06.."}) == {2, 3, 4, 5, 8}, "a record dated `1998` is not after June 1999"
    assert found({"date": "2003..2003-06"}) == set(), "`2003-07` is not wholly before July: contained, not overlapping"
    assert found({"date": "2003-07..2003-07"}) == {3}
    assert found({"date": "2003-07-01..2003-07-15"}) == set(), "a month is not inside a fortnight of it"
    assert found({"date": "1990..2000", "lang": "en"}) == {1, 2}, "a range intersects with another facet"


def test_numbers_are_compared_as_numbers_never_as_text(held):
    assert found({"extra.pages": "9..10"}) == {1, 2}, "as text, '100' sorts between '10' and '9'"
    assert found({"extra.pages": "10.."}) == {2, 3, 4}
    assert found({"extra.pages": "..9.5"}) == {1, 6}
    _c, codex, con, d, snap = held
    cache = facets.cached(d.parent / "index", "extra.pages", snapshot_id="2026-08")
    assert cache["type"] == "number" and cache["bounds"] == ["2", "100"]
    assert cache["postings"] == {"built": True, "rows": 5, "parts": 2, "unparsed": 0}
    dated = facets.cached(d.parent / "index", "date", snapshot_id="2026-08")
    assert dated["type"] == "date" and dated["bounds"] == ["1998", "2010"]
    assert dated["postings"]["unparsed"] == 1, "`c. 1850` is said, not guessed into a year"
    listed = {f["field"]: f for f in facets.fields(con, codex, d.parent / "index", snapshot_id="2026-08")["fields"]}
    assert listed["date"]["values"] == 2 and listed["date"]["filterable"] is True, "listed as its bounds"
    fresh = d.parent / "fresh-index"
    got = facets.values(con, codex, fresh, "date", snapshot_id="2026-08", shard_dir=d, snapshot=snap)
    assert got["bounds"] == ["1998", "2010"] and "this peer's" in got["note"], "read from the parts on request"


def test_a_malformed_range_or_a_range_on_another_kind_is_refused_naming_what_is_accepted(held):
    for bad in ("2000", "2005..2000", "..", "2000..soon"):
        with pytest.raises(filters.FilterError, match=r"FROM\.\.TO") as e:
            found({"date": bad})
        assert e.value.exit_code == 2
    with pytest.raises(filters.FilterError, match="'lang' is a flat facet.*not a range.*are date, extra.pages"):
        found({"lang": "2000..2005"})
    with pytest.raises(filters.FilterError, match="'lang' is a flat facet"):
        found({"lang": {"not": "..2005"}})


def test_a_range_too_broad_for_the_legs_is_refused_not_cut(held, monkeypatch):
    monkeypatch.setattr(filters, "MAX_IDS", 2)
    with pytest.raises(filters.FilterError, match="'date' in 2000..2005 admits records, more than the 2"):
        found({"date": "2000..2005"})
    with pytest.raises(filters.FilterError, match="excluding 'date' in 2000..2005 needs its records"):
        found({"date": {"not": "2000..2005"}})


def test_the_cli_and_the_http_body_take_the_same_range(held, capsys):
    assert louise.main(["ask", "search", "whale", "--filter", "date=!2000..2005"]) == 0
    text = capsys.readouterr().out
    out = json.loads(text[text.index("{\n"):])     # after the legs' log lines
    assert out["filters"] == {"date": {"not": "2000..2005"}}
    assert {h["uid"] for lst in out["lists"] for h in lst["hits"]} == {f"pubmed:{i}" for i in (1, 5, 6, 7)}
    assert louise.main(["ask", "search", "whale", "--filter", "date=2005..2000"]) == 2
    assert "FROM..TO" in capsys.readouterr().err
    client = TestClient(api.app)
    r = client.post("/api/ask/search", json={"query": "whale", "filters": {"date": "..1999"}})
    assert r.status_code == 200 and {h["uid"] for lst in r.json()["lists"] for h in lst["hits"]} == {"pubmed:1"}
    r = client.post("/api/ask/search", json={"query": "whale", "filters": {"lang": "1990..2000"}})
    assert r.status_code == 409 and r.json()["exit"] == 2 and "not a range" in r.json()["error"]


# ---- the pass over the parts ------------------------------------------------------------------

def test_the_pass_reads_a_part_once_per_root_replaces_a_reissued_one_and_drops_a_gone_one(held, monkeypatch):
    _c, codex, con, d, snap = held
    idx = d.parent / "index"
    reads = []
    real = ranges._read_part
    monkeypatch.setattr(ranges, "_read_part", lambda path, field: reads.append(path.parent.name) or real(path, field))
    facets.build(con, codex, idx, snapshot_id="2026-08", shard_dir=d, snapshot=snap)
    assert reads == [], "every part already read at its root"
    # 2026-09: the second part re-issued without record 5, the first gone
    again = write_part(d, "year=2004-2010", 1, [4, 8])
    snap2 = a_snapshot(codex, [again], "2026-09")
    facets.build(con, codex, idx, snapshot_id="2026-09", shard_dir=d, snapshot=snap2, fields=["date"])
    assert reads == ["year=2004-2010"]
    pcon = filters.connect(idx)
    assert {r[0] for r in pcon.execute("SELECT native_id FROM range_postings WHERE field='date'")} == {4, 8}
    assert filters.built_for(pcon, "date") == "2026-09"
    pcon.close()
    assert filters.book_ids_for(codex, idx, {"date": "2000.."}, snapshot_id="2026-09") == {4, 8}
    with pytest.raises(filters.FilterError, match="not indexed"):
        filters.book_ids_for(codex, idx, {"date": "2000.."}, snapshot_id="2026-08")


def test_a_field_of_dates_and_numbers_has_no_one_order_and_is_not_filterable(tmp_path):
    codex = a_codex(Facet("extra.pages", "range"))
    d = tmp_path / "shard"
    old = dict(PAGES)
    PAGES[1] = "1999-01"            # a date among numbers
    try:
        snap = a_snapshot(codex, [write_part(d, "year=1998-2003", 1, [1, 2, 3])])
    finally:
        PAGES.update(old)
    idx = tmp_path / "index"
    facets.build(None, codex, idx, snapshot_id="2026-08", shard_dir=d, snapshot=snap, fields=["extra.pages"])
    cache = facets.cached(idx, "extra.pages", snapshot_id="2026-08")
    assert cache["postings"]["built"] is False and "dates in some records and numbers" in cache["postings"]["why"]
    with pytest.raises(filters.FilterError, match="not indexed.*dates in some records"):
        filters.book_ids_for(codex, idx, {"extra.pages": "1..10"}, snapshot_id="2026-08")


def test_a_parquet_part_gives_the_one_column(tmp_path):
    p = tmp_path / "records" / "s" / "00001.parquet"
    pack_records.write_shard(p, [record(i) for i in (1, 2, 3)])
    assert list(pack_records.iter_fields(p, ("native_id", "date", "extra"))) == [
        {"native_id": "1", "date": "1998", "extra": {"pages": 9}},
        {"native_id": "2", "date": "2000-03-15", "extra": {"pages": 10}},
        {"native_id": "3", "date": "2003-07", "extra": {"pages": 100}}]
    got = ranges._read_part(p, "date")
    assert sorted(got["rows"]) == [(19980101, 19981231, 1), (20000315, 20000315, 2), (20030701, 20030731, 3)]


# ---- the resolving query: its plan, and what it reads ---------------------------------------

def _postings(tmp_path, n=20_000, analyze=False):
    pcon = filters.connect(tmp_path / "index")
    pcon.execute("BEGIN")
    pcon.executemany("INSERT INTO range_postings(field, lo, hi, native_id, part) VALUES(?,?,?,?,1)",
                     [("date", 19000101 + i, 19000101 + i, i) for i in range(n)]
                     + [("other", i, i, i) for i in range(n)])
    pcon.execute("COMMIT")
    if analyze:
        pcon.execute("ANALYZE")
    return pcon


@pytest.mark.parametrize("analyze", [False, True], ids=["as-built", "analyzed"])
@pytest.mark.parametrize("text", ["1900..1920", "1910..", "..1910"])
def test_the_resolving_query_searches_a_covering_index_never_scans_or_sorts(tmp_path, text, analyze):
    pcon = _postings(tmp_path, analyze=analyze)
    sql, args = ranges.query("date", ranges.parse(text, "date"))
    plan = " | ".join(str(r[-1]).upper() for r in pcon.execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "SEARCH RANGE_POSTINGS USING COVERING INDEX IDX_RANGE_LO (FIELD=? AND LO" in plan, plan
    assert "SCAN" not in plan and "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan
    pcon.close()


def test_a_broad_range_stops_after_one_record_past_the_bound(tmp_path):
    pcon = _postings(tmp_path)
    fetched = []

    class Counting:
        def execute(self, sql, args):
            for row in pcon.execute(sql, args):
                fetched.append(row)
                yield row
    with pytest.raises(ranges.TooBroad):
        ranges.ids(Counting(), "date", ranges.parse("1900..", "date"), limit=100)
    assert len(fetched) == 101, "the cost is the bound, not the field's 20,000 rows"
    assert ranges.ids(pcon, "date", ranges.parse("1900-01-01..1900-01-05", "date"), limit=100) == {0, 1, 2, 3, 4}
    pcon.close()
