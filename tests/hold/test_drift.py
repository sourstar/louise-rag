"""`louise dev index-drift` (app/hold/drift.py, 07 §7.3): what the node
serves — the manifest's counts and hashes, the text index — against the
latest held snapshot's records. The 127-part gap (STATUS, 2026-10-25): a
gutenberg node's manifest counts and text index came from an earlier parse
while its records had moved on, and nothing noticed."""
import hashlib
import json
import math
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import collections as collections_mod, db, fts, louise, worker
from app.hold import drift, records as rec, snapshot as sn
from app.pack import records as pack_records
from tests.hold.test_carry import _create, quiet

SEED = "ab" * 32
UID = "gutenberg:10001"            # en, two chunks, in part lang=en/00001 with 10002


def _drain(local) -> None:
    """The worker's extraction and text-index stages, until nothing is pending."""
    con = db.connect(local)
    try:
        for stage, pending in ((worker.stage_rdf, "rdf_done=0"), (worker.stage_fts, "fts_done=0")):
            while True:
                row = con.execute(f"SELECT * FROM documents WHERE in_scope=1 AND error IS NULL "
                                  f"AND {pending} ORDER BY uid LIMIT 1").fetchone()
                if row is None:
                    break
                stage(con, row, local)
                con.commit()
    finally:
        con.close()


@pytest.fixture
def held(isolated_config, tmp_path, corpus_dir):
    root = tmp_path / "collections"
    codex = _create(root, "g")
    rec.build(root, "g", corpus_dir=corpus_dir, records_per_shard=2, log=quiet)
    rec.write_validation(root, "g")
    sn.create(root, "g", snapshot_id="2026-08", log=quiet)
    local = rec.local_collection(root, "g", codex)
    _drain(local)
    return root, codex, local


def _cli(capsys, root, *args):
    code = louise.main(["dev", "index-drift", "g", "--root", str(root), "--seed", SEED, *args])
    out = capsys.readouterr().out
    return code, json.loads(out[out.index("{\n"):])


def _sql(local, sql, *params):
    con = db.connect(local)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def test_an_agreeing_collection_passes_and_cuts_nothing(held, capsys):
    root, _codex, _local = held
    code, out = _cli(capsys, root, "--sample", "0")
    assert code == 0 and out["named"] == 0 and out["documents"] == []
    assert (out["parts"], out["records"], out["served"], out["missing"]) == (2, 3, 3, 0)
    assert out["parts_cut"] == 0, "every part's manifest total is the snapshot's: no record is cut"
    assert out["served_without_record"] == 0 and out["took_s"] >= 0
    code, out = _cli(capsys, root, "--sample", "1")
    assert code == 0 and out["named"] == 0 and out["parts_text"] == 2, \
        "the text index's stored chunks are the records' own cut, chunk for chunk"


def test_a_manifest_count_older_than_its_record_is_named(held, capsys):
    """The 127-part shape: the row's count from an earlier parse, fts_version
    current, so the worker never re-indexes it."""
    root, _codex, local = held
    _sql(local, "UPDATE documents SET chunks=chunks+3 WHERE uid=?", UID)
    code, out = _cli(capsys, root, "--sample", "0")
    assert code == 1 and out["named"] == 1 and out["reindexable"] == 1
    (doc,) = out["documents"]
    assert doc["uid"] == UID and doc["part"] == "lang=en/00001"
    assert set(doc["kinds"]) == {"chunks", "index"}
    assert doc["manifest_chunks"] == doc["record_chunks"] + 3 == doc["index_chunks"] + 3
    assert out["parts_whose_total_differs"] == 1 and out["parts_cut"] == 1, "only the disagreeing part is cut"
    assert out["named_fts_versions"] == {"1": 1}


