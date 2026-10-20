"""SQLite-backed reference store. One global file (config.refs_db_path()),
independent of any collection: HGNC/ClinVar-mini etc. are standalone lookup
layers a query plan consults regardless of which corpus collection is being
searched (architecture-v2.md §4, §9).
"""
import sqlite3
import time
from pathlib import Path

from .. import config
from ..plugins.base import RefLink, RefSchema, RefSyncState, ReferenceSource


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.refs_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute(
        "CREATE TABLE IF NOT EXISTS ref_meta "
        "(source_id TEXT PRIMARY KEY, ref_version INTEGER, synced_at INTEGER)")
    # the input's own mtime at the last sync, for a source that knows its input
    # (`input_mtime`): compared exactly, where `synced_at` is a truncated clock
    # and an input written in the same second as the sync would never re-sync
    if "input_mtime" not in {r[1] for r in con.execute("PRAGMA table_info('ref_meta')")}:
        con.execute("ALTER TABLE ref_meta ADD COLUMN input_mtime REAL")
    return con


class RefSchemaError(RuntimeError):
    """A table in refs.db has another shape than its source declares, and
    holds rows: nothing is dropped on anyone's behalf."""


def ensure_schema(con: sqlite3.Connection, schema: RefSchema) -> None:
    """Every declared table exists in its declared shape. A table already
    there in ANOTHER shape (a source whose schema changed) is re-created
    when it is empty — `get_store()` creates every registered table at the
    first open, so a shape that moved before any sync leaves an empty table
    behind — and refused when it holds rows, by name, for the operator."""
    for table in schema.tables:
        col_defs = ", ".join(
            f"{c.name} {c.type}" + (" PRIMARY KEY" if c.name == table.primary_key else "")
            for c in table.columns)
        have = [r[1] for r in con.execute(f"PRAGMA table_info('{table.name}')")]
        want = [c.name for c in table.columns]
        if have and have != want:
            n = int(con.execute(f"SELECT count(*) FROM {table.name}").fetchone()[0])
            if n:
                raise RefSchemaError(
                    f"{schema.source_id}: table {table.name} has columns {have} and the source "
                    f"declares {want}; it holds {n} row(s), so it is not re-created here — "
                    f"drop it by hand when its rows are not wanted, then sync again")
            con.execute(f"DROP TABLE {table.name}")
        con.execute(f"CREATE TABLE IF NOT EXISTS {table.name} ({col_defs})")
    # Link columns are traversed on every entity expansion; at fixture scale a
    # scan was free, at real-dump scale (ClinVar's ~3M variants) it is a full
    # table scan per query. Index both ends of every declared link.
    for link in schema.links:
        con.execute(f"CREATE INDEX IF NOT EXISTS idx_{link.from_table}_{link.from_column} "
                    f"ON {link.from_table}({link.from_column})")


