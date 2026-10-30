"""tools/wikipedia_drop_twins.py: the dump-layer documents minted a second
time for pages the archive holds (wikipedia-v8, 2026-10-30: the archive was
reached through a link rglob does not enter) go — their text-index,
catalogue and vector entries first, then their manifest rows — and nothing
else does."""
from __future__ import annotations

import threading

import numpy as np
import pytest
import tantivy

from app import catalog, collections as collections_mod, db, embeddings, fts
from app.plugins.wikipedia.ids import native_id_of
from tools import wikipedia_drop_twins as tool

EN, DE = "en", "de"
ARCHIVE = [(EN, 10), (EN, 11), (DE, 5), (EN, 12)]
TWINS = [(EN, 10), (EN, 11), (DE, 5)]           # unembedded dump copies
EMBEDDED_TWIN = (EN, 12)                         # a dump copy that was embedded: kept
DUMP_ONLY = (EN, 20)                             # a page the archive lacks: kept


def _uid(nid: int) -> str:
    return f"wikipedia:{nid}"


@pytest.fixture
def v8(tmp_path, isolated_config):
    c = collections_mod.Collection(
        name="wikipedia-v8", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "v8" / "manifest.db",
        fts_dir=tmp_path / "v8" / "fts", lance_dir=tmp_path / "v8" / "lancedb")
    con = db.connect(c)
    rows = ([(native_id_of(l, p, archive=True), l, 1) for l, p in ARCHIVE]
            + [(native_id_of(l, p), l, 0) for l, p in TWINS]
            + [(native_id_of(*EMBEDDED_TWIN), EN, 1), (native_id_of(*DUMP_ONLY), EN, 0)])
    for nid, lang, embedded in rows:
        con.execute("INSERT INTO documents(uid, source, native_id, path, lang, title, rdf_done, "
                    "fts_done, embed_done) VALUES(?,?,?,?,?,?,1,1,?)",
                    (_uid(nid), "wikipedia", str(nid), "x", lang, f"page {nid}", embedded))
    docs = con.execute("SELECT rowid, uid, native_id, lang, title FROM documents").fetchall()
    fts.index_books([(r["lang"], int(r["native_id"]),
                      [{"id": f"{r['uid']}:0:1", "chapter": "", "text": f"the moon {r['native_id']}"}])
                     for r in docs], c)
    catalog.write(c, [(r["rowid"], r["title"], "", "", r["lang"]) for r in docs])
    # vectors: the archive's, and one a crash left for an unembedded twin
    held = [native_id_of(EN, 10, archive=True), native_id_of(EN, 10)]
    embeddings._create_table(embeddings._conn(c), [
        {"id": f"{_uid(b)}:0:1", "book_id": b, "lang": EN, "seq": 0, "chapter": "",
         "text": "the moon", "vector": np.ones(4, dtype=np.float32)} for b in held])
    for r in docs:
        con.execute("INSERT INTO embed_queue(uid, priority, ts) VALUES(?,0,0)", (r["uid"],))
        con.execute("INSERT INTO doc_keys(uid, entity_id) VALUES(?,?)", (r["uid"], "Q1"))
        con.execute("INSERT INTO issues(uid, code, detail, first_seen, last_seen) VALUES(?,?,'',0,0)",
                    (r["uid"], "x"))
        con.execute("INSERT INTO chunk_hashes(text_hash, chunk_id, native_id) VALUES(?,?,?)",
                    (r["uid"].encode(), f"{r['uid']}:0:1", int(r["native_id"])))
        con.execute("INSERT INTO verify_queue(chunk_id, ts) VALUES(?,0)", (f"{r['uid']}:0:1",))
    yield c, con
    con.close()
    fts.close_all()
    catalog.close_all()


def _held_uids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT uid FROM documents")}


def _vector_books(c) -> set[int]:
    return set(embeddings.books_with_vectors(
        [native_id_of(EN, 10, archive=True), native_id_of(EN, 10)], c))


