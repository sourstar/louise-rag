"""tests/test_db_schema_migration.py — `db.connect` on an OLD manifest.

Every other test in this suite builds its manifest fresh, so `CREATE TABLE
IF NOT EXISTS` always makes every column and the ALTER-based migrations in
`db.connect` are always no-ops. That is why 1,942 passing tests said
nothing about the bug this file exists for.

**Observed live 2026-09-10.** The reference node's 79,320-row manifest,
built in August, met the migration branch for the first time and the
worker crash-looped:

    sqlite3.OperationalError: no such column: content_hash
      File "app/db.py", line 258, in connect
        con.executescript(SCHEMA)

`SCHEMA` carries `CREATE INDEX … ON documents(uid, content_hash)` and ran
BEFORE the `ALTER TABLE … ADD COLUMN content_hash`. On a fresh database
the CREATE TABLE supplies the column and the order never matters; on a
real one it is fatal, and it takes the whole process rather than the one
collection.

So these tests build a manifest the OLD way — the shape of the columns a
pre-migration node actually has — and open it with the current code.
"""
import sqlite3

import pytest

from app import db

#: `documents` as it stood before the migration branch: no
#: `entity_version`, no `content_hash`. Only the columns `db.connect`'s
#: ALTERs are supposed to add are missing; everything else is enough of
#: the table for SQLite to accept the index.
OLD_DOCUMENTS = """
CREATE TABLE documents (
    uid TEXT PRIMARY KEY,
    source TEXT,
    native_id TEXT,
    path TEXT,
    txt_name TEXT,
    lang TEXT,
    mtime INTEGER,
    size INTEGER,
    in_scope INTEGER DEFAULT 1,
    rdf_done INTEGER DEFAULT 0,
    fts_done INTEGER DEFAULT 0,
    embed_done INTEGER DEFAULT 0,
    retries INTEGER DEFAULT 0,
    error TEXT,
    updated_at INTEGER
);
"""


class _Collection:
    """The two attributes `db.connect` needs, and nothing else."""

    def __init__(self, path):
        self.name = "old"
        self.manifest_path = path
        self.fts_dir = path.parent
        self.lance_dir = path.parent


@pytest.fixture
def old_manifest(tmp_path):
    p = tmp_path / "manifest.db"
    con = sqlite3.connect(p)
    con.executescript(OLD_DOCUMENTS)
    con.execute("INSERT INTO documents(uid, source, native_id, lang) "
                "VALUES('gutenberg:1', 'gutenberg', '1', 'en')")
    con.commit()
    con.close()
    return p


def test_a_pre_migration_manifest_opens(old_manifest):
    """The regression, in one line. Before the fix this raised
    `no such column: content_hash` from `executescript(SCHEMA)`."""
    con = db.connect(_Collection(old_manifest))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        assert "content_hash" in cols
        assert "entity_version" in cols
    finally:
        con.close()


def test_the_index_that_needed_the_column_exists(old_manifest):
    """`idx_documents_content_hash` is the one index in SCHEMA that depends
    on an ALTER-added column, and it is what made the ordering fatal rather
    than merely wrong."""
    con = db.connect(_Collection(old_manifest))
    try:
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_documents_content_hash" in names
    finally:
        con.close()


def test_the_existing_rows_survive_and_the_new_column_is_null(old_manifest):
    """A migration that dropped rows would be worse than one that crashed.
    `content_hash` is NULL on every pre-existing row until its shard is
    rebuilt, which is what the code says and what `records build` fills."""
    con = db.connect(_Collection(old_manifest))
    try:
        row = con.execute("SELECT uid, content_hash FROM documents").fetchone()
        assert row[0] == "gutenberg:1"
        assert row[1] is None
    finally:
        con.close()


def test_opening_twice_is_idempotent(old_manifest):
    """The ALTERs raise "duplicate column" the second time and are caught;
    a `connect` that only worked once would break every process that opens
    the manifest after the first."""
    for _ in range(3):
        con = db.connect(_Collection(old_manifest))
        con.close()


def test_a_fresh_manifest_still_opens(tmp_path):
    """The other half: on a database with no tables at all the ALTERs raise
    "no such table" and are caught, and SCHEMA creates everything."""
    con = db.connect(_Collection(tmp_path / "fresh.db"))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(documents)")}
        assert {"content_hash", "entity_version"} <= cols
    finally:
        con.close()


def test_every_altered_column_is_also_declared_in_SCHEMA():
    """The invariant that keeps the ordering safe.

    `connect` runs the column ALTERs BEFORE `executescript(SCHEMA)`, so an
    ALTER can no longer create a column on a fresh database — the table is
    not there yet. Every column an ALTER adds must therefore also be in
    SCHEMA's own `CREATE TABLE`, and the ALTER is a repair for OLD
    databases only. `entity_version` was not, and moving the ALTERs
    silently dropped it from every new manifest until this test was
    written.
    """
    import inspect
    import re
    altered = set(re.findall(r"ALTER TABLE documents ADD COLUMN (\w+)",
                             inspect.getsource(db.connect)))
    assert altered, "no ALTERs found — did connect() change shape?"
    # Comments first: `content_hash`'s own explains `origin_hash` in a
    # parenthesis and so contains a literal `);` — slicing on that without
    # stripping cuts the table in half and the test lies about what is
    # declared.
    sql = "\n".join(re.sub(r"--.*$", "", line)
                    for line in db.SCHEMA.splitlines())
    body = sql[sql.index("CREATE TABLE IF NOT EXISTS documents"):]
    body = body[:body.index(");")]
    for column in sorted(altered):
        assert re.search(rf"^\s*{column}\b", body, re.M), \
            f"{column} is added by ALTER but not declared in SCHEMA"