def test_a_text_index_older_than_its_record_is_named(held, capsys):
    root, codex, local = held
    (record,) = [r for r in pack_records.iter_records(
        root / "g" / "shard" / sn.load(root / "g" / "shard" / "snapshots" / "2026-08.json").shards[0].path)
        if r["uid"] == UID]
    pieces = drift.record_cut(codex, record)
    # the same number of chunks, one of them another parse's text
    stale = [dict(c, text=c["text"] + " (an earlier parse)") if k == 0 else c for k, c in enumerate(pieces)]
    fts.index_books([("en", 10001, stale)], local)
    code, out = _cli(capsys, root, "--sample", "0")
    assert code == 0, "a count cannot see a text that moved: that is what the sample is for"
    code, out = _cli(capsys, root, "--sample", "1")
    assert code == 1 and [(d["uid"], d["kinds"]) for d in out["documents"]] == [(UID, ["text"])]
    # one chunk fewer in the index: seen by the count, without the sample
    fts.index_books([("en", 10001, pieces[:-1])], local)
    code, out = _cli(capsys, root, "--sample", "0")
    assert code == 1 and [(d["uid"], d["kinds"], d["index_chunks"]) for d in out["documents"]] == \
        [(UID, ["index"], len(pieces) - 1)]


def test_hashes_are_named_but_left_out_of_the_reindex_list(held, capsys, tmp_path):
    """content_hash: the row predates a revised record; origin: the live
    origin is another revision, so a re-index from it cannot reach the
    record — neither is `dev reindex`'s to repair."""
    root, _codex, local = held
    _sql(local, "UPDATE documents SET content_hash=? WHERE uid='gutenberg:10002'", "0" * 32)
    _sql(local, "UPDATE documents SET txt_hash=?, chunks=chunks+1 WHERE uid='gutenberg:10003'", "f" * 32)
    ids = tmp_path / "ids.txt"
    code, out = _cli(capsys, root, "--sample", "0", "--ids-out", str(ids))
    kinds = {d["uid"]: sorted(d["kinds"]) for d in out["documents"]}
    assert code == 1 and kinds == {"gutenberg:10002": ["content_hash"],
                                   "gutenberg:10003": ["chunks", "index", "origin"]}
    assert out["reindexable"] == 0 and ids.read_text(encoding="utf-8") == ""


def test_pending_documents_are_counted_not_named(held, capsys):
    root, _codex, local = held
    _sql(local, "UPDATE documents SET fts_done=0 WHERE uid=?", UID)
    _sql(local, "UPDATE documents SET fts_version=0 WHERE uid='gutenberg:10003'")
    code, out = _cli(capsys, root, "--sample", "0")
    assert code == 0 and out["named"] == 0
    assert out["pending"] == {"not_indexed": 1, "queued": 1} and out["served"] == 1
    assert out["parts_cut"] == 1, "a part with an unserved record cannot be judged by its total: it is cut"


def test_the_ids_file_round_trips_into_dev_reindex(held, capsys, tmp_path):
    """index-drift --ids-out → dev reindex --ids → the worker's text stage →
    index-drift agrees again."""
    root, codex, local = held
    _sql(local, "UPDATE documents SET chunks=chunks+2 WHERE uid=?", UID)
    record = next(r for r in pack_records.iter_records(
        root / "g" / "shard" / sn.load(root / "g" / "shard" / "snapshots" / "2026-08.json").shards[1].path))
    pieces = drift.record_cut(codex, record)
    fts.index_books([(record["lang"], int(record["native_id"]),
                      [dict(c, text="stale") for c in pieces])], local)
    ids = tmp_path / "ids.txt"
    code, out = _cli(capsys, root, "--sample", "1", "--ids-out", str(ids))
    assert code == 1 and out["ids_out"] == str(ids)
    assert ids.read_text(encoding="utf-8").splitlines() == sorted([UID, record["uid"]])
    assert louise.main(["dev", "reindex", "g", "--root", str(root), "--ids", str(ids), "--yes"]) == 0
    capsys.readouterr()
    _drain(local)
    code, out = _cli(capsys, root, "--sample", "1")
    assert code == 0 and out["named"] == 0


