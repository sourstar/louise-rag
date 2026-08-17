"""SQLite manifest — the single source of truth for pipeline state.

Both the web process and the worker open this in WAL mode; per-document
commits make every stage resumable after a kill at any point. Each collection
gets its own manifest.db (see app/collections.py); connect() defaults to the
single default collection when none is given.
"""
import json
import sqlite3
import time

from . import collections as collections_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
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
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_native ON documents(source, native_id);
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(in_scope, rdf_done, fts_done, embed_done);
CREATE INDEX IF NOT EXISTS idx_documents_lang ON documents(lang);

CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
  uid UNINDEXED, title, authors, subjects, bookshelves
);

CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS embed_queue (
  uid TEXT PRIMARY KEY,
  priority INTEGER DEFAULT 0,
  ts INTEGER
);

CREATE TABLE IF NOT EXISTS summaries (
  uid TEXT PRIMARY KEY,
  model TEXT,
  created_at INTEGER,
  summary TEXT,
  chapter_summaries TEXT
);

CREATE TABLE IF NOT EXISTS query_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  endpoint TEXT,
  query TEXT,
  mode TEXT,
  k INTEGER,
  t_catalog REAL, t_fts REAL, t_dense REAL, t_rerank REAL, t_llm REAL,
  total_ms REAL,
  n_results INTEGER,
  books TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  level TEXT,
  stage TEXT,
  doc_uid TEXT,
  message TEXT
);

CREATE TABLE IF NOT EXISTS space (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  space_id TEXT,
  dim INTEGER,
  components TEXT,
  pinned_at INTEGER
);

