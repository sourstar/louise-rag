"""Golden test — the refactored plugin/collection pipeline must reproduce
tests/fixtures/gutenberg-mini/golden.json (captured from the pre-refactor
code by tests/capture_golden.py), with one user-approved deviation: chunk ids
gained a `{source}:` prefix (architecture-v2.md §1/§3's format). Every other
field — manifest column values, chapter text/tokens, FTS ranking order — is
asserted byte-identical to the pre-refactor golden snapshot.
"""
import json
from pathlib import Path

from app import chunking, collections as collections_mod, config, db, discovery, fts

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "gutenberg-mini" / "golden.json").read_text())

SHARED_COLUMNS = (
    "title", "authors", "lang", "subjects", "bookshelves", "mtime", "size",
    "in_scope", "rdf_done", "fts_done", "fts_version", "embed_done",
    "embed_version", "summary_done", "chunks", "tokens", "error", "retries",
    "txt_name", "txt_hash",
)


def _run_pipeline(con, collection):
    stats = discovery.sync_collection(con, collection)
    plugin = collection.plugin_instance()
    chunks_by_native: dict[int, list[dict]] = {}
    for row in con.execute("SELECT * FROM documents ORDER BY CAST(native_id AS INTEGER)"):
        native_id = int(row["native_id"])
        meta = plugin.read_metadata(Path(row["path"]), native_id)
        con.execute(
            "UPDATE documents SET title=?,authors=?,lang=?,subjects=?,bookshelves=?,"
            "rdf_done=1 WHERE uid=?",
            (meta["title"], meta["authors"], meta["lang"], meta["subjects"],
             meta["bookshelves"], row["uid"]))

        chapters = plugin.read_chapters(Path(row["path"]), native_id)
        chunks = list(chunking.chunk_book(row["uid"], native_id, chapters))
        chunks_by_native[native_id] = chunks
        fts.index_book(meta["lang"], native_id, chunks, collection)
        con.execute(
            "UPDATE documents SET fts_done=1,fts_version=?,chunks=?,tokens=? WHERE uid=?",
            (config.FTS_VERSION, len(chunks), sum(c["tokens"] for c in chunks), row["uid"]))
    return stats, chunks_by_native


def _pin_fixture_mtimes(corpus_dir: Path) -> None:
    """The golden records each text file's mtime as discovery saw it. git
    does not preserve mtimes, so on a fresh clone every fixture file carries
    the clone time and the `mtime` column could never match — the test
    failed on every new machine for an environmental reason. Pin the copied
    fixture's mtimes to the golden's values: what the pipeline then records
    is deterministic, and the comparison tests the pipeline, not the clone."""
    import os
    for book in GOLDEN["books"]:
        d = corpus_dir / str(book["id"])
        # parser 9: discovery's signature is the book file AND its catalogue
        # entry (the newer mtime, the summed size) — both pinned
        for f in (d / book["txt_name"], d / f"pg{book['id']}.rdf"):
            if f.exists():
                os.utime(f, (book["mtime"], book["mtime"]))


def test_golden_gutenberg_mini(isolated_config):
    collection = collections_mod.default_collection()
    _pin_fixture_mtimes(collection.corpus_dir)
    con = db.connect(collection)

    stats, chunks_by_native = _run_pipeline(con, collection)

    # --- discover stats: identical to the pre-refactor first run ---
    golden_stats = GOLDEN["discover_stats"]
    assert stats["seen"] == golden_stats["seen"]
    assert stats["added"] == golden_stats["added"]
    assert stats["changed"] == golden_stats["changed"]
    assert stats["missing_txt"] == golden_stats["missing_txt"]

    # --- idempotence: a second sync is a no-op ---
    stats2 = discovery.sync_collection(con, collection)
    assert stats2["added"] == 0
    assert stats2["changed"] == 0

    # --- manifest state: byte-identical on every shared column ---
    golden_books = {b["id"]: b for b in GOLDEN["books"]}
    rows = con.execute(
        "SELECT * FROM documents ORDER BY CAST(native_id AS INTEGER)").fetchall()
    assert len(rows) == len(golden_books)
    for row in rows:
        native_id = int(row["native_id"])
        assert row["uid"] == f"gutenberg:{native_id}"
        assert row["source"] == "gutenberg"
        expected = dict(golden_books[native_id])
        # the approved deviation of parser 9 (2026-10-11): `size` adds the
        # catalogue entry's bytes to the text's
        rdf_file = collection.corpus_dir / str(native_id) / f"pg{native_id}.rdf"
        if rdf_file.exists():
            expected["size"] += rdf_file.stat().st_size
        for col in SHARED_COLUMNS:
            assert row[col] == expected[col], f"book {native_id} column {col}"

    # --- chunks: id modulo the new uid prefix; everything else literal ---
    for native_id_str, golden_chunks in GOLDEN["chunks"].items():
        native_id = int(native_id_str)
        actual = chunks_by_native[native_id]
        assert len(actual) == len(golden_chunks)
        for a, g in zip(actual, golden_chunks):
            assert a["id"] == f"gutenberg:{g['id']}"
            assert a["seq"] == g["seq"]
            assert a["chapter"] == g["chapter"]
            assert a["text"] == g["text"]
            assert a["tokens"] == g["tokens"]

    # --- FTS search results: same hits/order, chunk_id modulo the prefix ---
    for key, golden_hits in GOLDEN["search"].items():
        group, _, q = key.partition(":")
        hits = fts.search(group, q, limit=20, collection=collection)
        assert len(hits) == len(golden_hits)
        for h, g in zip(hits, golden_hits):
            assert h["book_id"] == g["book_id"]
            assert h["chunk_id"] == f"gutenberg:{g['chunk_id']}"
            assert h["chapter"] == g["chapter"]
            assert h["lang"] == g["lang"]