def test_the_check_writes_nothing(held, capsys):
    root, _codex, local = held
    _sql(local, "UPDATE documents SET chunks=chunks+1 WHERE uid=?", UID)
    fts.close_all()            # the stage's kept writer finishes its merges first

    def state():
        # `-shm` is SQLite's wal-index, where every READER keeps its read
        # mark, and a reader of a WAL database opens an empty `-wal`
        files = sorted(p for p in Path(local.manifest_path).parent.iterdir()
                       if p.is_file() and not p.name.endswith("-shm")
                       and not (p.name.endswith("-wal") and p.stat().st_size == 0))
        files += sorted(p for p in Path(local.fts_dir).rglob("*") if p.is_file() and p.name != ".tantivy-meta.lock")
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    before = state()
    assert _cli(capsys, root, "--sample", "1")[0] == 1
    assert state() == before


def test_a_stop_ends_the_walk_at_a_part_boundary(held):
    """07 §7.7: a signal ends the walk after the current part; the report
    covers the parts walked and says so (the CLI exits 130)."""
    root, codex, local = held
    _sql(local, "UPDATE documents SET chunks=chunks+1 WHERE uid='gutenberg:10003'")    # in the second part
    asked = iter([False, True])
    out = drift.run(root, "g", codex, sn.load(root / "g" / "shard" / "snapshots" / "2026-08.json"), local,
                    sample="0", stop=lambda: next(asked))
    assert out["interrupted"] and out["parts"] == 1 and out["named"] == 0
    assert out["served_without_record"] is None, "a partial walk cannot count what no record accounts for"


def test_a_row_the_worker_moves_during_the_walk_is_set_aside_not_named(held, monkeypatch):
    """The check runs beside the worker: a document re-indexed between the
    manifest read and the index read would look stale for that instant."""
    root, codex, local = held
    _sql(local, "UPDATE documents SET chunks=chunks+1 WHERE uid=?", UID)
    real = fts.chunk_counts

    def and_the_worker_moves_it(collection, ids):
        got = real(collection, ids)
        _sql(local, "UPDATE documents SET updated_at=updated_at+1 WHERE uid=?", UID)
        return got
    monkeypatch.setattr(fts, "chunk_counts", and_the_worker_moves_it)
    out = drift.run(root, "g", codex, sn.load(root / "g" / "shard" / "snapshots" / "2026-08.json"), local,
                    sample="0")
    assert out["named"] == 0 and out["changed_during_walk"] == 1 and out["ids"] == []


def test_an_unknown_part_is_refused(held, capsys):
    root, _codex, _local = held
    assert louise.main(["dev", "index-drift", "g", "--root", str(root), "--shards", "lang=xx/00001"]) == 2
    assert "names no part lang=xx/00001" in capsys.readouterr().err


def test_the_manifest_reads_are_indexed(held):
    """CLAUDE.md: selection queries plan-checked — the window's read on the
    primary key, the served count on the covering scope index."""
    _root, _codex, local = held
    con = db.connect(local)
    try:
        plan = " | ".join(r[3] for r in con.execute(
            "EXPLAIN QUERY PLAN " + drift.ROWS_SQL.format(",".join("?" * drift.SQL_BATCH)),
            [f"g:{i}" for i in range(drift.SQL_BATCH)]))
        assert "SEARCH documents USING" in plan and "(uid=?)" in plan, plan
        plan2 = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + drift.SERVED_COUNT_SQL))
        assert "COVERING INDEX idx_documents_scope" in plan2, plan2
        for p in (plan, plan2):
            assert "TEMP B-TREE" not in p and "LIST SUBQUERY" not in p and "SCAN" not in p, p
    finally:
        con.close()


# ---- the cost: windows, and nothing that grows with what was walked ----------

