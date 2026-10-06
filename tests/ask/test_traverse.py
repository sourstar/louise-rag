"""tests/ask/test_traverse.py — `louise_traverse` (05 §5.4, §5.5; 09 §9.14):
edge shards written in §9.5's framing, indexed once per root into the
manifest, walked both directions `depth` hops with one relation type when
asked; an edge into a record not held here or a canonical id unresolved
and saying what it needs."""
import sqlite3

from app.ask import traverse
from app.hold import records as rec, snapshot as sn
from app.hold.drag import pieces_root_of
from app.pack import records as pack_records
from tests.hold.conftest import pubmed


def edges_shard(path, edges, snapshot_id="2026-08"):
    lines = [rec.edge_line(f, t, ty, snapshot_id) for f, t, ty in edges]
    return pack_records.write_lines(path, lines, key_of=lambda line: __import__("json").loads(line)["from"])


def a_drag_with_edges(tmp_path):
    codex = pubmed()
    drag = tmp_path / "drag"
    p = drag / "relations/year=2023/00001.jsonl.zst"
    info = edges_shard(p, [
        ("pubmed:1", "pubmed:2", "pubmed:cites"), ("pubmed:1", "pubmed:3", "pubmed:cites"),
        ("pubmed:2", "pubmed:3", "pubmed:cites"), ("pubmed:3", "pubmed:9", "pubmed:cites"),
        ("pubmed:3", "eli:eu:dir:2019:1937", "pubmed:cites"), ("pubmed:2", "pubmed:1", "pubmed:corrects")])
    shard = sn.Shard(slice="year=2023", n=1, root="sha256:" + "11" * 32, records_root="sha256:" + "22" * 32,
                     first_id="1", last_id="9", records=4, chunks=4, chunks_root="sha256:" + "33" * 32,
                     tokens=2048, bytes=1)
    relation = sn.RelationShard(slice="year=2023", n=1, root=pieces_root_of(p), edges=info.records, bytes=info.bytes)
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None, shards=(shard,),
                       relations=(relation,), delta=sn.Delta((), (), ()), files={})
    con = sqlite3.connect(":memory:", isolation_level=None)       # autocommit, as the manifests are
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO documents(uid) VALUES(?)", [("pubmed:1",), ("pubmed:2",), ("pubmed:3",)])
    return codex, drag, snap, con, p


def test_edge_lines_round_trip_through_the_framing_and_are_indexed_once_per_root(tmp_path):
    codex, drag, snap, con, p = a_drag_with_edges(tmp_path)
    assert [e["from"] for e in pack_records.iter_records(p)][:2] == ["pubmed:1", "pubmed:1"]
    assert pack_records.read_index(p).uids["pubmed:3"] == [0, 3], "the index keyed by `from`: frame and line"
    assert traverse.index_edges(con, drag, snap) == {"shards": 1, "edges": 6, "skipped": 0}
    assert traverse.index_edges(con, drag, snap) == {"shards": 0, "edges": 0, "skipped": 1}, "the same root: skipped"
    assert con.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 6
    # re-issued: replaced whole
    edges_shard(p, [("pubmed:1", "pubmed:2", "pubmed:cites")])
    snap2 = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-09", previous=snap.hash,
                        shards=snap.shards, relations=(sn.RelationShard(slice="year=2023", n=1, root=pieces_root_of(p),
                                                                        edges=1, bytes=p.stat().st_size),),
                        delta=sn.Delta((), (), ()), files={})
    assert traverse.index_edges(con, drag, snap2)["edges"] == 1
    assert con.execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 1
    assert traverse.index_edges(con, drag, None) == {"shards": 0, "edges": 0, "skipped": 0}


def test_the_walk_goes_both_ways_depth_hops_and_names_what_is_unresolved(tmp_path):
    codex, drag, snap, con, p = a_drag_with_edges(tmp_path)
    traverse.index_edges(con, drag, snap)
    one = traverse.walk(con, codex, snap, "pubmed:2", depth=1)
    assert sorted((e["from"], e["to"]) for e in one) == [("pubmed:1", "pubmed:2"), ("pubmed:2", "pubmed:1"),
                                                         ("pubmed:2", "pubmed:3")], "inbound and outbound"
    assert all(e["resolved"] and e["needs"] is None and e["collection_id"] == codex.collection_id for e in one)
    cites = traverse.walk(con, codex, snap, "pubmed:2", relation="pubmed:cites", depth=1)
    assert sorted(e["type"] for e in cites) == ["pubmed:cites", "pubmed:cites"]
    two = traverse.walk(con, codex, snap, "pubmed:1", depth=2)
    tos = {e["to"] for e in two}
    assert {"pubmed:2", "pubmed:3", "pubmed:9", "eli:eu:dir:2019:1937", "pubmed:1"} <= tos
    by_to = {e["to"]: e for e in two}
    assert by_to["pubmed:9"]["resolved"] is False and by_to["pubmed:9"]["needs"] == {
        "collection_id": codex.collection_id, "shard": "year=2023/00001"}, "a uid of ours not held: the shard named"
    assert by_to["eli:eu:dir:2019:1937"]["resolved"] is False and by_to["eli:eu:dir:2019:1937"]["needs"] is None, \
        "a canonical id: another collection's to resolve (05 §5.5)"
    assert len({(e["from"], e["to"], e["type"]) for e in two}) == len(two), "each edge once"
    assert traverse.walk(con, codex, snap, "pubmed:404", depth=3) == []
