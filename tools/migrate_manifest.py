"""Idempotent manifest migration: books -> documents, book_id -> uid.

See docs/architecture-v2.md §3 and CLAUDE.md invariant 3 ("never silently") —
this never runs automatically; an operator invokes it explicitly.

Usage:
  python -m tools.migrate_manifest --db /data/manifest.db [--source gutenberg]
                                    [--dry-run] [--no-backup]

Always transforms a scratch copy of the database first; the original file is
never touched until the transform succeeds and is verified. Already-migrated
databases are a no-op.
"""
import argparse
import os
import shutil
import sqlite3
import time
from pathlib import Path


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _row_count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _already_migrated(con: sqlite3.Connection) -> bool:
    return _table_exists(con, "documents") and not _table_exists(con, "books")


def _migrate_books(con: sqlite3.Connection, source: str) -> tuple[int, int]:
    old_count = _row_count(con, "books")
    con.execute("""
        CREATE TABLE documents (
          uid TEXT PRIMARY KEY,
          source TEXT NOT NULL,
          native_id TEXT NOT NULL,
          path TEXT NOT NULL,
          txt_name TEXT,
          lang TEXT,
          title TEXT,
          authors TEXT,
          subjects TEXT,
          bookshelves TEXT,
          mtime INTEGER,
          size INTEGER,
          txt_hash TEXT,
          in_scope INTEGER DEFAULT 1,
          rdf_done INTEGER DEFAULT 0,
          fts_done INTEGER DEFAULT 0,
          fts_version INTEGER DEFAULT 0,
          embed_done INTEGER DEFAULT 0,
          embed_version INTEGER DEFAULT 0,
          summary_done INTEGER DEFAULT 0,
          chunks INTEGER DEFAULT 0,
          tokens INTEGER DEFAULT 0,
          error TEXT,
          retries INTEGER DEFAULT 0,
          updated_at INTEGER
        )
    """)
    con.execute("""
        INSERT INTO documents(uid,source,native_id,path,txt_name,lang,title,authors,
          subjects,bookshelves,mtime,size,txt_hash,in_scope,rdf_done,fts_done,
          fts_version,embed_done,embed_version,summary_done,chunks,tokens,error,
          retries,updated_at)
        SELECT ? || ':' || id, ?, CAST(id AS TEXT), path,txt_name,lang,title,authors,
          subjects,bookshelves,mtime,size,txt_hash,in_scope,rdf_done,fts_done,
          fts_version,embed_done,embed_version,summary_done,chunks,tokens,error,
          retries,updated_at
        FROM books
    """, (source, source))
    con.execute("CREATE UNIQUE INDEX idx_documents_native ON documents(source, native_id)")
    con.execute("CREATE INDEX idx_documents_scope ON documents(in_scope, rdf_done, fts_done, embed_done)")
    con.execute("CREATE INDEX idx_documents_lang ON documents(lang)")
    con.execute("DROP TABLE books")
    return old_count, _row_count(con, "documents")


def _migrate_catalog_fts(con: sqlite3.Connection, source: str) -> tuple[int, int] | None:
    if not _table_exists(con, "catalog_fts"):
        return None
    old_rows = list(con.execute(
        "SELECT book_id, title, authors, subjects, bookshelves FROM catalog_fts"))
    con.execute("DROP TABLE catalog_fts")
    con.execute("""
        CREATE VIRTUAL TABLE catalog_fts USING fts5(
          uid UNINDEXED, title, authors, subjects, bookshelves)
    """)
    for book_id, title, authors, subjects, bookshelves in old_rows:
        con.execute(
            "INSERT INTO catalog_fts(uid,title,authors,subjects,bookshelves) "
            "VALUES(?,?,?,?,?)",
            (f"{source}:{book_id}", title, authors, subjects, bookshelves))
    return len(old_rows), _row_count(con, "catalog_fts")


def _migrate_embed_queue(con: sqlite3.Connection, source: str) -> tuple[int, int] | None:
    if not _table_exists(con, "embed_queue"):
        return None
    old_rows = list(con.execute("SELECT book_id, priority, ts FROM embed_queue"))
    con.execute("DROP TABLE embed_queue")
    con.execute("""
        CREATE TABLE embed_queue (uid TEXT PRIMARY KEY, priority INTEGER DEFAULT 0, ts INTEGER)
    """)
    for book_id, priority, ts in old_rows:
        con.execute("INSERT INTO embed_queue(uid,priority,ts) VALUES(?,?,?)",
                    (f"{source}:{book_id}", priority, ts))
    return len(old_rows), _row_count(con, "embed_queue")