def _synthetic(tmp_path, n_records: int, per_part: int, chunks_each: int = 3):
    """A collection's parts, manifest and text index written directly: the
    agreeing shape a node is in after its text stage, at a size records build
    does not reach in a test."""
    c = collections_mod.Collection(
        name="syn", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=tmp_path / "none",
        manifest_path=tmp_path / "syn" / "state" / "manifest.sqlite", fts_dir=tmp_path / "syn" / "index",
        lance_dir=tmp_path / "syn" / "lance")
    shards, rows, entries = [], [], []
    for p in range(math.ceil(n_records / per_part)):
        recs = []
        for i in range(p * per_part, min(n_records, (p + 1) * per_part)):
            nid = 1_000_000 + i
            secs = [[f"Chapter {k}", f"text {nid} {k}"] for k in range(chunks_each)]
            recs.append({"uid": f"gutenberg:{nid}", "native_id": str(nid), "lang": "en", "title": f"t{nid}",
                         "creators": [], "date": None, "subjects": [], "keys": [], "sections": secs,
                         "extra": {}, "origin": str(nid), "content_hash": f"{nid:032x}",
                         "origin_hash": f"{nid:032x}"})
            rows.append((f"gutenberg:{nid}", str(nid), chunks_each, f"{nid:032x}"))
            entries.append(("en", nid, [{"id": f"gutenberg:{nid}:{k}", "chapter": f"Chapter {k}",
                                         "text": f"text {nid} {k}", "tokens": 3} for k in range(chunks_each)]))
        shard = SimpleNamespace(slice="lang=en", n=p + 1, chunks=len(recs) * chunks_each,
                                path=f"lang=en/{p + 1:05d}.jsonl.zst")
        pack_records.write_shard(tmp_path / "syn" / "shard" / shard.path, recs)
        shards.append(shard)
    con = db.connect(c)
    con.executemany("INSERT INTO documents (uid, source, native_id, path, chunks, content_hash, in_scope, "
                    "rdf_done, fts_done, fts_version, entity_version) VALUES (?, 'gutenberg', ?, '', ?, ?, "
                    "1, 1, 1, 1, 1)", rows)
    con.commit()
    con.close()
    for k in range(0, len(entries), 5_000):
        fts.index_books(entries[k:k + 5_000], c)
    return c, SimpleNamespace(snapshot_id="2026-09", shards=shards)


def test_a_walk_is_windows_of_bounded_work(tmp_path, isolated_config, monkeypatch):
    """Every manifest read and every index aggregation covers at most one
    window, their number is the windows', and an agreeing collection cuts
    nothing — so the cost is per record walked, whatever came before."""
    from app.hold import collection as col
    n, per_part, window = 12_000, 5_000, 2_000
    c, snapshot = _synthetic(tmp_path, n, per_part)
    monkeypatch.setattr(col, "shard_dir_of", lambda root, name: Path(root) / name / "shard")
    seen = {"rows": [], "counts": [], "cut": 0}
    real_rows, real_counts, real_cut = drift.manifest_rows, fts.chunk_counts, drift.record_cut
    monkeypatch.setattr(drift, "manifest_rows", lambda con, uids: seen["rows"].append(len(uids)) or real_rows(con, uids))
    monkeypatch.setattr(fts, "chunk_counts", lambda col_, ids: seen["counts"].append(len(ids)) or real_counts(col_, ids))
    monkeypatch.setattr(drift, "record_cut", lambda *a: seen.__setitem__("cut", seen["cut"] + 1) or real_cut(*a))
    t = time.monotonic()
    out = drift.run(tmp_path, "syn", None, snapshot, c, sample="0", window=window)
    took = time.monotonic() - t
    windows = sum(math.ceil(min(per_part, n - k) / window) for k in range(0, n, per_part))
    assert out["named"] == 0 and out["records"] == n and out["parts"] == 3 and out["served_without_record"] == 0
    assert len(seen["rows"]) == len(seen["counts"]) == windows and max(seen["rows"]) <= window
    assert seen["cut"] == 0
    print(f"index-drift: {n} records in {took:.2f}s, {took / n * 1e6:.0f} µs a record")
    assert took / n < 2e-3, "a generous bound: measured 42-58 µs a record unsampled, 20k-300k records (STATUS, 2026-10-25)"