def test_a_dry_run_counts_what_each_index_holds_and_writes_nothing(v8):
    c, con = v8
    before = _held_uids(con)
    out = tool.run(con, c, log=lambda *_: None)
    assert (out["twins"], out["removed"], out["kept_embedded"]) == (3, 3, 1)
    assert (out["text_entries"], out["catalogue_entries"], out["with_vectors"]) == (3, 3, 1)
    assert _held_uids(con) == before
    assert fts.books_held("en", [native_id_of(EN, 10)], c) == 1
    assert _vector_books(c) == {native_id_of(EN, 10, archive=True), native_id_of(EN, 10)}


def test_only_the_unembedded_twins_go_and_every_trace_of_them_with_them(v8):
    c, con = v8
    twin_rows = {r[0]: r[1] for r in con.execute(
        f"SELECT uid, rowid FROM documents WHERE uid IN ({','.join('?' * 3)})",
        [_uid(native_id_of(l, p)) for l, p in TWINS])}
    keep = _held_uids(con) - set(twin_rows)
    out = tool.run(con, c, apply=True, log=lambda *_: None)
    assert (out["removed"], out["rechecked_kept"], out["interrupted"]) == (3, 0, False)
    assert _held_uids(con) == keep, "the archive, the embedded twin and the dump-only page stay"
    for lang, page in TWINS:
        nid = native_id_of(lang, page)
        assert fts.books_held(fts.group_for(lang), [nid], c) == 0
        assert fts.books_held(fts.group_for(lang), [native_id_of(lang, page, archive=True)], c) == 1
    assert catalog.rows_held(c, list(twin_rows.values())) == 0
    assert catalog.rows_held(c, [r[0] for r in con.execute("SELECT rowid FROM documents")]) == len(keep)
    assert _vector_books(c) == {native_id_of(EN, 10, archive=True)}
    for table in ("embed_queue", "doc_keys", "issues"):
        assert {r[0] for r in con.execute(f"SELECT uid FROM {table}")} == keep, table
    assert {f"wikipedia:{r[0]}" for r in con.execute("SELECT native_id FROM chunk_hashes")} == keep
    assert {r[0].rsplit(":", 2)[0] for r in con.execute("SELECT chunk_id FROM verify_queue")} == keep


def test_a_second_run_finds_nothing(v8):
    c, con = v8
    tool.run(con, c, apply=True, log=lambda *_: None)
    again = tool.run(con, c, apply=True, log=lambda *_: None)
    assert (again["twins"], again["removed"], again["kept_embedded"]) == (0, 0, 1)


def test_a_signal_ends_it_at_a_batch_boundary_and_a_rerun_takes_the_rest(v8):
    c, con = v8
    stop = threading.Event()
    said: list[str] = []

    def log(line: str) -> None:
        said.append(line)
        if "1 removed" in line:
            stop.set()
    out = tool.run(con, c, apply=True, batch=1, stop=stop, log=log)
    assert (out["removed"], out["interrupted"]) == (1, True)
    rest = tool.run(con, c, apply=True, log=lambda *_: None)
    assert (rest["removed"], rest["interrupted"]) == (2, False)


def test_an_index_another_process_writes_stops_the_batch_before_the_manifest(v8):
    """The worker keeps one writer per index while it runs: a batch with an
    entry there cannot be removed, and nothing of it leaves the manifest."""
    c, con = v8
    fts.close_all()
    other = tantivy.Index.open(str(c.fts_dir / "en"))
    held = other.writer(heap_size=15_000_000)
    before = _held_uids(con)
    try:
        with pytest.raises(tool.WritersBusy, match="louise node stop"):
            tool.run(con, c, apply=True, log=lambda *_: None)
    finally:
        held.wait_merging_threads()
    assert _held_uids(con) == before


def test_the_twin_join_is_answered_by_the_uid_key(v8):
    _c, con = v8
    plan = " | ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN " + tool._TWINS, ("wikipedia", tool.DUMP_BASE, 0, 10, tool.DUMP_BASE)))
    assert "TEMP B-TREE" not in plan and "LIST SUBQUERY" not in plan, plan
    assert "USING INTEGER PRIMARY KEY" in plan and "INDEX sqlite_autoindex_documents_1" in plan, plan