def _migrate_summaries(con: sqlite3.Connection, source: str) -> tuple[int, int] | None:
    if not _table_exists(con, "summaries"):
        return None
    old_rows = list(con.execute(
        "SELECT book_id, model, created_at, summary, chapter_summaries FROM summaries"))
    con.execute("DROP TABLE summaries")
    con.execute("""
        CREATE TABLE summaries (
          uid TEXT PRIMARY KEY, model TEXT, created_at INTEGER,
          summary TEXT, chapter_summaries TEXT)
    """)
    for book_id, model, created_at, summary, chapter_summaries in old_rows:
        con.execute(
            "INSERT INTO summaries(uid,model,created_at,summary,chapter_summaries) "
            "VALUES(?,?,?,?,?)",
            (f"{source}:{book_id}", model, created_at, summary, chapter_summaries))
    return len(old_rows), _row_count(con, "summaries")


def _migrate_events(con: sqlite3.Connection) -> None:
    if not _table_exists(con, "events"):
        return
    cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    if "book_id" in cols and "doc_uid" not in cols:
        con.execute("ALTER TABLE events RENAME COLUMN book_id TO doc_uid")


def migrate(con: sqlite3.Connection, source: str) -> dict:
    """Runs inside an already-open transaction. Returns {table: (old, new)}."""
    stats: dict = {"documents": _migrate_books(con, source)}
    for name, fn in (("catalog_fts", _migrate_catalog_fts),
                      ("embed_queue", _migrate_embed_queue),
                      ("summaries", _migrate_summaries)):
        result = fn(con, source)
        if result is not None:
            stats[name] = result
    _migrate_events(con)
    con.execute("""
        CREATE TABLE IF NOT EXISTS space (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          space_id TEXT, dim INTEGER, components TEXT, pinned_at INTEGER
        )
    """)
    return stats


def _verify(con: sqlite3.Connection, stats: dict) -> None:
    for table, (old, new) in stats.items():
        if old != new:
            raise RuntimeError(
                f"row count mismatch after migrating {table}: {old} -> {new}")
    bad = con.execute(
        "SELECT COUNT(*) FROM documents WHERE uid != source || ':' || native_id"
    ).fetchone()[0]
    if bad:
        raise RuntimeError(f"{bad} documents rows have a malformed uid after migration")


def run(db_path: Path, source: str, dry_run: bool, make_backup: bool) -> dict:
    if not db_path.exists():
        raise SystemExit(f"no such file: {db_path}")

    con = sqlite3.connect(db_path)
    try:
        if _already_migrated(con):
            print(f"{db_path}: already migrated (documents present, books absent) — no-op")
            return {"status": "already-migrated"}
        if not _table_exists(con, "books"):
            raise SystemExit(f"{db_path}: no 'books' table found — nothing to migrate")
        # fold the WAL into the main file so the scratch copy below is complete
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()

    scratch = db_path.parent / f".{db_path.name}.migrate-scratch-{int(time.time())}"
    shutil.copy2(db_path, scratch)
    try:
        con = sqlite3.connect(scratch)
        con.execute("BEGIN IMMEDIATE")
        try:
            stats = migrate(con, source)
            _verify(con, stats)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

        if dry_run:
            print(f"[dry-run] {db_path}: would migrate. Row-count deltas: {stats}")
            return {"status": "dry-run", "stats": stats}

        if make_backup:
            backup = db_path.with_name(db_path.name + f".bak-{int(time.time())}")
            shutil.copy2(db_path, backup)
            print(f"backed up original to {backup}")

        os.replace(scratch, db_path)
        print(f"{db_path}: migrated. Row-count deltas: {stats}")
        return {"status": "migrated", "stats": stats}
    finally:
        if scratch.exists():
            scratch.unlink()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--source", default="gutenberg",
                    help="source_id prefix for uid=f'{source}:{{native_id}}' (default: gutenberg)")
    p.add_argument("--dry-run", action="store_true",
                    help="report row-count deltas without touching the real file")
    p.add_argument("--no-backup", action="store_true",
                    help="skip writing a .bak-<timestamp> copy of the original file")
    args = p.parse_args()
    run(args.db, args.source, args.dry_run, not args.no_backup)


if __name__ == "__main__":
    main()
