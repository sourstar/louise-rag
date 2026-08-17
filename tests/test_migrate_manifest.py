"""tools/migrate_manifest.py: idempotent books->documents / book_id->uid
migration. Uses a hand-built pre-refactor-shaped manifest.db fixture (no live
corpus needed) since the migration is a schema transform independent of any
particular pipeline run.
"""
import sqlite3
from pathlib import Path

from tools import migrate_manifest

OLD_SCHEMA = """
CREATE TABLE books (
  id INTEGER PRIMARY KEY,
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
);
CREATE VIRTUAL TABLE catalog_fts USING fts5(
  book_id UNINDEXED, title, authors, subjects, bookshelves
);
CREATE TABLE control (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE embed_queue (book_id INTEGER PRIMARY KEY, priority INTEGER DEFAULT 0, ts INTEGER);
CREATE TABLE summaries (
  book_id INTEGER PRIMARY KEY, model TEXT, created_at INTEGER,
  summary TEXT, chapter_summaries TEXT
);
CREATE TABLE query_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, endpoint TEXT, query TEXT,
  mode TEXT, k INTEGER, t_catalog REAL, t_fts REAL, t_dense REAL, t_rerank REAL,
  t_llm REAL, total_ms REAL, n_results INTEGER, books TEXT
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, stage TEXT,
  book_id INTEGER, message TEXT
);
"""


def _make_old_db(path: Path):
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute(
        "INSERT INTO books(id,path,title,lang,rdf_done,updated_at) "
        "VALUES (1342,'/c/1342','A Study','en',1,0)")
    con.execute(
        "INSERT INTO books(id,path,title,lang,rdf_done,updated_at) "
        "VALUES (84,'/c/84','Frank','en',1,0)")
    con.execute(
        "INSERT INTO catalog_fts(book_id,title,authors,subjects,bookshelves) "
        "VALUES (1342,'A Study','A','S','B')")
    con.execute(
        "INSERT INTO catalog_fts(book_id,title,authors,subjects,bookshelves) "
        "VALUES (84,'Frank','M','S2','B2')")
    con.execute("INSERT INTO embed_queue(book_id,priority,ts) VALUES (1342,0,100)")
    con.execute(
        "INSERT INTO summaries(book_id,model,created_at,summary,chapter_summaries) "
        "VALUES (84,'m',1,'sum','[]')")
    con.execute(
        "INSERT INTO events(ts,level,stage,book_id,message) VALUES (1,'info','rdf',1342,'ok')")
    con.commit()
    con.close()


def test_dry_run_leaves_file_untouched(tmp_path):
    db_path = tmp_path / "manifest.db"
    _make_old_db(db_path)
    before = db_path.read_bytes()

    result = migrate_manifest.run(db_path, "gutenberg", dry_run=True, make_backup=False)

    assert result["status"] == "dry-run"
    assert db_path.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*"))


def test_migrate_row_counts_and_uid_format(tmp_path):
    db_path = tmp_path / "manifest.db"
    _make_old_db(db_path)

    result = migrate_manifest.run(db_path, "gutenberg", dry_run=False, make_backup=True)

    assert result["status"] == "migrated"
    assert list(tmp_path.glob("*.bak-*")), "expected a backup file"

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "documents" in tables
    assert "books" not in tables

    rows = {r["native_id"]: r for r in con.execute("SELECT * FROM documents")}
    assert set(rows) == {"1342", "84"}
    assert rows["1342"]["uid"] == "gutenberg:1342"
    assert rows["1342"]["source"] == "gutenberg"
    assert rows["1342"]["title"] == "A Study"

    fts_uids = {r[0] for r in con.execute("SELECT uid FROM catalog_fts")}
    assert fts_uids == {"gutenberg:1342", "gutenberg:84"}

    eq = [tuple(r) for r in con.execute("SELECT uid,priority,ts FROM embed_queue")]
    assert eq == [("gutenberg:1342", 0, 100)]

    summ = [tuple(r) for r in con.execute("SELECT uid,summary FROM summaries")]
    assert summ == [("gutenberg:84", "sum")]

    ev_cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
    assert "doc_uid" in ev_cols and "book_id" not in ev_cols
    con.close()


def test_migrate_is_idempotent(tmp_path):
    db_path = tmp_path / "manifest.db"
    _make_old_db(db_path)
    migrate_manifest.run(db_path, "gutenberg", dry_run=False, make_backup=False)
    after_first = db_path.read_bytes()

    result = migrate_manifest.run(db_path, "gutenberg", dry_run=False, make_backup=False)

    assert result["status"] == "already-migrated"
    assert db_path.read_bytes() == after_first