def sync_source(con: sqlite3.Connection, source: ReferenceSource, state: RefSyncState) -> dict:
    """Idempotent by primary key (INSERT OR REPLACE, invariant 2); short-
    circuits if ref_version already matches what's pinned AND no file under
    ref_root is newer than the last sync — a refreshed dump (refs/acquire.py)
    reprocesses without a version bump, mirroring the mtime/size gate the
    corpus pipeline uses for discovery."""
    schema = source.schema()
    ensure_schema(con, schema)
    row = con.execute(
        "SELECT ref_version, synced_at, input_mtime FROM ref_meta WHERE source_id=?",
        (source.source_id,)).fetchone()
    input_mtime = getattr(source, "input_mtime", None)
    # a source that knows its own input (a pack, a corpus) says when it was
    # last written (app/refs/wikidata.py); read BEFORE the sync, so an input
    # rewritten while the sync runs reads as newer next time
    seen = float(input_mtime(state)) if input_mtime is not None else None
    if row is not None and row["ref_version"] == source.ref_version:
        if seen is not None:
            if row["input_mtime"] is not None and seen <= float(row["input_mtime"]):
                return {"skipped": True, "rows": 0}
        else:
            # only the TSV dumps sources read — never refs.db/-wal, which may
            # share the directory and are always newer than their own sync
            newest = max((f.stat().st_mtime for f in state.ref_root.glob("*.tsv")),
                         default=0)
            # int() both sides: synced_at is stored truncated, so a float mtime
            # from the same second would otherwise always look "newer"
            if int(newest) <= (row["synced_at"] or 0):
                return {"skipped": True, "rows": 0}

    valid_tables = {t.name for t in schema.tables}
    n = 0
    # ONE TRANSACTION PER BATCH, not per sync: every row is an upsert by the
    # table's primary key, so a sync cut off leaves rows a later sync writes
    # again identically, and ref_meta — written last, in its own transaction
    # — says the sync completed only when it did. One transaction over the
    # whole sync held a WAL of the whole table (10⁸ rows for wikidata) and
    # lost it all on a cut.
    for batch in source.sync(state):
        if batch.table not in valid_tables:
            raise ValueError(f"{source.source_id}: unknown table {batch.table!r}")
        if not batch.rows:
            continue
        # Project to the DECLARED columns: real dumps carry dozens of
        # extra fields (HGNC's complete set has ~50), and a source's
        # sync() may hand rows through unfiltered. Extra keys are
        # ignored; a MISSING declared column is still an error.
        table_schema = next(t for t in schema.tables if t.name == batch.table)
        cols = [c.name for c in table_schema.columns]
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        values = [[r[c] for c in cols] for r in batch.rows]
        con.execute("BEGIN")
        try:
            con.executemany(
                f"INSERT OR REPLACE INTO {batch.table}({col_list}) VALUES({placeholders})", values)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        n += len(values)
    con.execute(
        "INSERT INTO ref_meta(source_id,ref_version,synced_at,input_mtime) VALUES(?,?,?,?) "
        "ON CONFLICT(source_id) DO UPDATE SET ref_version=excluded.ref_version,"
        "synced_at=excluded.synced_at, input_mtime=excluded.input_mtime",
        (source.source_id, source.ref_version, int(time.time()), seen))
    return {"skipped": False, "rows": n}


def sync_all(con: sqlite3.Connection, ref_root: Path) -> dict[str, dict]:
    from . import REFS_REGISTRY
    state = RefSyncState(ref_root=ref_root)
    return {sid: sync_source(con, src, state) for sid, src in REFS_REGISTRY.items()}


class RefStore:
    """Read interface for EntityResolver — implements plugins.base.RefLookup."""

    def __init__(self, con: sqlite3.Connection, schemas: list[RefSchema]):
        self.con = con
        self._links: list[RefLink] = [l for s in schemas for l in s.links]
        self._pk: dict[str, str] = {t.name: t.primary_key for s in schemas for t in s.tables}
        self._owner: dict[str, str] = {t.name: s.source_id for s in schemas for t in s.tables}

    def rows(self, table: str, where: dict | None = None, *,
             order_by: str | None = None, limit: int | None = None) -> list[dict]:
        """`order_by` is SQL the caller composes from its own constants, never
        from a query's text; `limit` bounds what is materialised — the point
        of both (resolver.expand_bounded)."""
        sql, params = f"SELECT * FROM {table}", []
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
            params = list(where.values())
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def count(self, table: str, where: dict | None = None) -> int:
        sql, params = f"SELECT count(*) FROM {table}", []
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
            params = list(where.values())
        return int(self.con.execute(sql, params).fetchone()[0])

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self.con.execute(f"PRAGMA table_info('{table}')")}

    def get(self, table: str, pk_value) -> dict | None:
        row = self.con.execute(
            f"SELECT * FROM {table} WHERE {self.pk_of(table)}=?", (pk_value,)).fetchone()
        return dict(row) if row else None

    def links_for(self, table: str) -> list[RefLink]:
        return [l for l in self._links if l.from_table == table or l.to_table == table]

    def pk_of(self, table: str) -> str:
        return self._pk[table]

    def tables(self) -> list[str]:
        """Every table the store knows, in declaration order."""
        return list(self._pk)

    def owner_of(self, table: str) -> str:
        return self._owner[table]
