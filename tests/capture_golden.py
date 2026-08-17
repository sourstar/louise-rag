"""One-time capture of golden pipeline output from *today's unmodified* app/
code, run against tests/fixtures/gutenberg-mini/corpus and committed as
golden.json — the P0.1 refactor's test oracle (see docs/architecture-v2.md
task P0.1 and CLAUDE.md).

Run this ONCE, before any refactor code lands, then commit the resulting
golden.json. Do not re-run it after the refactor — golden.json is meant to
freeze pre-refactor behavior, not track the current tree.

Usage: python -m tests.capture_golden
"""
import json
import shutil
import tempfile
from pathlib import Path

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "golden.json"

# One deterministic full-text query per language group present in the fixture
# corpus, chosen from words that appear verbatim (no stemmer ambiguity).
QUERIES = {"en": ["committee", "bridge"], "fr": ["horloger"]}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        corpus_dir = tmp / "corpus"
        shutil.copytree(FIXTURE_CORPUS, corpus_dir)
        data_dir = tmp / "data"
        data_dir.mkdir()

        from app import config

        config.CORPUS_DIR = corpus_dir
        config.DATA_DIR = data_dir
        config.DB_PATH = data_dir / "manifest.db"
        config.FTS_DIR = data_dir / "fts"
        config.LANCE_DIR = data_dir / "lancedb"
        config.ensure_dirs()

        from app import chunking, db, discovery, fts, pg_text, rdf

        con = db.connect()
        stats = discovery.run(con)

        chunks_by_book: dict[int, list[dict]] = {}
        for row in con.execute("SELECT * FROM books ORDER BY id").fetchall():
            book_id = row["id"]
            meta = rdf.parse(Path(row["path"]), book_id)
            con.execute(
                "UPDATE books SET title=?,authors=?,lang=?,subjects=?,bookshelves=?,"
                "rdf_done=1 WHERE id=?",
                (meta["title"], meta["authors"], meta["lang"], meta["subjects"],
                 meta["bookshelves"], book_id))
            con.execute("DELETE FROM catalog_fts WHERE book_id=?", (book_id,))
            con.execute(
                "INSERT INTO catalog_fts(book_id,title,authors,subjects,bookshelves) "
                "VALUES(?,?,?,?,?)",
                (book_id, meta["title"], meta["authors"], meta["subjects"],
                 meta["bookshelves"]))

            txt = pg_text.find_txt(Path(row["path"]), book_id)
            text = pg_text.read_clean(txt)
            chapters = pg_text.split_chapters(text)
            chunks = list(chunking.chunk_book(book_id, chapters))
            chunks_by_book[book_id] = chunks
            fts.index_book(meta["lang"], book_id, chunks)
            con.execute(
                "UPDATE books SET fts_done=1,fts_version=?,chunks=?,tokens=? WHERE id=?",
                (config.FTS_VERSION, len(chunks),
                 sum(c["tokens"] for c in chunks), book_id))

        books = [dict(r) for r in con.execute("SELECT * FROM books ORDER BY id")]

        search_results: dict[str, list[dict]] = {}
        for group, queries in QUERIES.items():
            for q in queries:
                hits = fts.search(group, q, limit=20)
                search_results[f"{group}:{q}"] = [
                    {"book_id": h["book_id"], "chunk_id": h["chunk_id"],
                     "chapter": h["chapter"], "lang": h["lang"]}
                    for h in hits
                ]

        golden = {
            "discover_stats": {k: v for k, v in stats.items() if k != "secs"},
            "books": [
                {k: v for k, v in b.items() if k not in ("path", "updated_at")}
                for b in books
            ],
            "chunks": {
                str(book_id): [
                    {"id": c["id"], "seq": c["seq"], "chapter": c["chapter"],
                     "text": c["text"], "tokens": c["tokens"]}
                    for c in chunks
                ]
                for book_id, chunks in chunks_by_book.items()
            },
            "search": search_results,
        }
        GOLDEN_PATH.write_text(
            json.dumps(golden, indent=2, ensure_ascii=False, sort_keys=True))
        print(f"wrote {GOLDEN_PATH}")


if __name__ == "__main__":
    main()