CREATE TABLE IF NOT EXISTS doc_keys (
  uid TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  PRIMARY KEY (uid, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_doc_keys_entity ON doc_keys(entity_id);

-- Imported vector bundles (architecture-v2.md §3 names `bundles` and
-- `providers` as manifest tables; §16.1 lists the bundles ledger under
-- SQLite's role). This is node-local truth and NEVER travels: P1.3's
-- coordinator keeps its own separate ledger in its own service DB.
--
-- `uids` holds a JSON map of uid -> chunks actually written. Without it,
-- quarantining a producer cannot answer "which rows do I delete?", and §6's
-- "proof of forgery" is an accusation rather than a proof — which is also why
-- the signature and both digests are retained.
CREATE TABLE IF NOT EXISTS bundles (
  bundle_id TEXT PRIMARY KEY,
  producer_pubkey TEXT NOT NULL,
  space_id TEXT,
  collection TEXT,
  state TEXT NOT NULL,              -- applying | applied | rejected | aborted
  uids TEXT,                        -- JSON {uid: chunk_count}
  chunk_count INTEGER DEFAULT 0,
  signature TEXT,                   -- 128 hex chars; evidence, not a secret
  parquet_sha256 TEXT,
  content_digest TEXT,
  space_weak INTEGER DEFAULT 0,     -- local model hash unconfirmed at import time
  tier_reached INTEGER,             -- highest ladder tier that RAN and passed;
                                    -- NULL = only coarse checks (no artifact)
  verdict TEXT,                     -- admission-check evidence summary
  error TEXT,
  created_at INTEGER,               -- the producer's claim
  imported_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bundles_producer ON bundles(producer_pubkey);
CREATE INDEX IF NOT EXISTS idx_bundles_state ON bundles(state);

CREATE TABLE IF NOT EXISTS providers (
  pubkey TEXT PRIMARY KEY,
  trusted INTEGER DEFAULT 0,
  quarantined INTEGER DEFAULT 0,
  quarantine_reason TEXT,
  bundles_accepted INTEGER DEFAULT 0,
  bundles_rejected INTEGER DEFAULT 0,
  note TEXT,
  first_seen INTEGER,
  last_seen INTEGER
);

-- Genesis imports (architecture-v2.md §8, amendment 10): published vector
-- archives admitted shard by shard. One row per (archive, shard) is the
-- resume cursor AND the provenance record — quarantine and re-audit work on
-- genesis imports exactly as on bundles.
CREATE TABLE IF NOT EXISTS genesis_imports (
  archive TEXT NOT NULL,            -- archive identifier (e.g. medcpt-pubmed)
  shard TEXT NOT NULL,              -- shard name within the archive
  source_url TEXT,
  content_digest TEXT,              -- sha256 of the shard bytes as fetched
  state TEXT NOT NULL,              -- applying | applied | rejected | skipped
  docs INTEGER DEFAULT 0,
  chunks INTEGER DEFAULT 0,
  detail TEXT,
  updated_at INTEGER,
  PRIMARY KEY (archive, shard)
);

-- Full verification evidence, one blob per bundle, separate from the bundles
-- row deliberately: the ladder's evidence includes per-chunk claimed and
-- recomputed vectors — §6's "proof of forgery" — and truncating it into a
-- summary column would amputate exactly the parts a third party needs to
-- recompute the verdict. Local truth, never travels.
CREATE TABLE IF NOT EXISTS bundle_evidence (
  bundle_id TEXT PRIMARY KEY,
  evidence TEXT NOT NULL,
  created_at INTEGER
);

-- Per-chunk verification state for imported vectors (architecture-v2.md §6
-- T4, "verify-on-use"). ABSENCE MEANS LOCALLY EMBEDDED: rows exist only for
-- imported chunks, so this table stays proportional to imported content
-- rather than to the whole corpus.
CREATE TABLE IF NOT EXISTS chunk_verification (
  chunk_id TEXT PRIMARY KEY,
  uid TEXT,
  state TEXT NOT NULL,              -- imported | verified | failed
  producer TEXT,
  bundle_id TEXT,
  cosine REAL,
  checked_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunk_verification_state ON chunk_verification(state);
CREATE INDEX IF NOT EXISTS idx_chunk_verification_uid ON chunk_verification(uid);

CREATE TABLE IF NOT EXISTS fetch_files (
  source_id TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  endpoint_url TEXT,
  remote_size INTEGER,
  remote_mtime INTEGER,
  local_path TEXT,
  bytes_fetched INTEGER DEFAULT 0,
  checksum_declared TEXT,
  checksum_algo TEXT,
  blake2 TEXT,
  fetch_done INTEGER DEFAULT 0,
  fetch_version INTEGER DEFAULT 0,
  profile TEXT,
  error TEXT,
  retries INTEGER DEFAULT 0,
  updated_at INTEGER,
  PRIMARY KEY (source_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_fetch_files_pending ON fetch_files(source_id, fetch_done);
"""

def connect(collection=None) -> sqlite3.Connection:
    if collection is None:
        collection = collections_mod.default_collection()
    collection.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    collection.fts_dir.mkdir(parents=True, exist_ok=True)
    collection.lance_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(collection.manifest_path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE documents ADD COLUMN entity_version INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists — no reusable "add column" migration tool for this
    try:
        con.execute("ALTER TABLE bundles ADD COLUMN tier_reached INTEGER")
    except sqlite3.OperationalError:
        pass  # same defensive pattern: pre-hardening manifests lack the column
    return con

# ---------------- control ----------------

DEFAULT_SCOPE = {
    "languages": [],        # [] = all languages
    "book_ids": [],         # explicit allow-list, [] = no restriction
    "limit": 0,             # 0 = unlimited; else cap total in-scope books
    "sample_per_lang": 0,   # 0 = off; else pick N lowest ids per language
    "embed_mode": "lazy",   # off | lazy | all
}

def get_control(con, key, default=None):
    row = con.execute("SELECT value FROM control WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_control(con, key, value):
    con.execute(
        "INSERT INTO control(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def get_scope(con) -> dict:
    raw = get_control(con, "scope")
    scope = dict(DEFAULT_SCOPE)
    if raw:
        try:
            scope.update(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return scope

def set_scope(con, scope: dict):
    merged = dict(DEFAULT_SCOPE)
    merged.update({k: v for k, v in scope.items() if k in DEFAULT_SCOPE})
    set_control(con, "scope", json.dumps(merged))
    return merged

def node_id(con) -> str:
    """A stable, unique identity for this node, generated once and persisted.

    F1-D2: the coordinator's demand feed counts DISTINCT reporters precisely so
    one noisy consumer cannot dominate work ordering — and before this
    existed, contribute.report_demand defaulted the reporter id to the
    COLLECTION NAME, so every node in the network reported as "gutenberg" and
    N consumers silently collapsed into one. Random rather than derived from
    hostname/paths: those leak deployment topology onto a shared service.
    """
    existing = get_control(con, "node_id")
    if existing:
        return existing
    import secrets
    fresh = f"node-{secrets.token_hex(8)}"
    set_control(con, "node_id", fresh)
    return fresh

BANDWIDTH_OVERRIDE_KEY = "bandwidth_override"

def set_bandwidth_override(con, rate_bps: float | None, ttl_seconds: int = 12 * 3600) -> None:
    """architecture-v2.md §14's "boost bandwidth tonight" — a Tier-3 runtime
    verb. It goes through the control table because the process that SETS it
    (web) is not the process that MOVES bytes (worker); an in-memory override
    could never reach the worker at all.

    It carries an explicit expiry so it stays genuinely ephemeral: §14 says
    "on restart the files win", and an override with no expiry would quietly
    become permanent desired state, which CLAUDE.md invariant 10 forbids.
    Passing None clears it.
    """
    if rate_bps is None:
        set_control(con, BANDWIDTH_OVERRIDE_KEY, "")
        return
    set_control(con, BANDWIDTH_OVERRIDE_KEY,
                json.dumps({"rate_bps": float(rate_bps),
                            "until": int(time.time()) + int(ttl_seconds)}))

def get_bandwidth_override(con) -> float | None:
    """None when unset or expired. Never raises on a malformed value — a bad
    override must not be able to stop the worker from moving bytes."""
    raw = get_control(con, BANDWIDTH_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        if int(payload.get("until", 0)) < int(time.time()):
            return None
        return float(payload["rate_bps"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

def apply_reset(con, stage: str, book_ids: list[int]) -> None:
    """Shared by app/api.py's /api/reset (default collection) and
    app/web.py's per-collection Library admin action — same SQL either way,
    just a different `con` (each collection's manifest.db has its own
    documents table under the same schema)."""
    where, params = "in_scope=1", []
    if book_ids:
        where += " AND CAST(native_id AS INTEGER) IN (%s)" % ",".join("?" * len(book_ids))
        params = list(book_ids)
    if stage == "errors":
        con.execute(f"UPDATE documents SET error=NULL, retries=0 WHERE {where}", params)
    elif stage in ("rdf", "fts", "embed"):
        con.execute(f"UPDATE documents SET {stage}_done=0 WHERE {where}", params)
        if stage == "rdf":
            con.execute(f"UPDATE documents SET fts_done=0, embed_done=0 WHERE {where}", params)
    else:
        raise ValueError(f"unknown stage: {stage}")

def worker_state(con) -> str:
    return get_control(con, "worker_state", "paused")

def set_worker_state(con, state: str):
    assert state in ("running", "paused")
    set_control(con, "worker_state", state)

# ---------------- events ----------------

def log_event(con, level, stage, message, doc_uid=None):
    stamp = time.strftime("%H:%M:%S")
    tag = f" #{doc_uid}" if doc_uid else ""
    print(f"{stamp} [{level}:{stage}{tag}] {message}", flush=True)
    con.execute(
        "INSERT INTO events(ts,level,stage,doc_uid,message) VALUES(?,?,?,?,?)",
        (int(time.time()), level, stage, doc_uid, message[:2000]))
    # keep the table bounded
    con.execute(
        "DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0) FROM events) - 5000")

def log_query(con, endpoint, query, mode, k, timings, n_results, books):
    con.execute(
        "INSERT INTO query_log(ts,endpoint,query,mode,k,t_catalog,t_fts,t_dense,"
        "t_rerank,t_llm,total_ms,n_results,books) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), endpoint, query[:500], mode, k,
         timings.get("catalog", 0), timings.get("fts", 0), timings.get("dense", 0),
         timings.get("rerank", 0), timings.get("llm", 0), timings.get("total", 0),
         n_results, json.dumps(books[:20])))
