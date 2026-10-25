"""tools/measure/chunks_drift.py — records whose chunk count in the manifest
differs from the count on the record line (2026-10-25: three gutenberg parts
counted 6,975 / 7,814 / 7,066 here against the snapshot's 7,714 / 8,820 /
7,085, every record covered)."""
import sqlite3

from tools.measure import chunks_drift as d


def test_a_record_differs_when_its_manifest_count_does_or_its_row_is_missing():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, chunks INTEGER, fts_version INTEGER, "
                "entity_version INTEGER, updated_at INTEGER, in_scope INTEGER, error TEXT)")
    con.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                    [("g:1", 10, 7, 1, 100, 1, None), ("g:2", 3, 6, 1, 50, 1, None)])
    cut = {"g:1": 10, "g:2": 5, "g:3": 2}
    got = d.drift(con, [{"uid": "g:1"}, {"uid": "g:2"}, {"uid": "g:3"}], lambda rec: cut[rec["uid"]])
    assert (got["records"], got["record_chunks"], got["manifest_chunks"]) == (3, 17, 13)
    assert [x["uid"] for x in got["differs"]] == ["g:2", "g:3"]
    assert got["differs"][0] == {"uid": "g:2", "record_chunks": 5, "manifest_chunks": 3, "fts_version": 6,
                                 "entity_version": 1, "updated_at": 50, "in_scope": 1, "error": None}
    assert got["differs"][1]["missing"] is True and got["differs"][1]["manifest_chunks"] is None


def test_a_record_is_counted_by_cutting_its_sections_as_landing_does(isolated_config, corpus_dir):
    """A record line has no chunk count (München, 2026-10-25: the first form
    of the tool read 0 for all 79,658): the count is the record's sections
    through `layers.chunks_for_document`, the cut `hold.land.cut` makes."""
    from app import collections as collections_mod
    from app.hold import land
    c = collections_mod.default_collection()
    rec = {"uid": "gutenberg:10001", "native_id": 10001,
           "sections": [["Chapter I", "word " * 3000], ["Chapter II", "more " * 50]]}
    n = d.record_chunks(c, rec)
    assert n > 1 and n == len(land.cut(c, [rec]))
