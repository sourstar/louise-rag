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
  -- 09 §9.5's `content_hash`: BLAKE2b-128 of the canonical JSON of
  -- [title, sections] — the cleaned text as the chunker sees it. NOT
  -- `txt_hash`, which is the ORIGIN bytes' hash (§9.5 calls that
  -- `origin_hash`); both are 32 hex characters, so the two are
  -- indistinguishable by inspection and a peer that sent one where the
  -- other was meant would refuse every record in silence.
  --
  -- Stored, and indexed, because `possession.json` (§9.7) binds a
  -- bundle's uids to this value: verifying an arriving bundle is then
  -- one indexed SELECT over its uids. Deriving it instead would mean
  -- re-reading and re-hashing the shards each import — a per-bundle cost
  -- that scales with the CORPUS, which is the engineering rule this
  -- project keeps paying for.
  content_hash TEXT,
  -- What WORK this document is an edition of, as JSON:
  -- {"author": <agent id>, "subjects": [...], "translated": bool}.
  -- What retrieval needs to tell one work from its translations
  -- (app/work_dedup.py). Derived by the plugin's parser, NULL for a
  -- plugin that does not say — and a NULL never groups, so it never
  -- suppresses anything. Local state: in no record, moving no hash.
  work TEXT,
  in_scope INTEGER DEFAULT 1,
  rdf_done INTEGER DEFAULT 0,
  fts_done INTEGER DEFAULT 0,
  fts_version INTEGER DEFAULT 0,
  embed_done INTEGER DEFAULT 0,
  embed_version INTEGER DEFAULT 0,
  summary_done INTEGER DEFAULT 0,
  chunks INTEGER DEFAULT 0,
  tokens INTEGER DEFAULT 0,
  -- Added here 2026-09-10. It arrived only through the ALTER below, which
  -- meant a FRESH database got the table without it and an OLD one got it
  -- afterwards — two shapes for one schema, and the difference was
  -- invisible because both ended up with the column. It stopped being
  -- invisible when the ALTERs had to move ahead of this script (see
  -- `connect`): an ALTER that runs before `CREATE TABLE` cannot add a
  -- column to a table that does not exist yet. Declared here, every ALTER
  -- below is now an OLD-database repair and nothing else.
  entity_version INTEGER DEFAULT 0,
  error TEXT,
  retries INTEGER DEFAULT 0,
  updated_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_native ON documents(source, native_id);
CREATE INDEX IF NOT EXISTS idx_documents_scope ON documents(in_scope, rdf_done, fts_done, embed_done);
CREATE INDEX IF NOT EXISTS idx_documents_lang ON documents(lang);
-- The import path looks these up BY UID for a bundle's uid set, so the
-- index is on the pair and covers the lookup without touching the row.
CREATE INDEX IF NOT EXISTS idx_documents_content_hash
  ON documents(uid, content_hash);

CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS embed_queue (
  uid TEXT PRIMARY KEY,
  priority INTEGER DEFAULT 0,
  ts INTEGER
);

-- 05 §5.3 / 03 §3.5 T4: the imported chunks a search cited, for the worker
-- to re-embed once (app/bundles/verify_on_use.py); proportional to what is
-- cited and still unverified, emptied as the worker drains it.
CREATE TABLE IF NOT EXISTS verify_queue (
  chunk_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL
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
-- One record per Expression (app/validity.py; 05 §5.1 amended 2026-10-12):
-- the version table of a collection whose Codex profile declares a
-- validity axis. `valid_to` is DERIVED — the next version's start — and
-- lives here and in the index columns, never in a record (02 §2.3);
-- `shown` marks the Work's latest version that has text; `synced` = 0 marks
-- a row whose index columns may lag what is here.
CREATE TABLE IF NOT EXISTS validity (
  uid TEXT PRIMARY KEY,
  native_id INTEGER NOT NULL,
  work TEXT NOT NULL,
  valid_from INTEGER NOT NULL,
  valid_to INTEGER,
  has_text INTEGER NOT NULL DEFAULT 0,
  in_force INTEGER,
  shown INTEGER NOT NULL DEFAULT 0,
  synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_validity_work ON validity(work, valid_from, native_id);
CREATE INDEX IF NOT EXISTS idx_validity_native ON validity(native_id);
-- what ends a Work's validity from OUTSIDE its chain (app/validity.py; the
-- review's §3.2 case 2): the version `by_uid` states that the Work keyed
-- `key` (one of its canonical ids) is no longer in force from `closed_at`.
-- Node-local and derived, like `validity`; rewritten per stating uid.
CREATE TABLE IF NOT EXISTS validity_closures (
  key TEXT NOT NULL,
  by_uid TEXT NOT NULL,
  closed_at INTEGER NOT NULL,
  PRIMARY KEY (key, by_uid)
);
CREATE INDEX IF NOT EXISTS idx_validity_closures_by ON validity_closures(by_uid);
-- a Work's canonical ids, so a closure finds the chain it ends
CREATE TABLE IF NOT EXISTS validity_works (
  key TEXT NOT NULL,
  work TEXT NOT NULL,
  PRIMARY KEY (key, work)
);
CREATE INDEX IF NOT EXISTS idx_validity_works_work ON validity_works(work);
CREATE INDEX IF NOT EXISTS idx_validity_unsynced ON validity(synced, native_id);
-- 06 §6.3 (amended 2026-10-12): the hash of every chunk text this peer holds
-- a vector for, so a versioned collection's new version carries the vectors
-- of the articles it shares with another version instead of embedding them
-- again (app/embeddings.py cross_carry). Node-local; one holder per hash.
CREATE TABLE IF NOT EXISTS chunk_hashes (
  text_hash BLOB PRIMARY KEY,
  chunk_id TEXT NOT NULL,
  native_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_hashes_native ON chunk_hashes(native_id);
"""

class ManifestConnection(sqlite3.Connection):
    """A manifest connection that knows WHICH collection it belongs to, so
    every log line written through it can say so. Seven collections share
    one worker stdout; `[info:fts] indexed 200 documents` could be any of
    them, which is useless exactly when one of them is the problem. A
    plain sqlite3.Connection accepts no attributes, hence the subclass."""
    collection_name: str = ""


def wal(con, *, tries: int = 50, pause_s: float = 0.1) -> str:
    """`PRAGMA journal_mode=WAL`, waiting out a lock (invariant 6).

    Converting a FRESH database to WAL needs the file to itself for an
    instant, and the busy handler does not cover that step: two connections
    opening one new file at once — the web and the deposit thread on
    node.sqlite, two threads on a deposit manifest — made the loser raise
    "database is locked" at once (2026-10-05 on a deposit manifest,
    2026-10-07 on node.sqlite in the full suite). Retried for about five
    seconds; any other error, or a lock that never lifts, is raised."""
    last: sqlite3.OperationalError | None = None
    for i in range(max(1, tries)):
        try:
            return con.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            last = e
            if i + 1 < tries:
                time.sleep(pause_s)
    assert last is not None
    raise last


def connect(collection=None) -> sqlite3.Connection:
    if collection is None:
        collection = collections_mod.default_collection()
    collection.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    collection.fts_dir.mkdir(parents=True, exist_ok=True)
    collection.lance_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(collection.manifest_path, timeout=30, isolation_level=None,
                          factory=ManifestConnection)
    con.collection_name = collection.name
    con.row_factory = sqlite3.Row
    wal(con)
    con.execute("PRAGMA synchronous=NORMAL")
    # 60s, not 15: per-collection pipeline threads mean several writers now
    # contend for one manifest, and a discovery pass streaming millions of
    # rows can starve a short writer well past 15s. Observed live as
    # "corpus fetch failed: database is locked" on the acquire path — a
    # transient loss dressed up as a failure. The PRAGMA governs; the
    # connect(timeout=) argument above is its Python-side twin.
    con.execute("PRAGMA busy_timeout=60000")
    # THE COLUMNS COME BEFORE THE SCHEMA SCRIPT, and the order is the whole
    # point. `SCHEMA` carries `CREATE INDEX … ON documents(uid,
    # content_hash)`, and on a manifest that predates the column
    # `CREATE TABLE IF NOT EXISTS` is a no-op — so the index refers to a
    # column that is not there yet and `executescript` dies with
    # "no such column: content_hash", taking the whole worker with it.
    #
    # Observed live 2026-09-10 on the reference node's 79,320-row manifest:
    # the app crash-looped on first boot after the migration branch was
    # deployed. It cannot happen in the tests, because every test builds a
    # fresh manifest where `CREATE TABLE` makes the column and these ALTERs
    # are all no-ops — the bug needs an OLD database to appear at all.
    #
    # On a fresh database each ALTER raises "no such table" and is caught
    # here, exactly as it is caught for "duplicate column" on an old one.
    for statement in (
            "ALTER TABLE documents ADD COLUMN entity_version INTEGER DEFAULT 0",
            # NULL on every pre-existing row until its shard is rebuilt
            "ALTER TABLE documents ADD COLUMN content_hash TEXT",
            # WHAT WORK THIS DOCUMENT IS AN EDITION OF, as JSON:
            # {"author": <agent id>, "subjects": [...], "translated": bool}.
            # What retrieval needs to tell "Madame Bovary" from its
            # fourteen translations (app/work_dedup.py) — the author AGENT
            # id, not the name, because a translation's display authors
            # carry the translator too. Derived by the plugin's parser, so
            # a re-scan with `rdf_done=0` fills it; NULL until then, and a
            # NULL never groups and so never suppresses anything.
            #
            # Local manifest state (invariant 6): it never travels, it is
            # not in any record, and it moves no hash. Adding it is an
            # ALTER plus an rdf re-derive, not a rebuild.
            "ALTER TABLE documents ADD COLUMN work TEXT"):
        try:
            con.execute(statement)
        except sqlite3.OperationalError:
            pass  # already there, or no table yet — see above

    con.executescript(SCHEMA)
    try:
        con.execute("ALTER TABLE bundles ADD COLUMN tier_reached INTEGER")
    except sqlite3.OperationalError:
        pass  # same defensive pattern: pre-hardening manifests lack the column
    try:
        # opted-in EXTRA formats (spec.extras): local-only files counted
        # apart from Louise data everywhere they appear
        con.execute("ALTER TABLE fetch_files ADD COLUMN extra INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # amendment 38: remote archive shards are fetched ahead, revisited and
    # finalised — the ledger row carries the shard's id range (coverage),
    # its size on disk, how many held documents still wait for fts, and
    # the per-shard backoff for revisit passes that imported nothing
    # `proven_at`: when the shard's same-function proof passed — the proof
    # is per archive and persists across passes (genesis.RE_ENCODE_REJECT_MIN)
    for column, decl in (("id_min", "INTEGER"), ("id_max", "INTEGER"),
                         ("bytes", "INTEGER"), ("pending", "INTEGER"),
                         ("next_pass_at", "INTEGER"), ("proven_at", "INTEGER")):
        try:
            con.execute(f"ALTER TABLE genesis_imports ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass
    return con

def ensure_browse_index(con: sqlite3.Connection) -> bool:
    """Expression index behind every `ORDER BY CAST(native_id AS INTEGER)` —
    the browse listing (/api/books), the CLI listing and the bundle-export
    default all order by it, and without an index SQLite builds a TEMP
    B-TREE over the whole filtered set to return one page: ~10s PER PAGE
    VIEW on a 33M-row manifest.

    Deliberately NOT part of SCHEMA: connect() runs from the web process
    too, and the first build on a large manifest reads the whole table —
    minutes, not milliseconds. Only the worker calls this (once per
    collection at startup), so a web request never pays the build; until
    the worker's first boot after upgrade, browsing is simply as slow as it
    always was. Returns True when it actually built."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                   "AND name='idx_documents_native_int'").fetchone():
        return False
    log_event(con, "info", "worker",
              "building browse index — one-time, may take minutes on a "
              "large collection")
    con.execute("CREATE INDEX idx_documents_native_int "
                "ON documents(CAST(native_id AS INTEGER))")
    log_event(con, "info", "worker", "browse index built")
    return True


def drop_catalog_fts(con: sqlite3.Connection) -> bool:
    """One-time: the SQLite FTS5 catalog goes. The catalog is the tantivy
    index of app/catalog.py since 2026-09-19. FTS5 ranked every match (57.3 s
    for "cancer" on pubmed's 40M records), and the table held 15.7 GiB there
    and 1.06 GiB in wikipedia's manifest. The index is built from
    `documents`, never from this table, so nothing is lost.

    The worker's startup, like ensure_browse_index: one transaction that
    holds the manifest's write lock while SQLite frees the table's pages,
    which go to the free list for the manifest to reuse. The file does not
    shrink, because nothing VACUUMs a manifest (invariant 6). A crash rolls
    it back, and the next boot drops it again."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_fts'").fetchone():
        return False
    log_event(con, "info", "worker",
              "dropping the SQLite catalog (one-time; the catalog index replaced it)")
    t0 = time.monotonic()
    # This SQLite is built with SECURE_DELETE on: every freed page would be
    # zeroed THROUGH THE WAL. On a clone of pubmed's manifest that was a
    # 14.6 GB WAL still growing after 12 minutes, with a checkpoint to
    # rewrite all of it after. The catalog is public metadata that
    # `documents` also holds, so its freed pages keep their bytes until
    # reused.
    secure = con.execute("PRAGMA secure_delete").fetchone()[0]
    con.execute("PRAGMA secure_delete = OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP TABLE catalog_fts")
        con.execute("DELETE FROM control WHERE key='catalog_fts_rowid_aligned'")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute(f"PRAGMA secure_delete = {int(secure)}")
    log_event(con, "info", "worker",
              f"SQLite catalog dropped in {time.monotonic() - t0:.0f}s")
    return True


# ---------------- control ----------------

DEFAULT_SCOPE = {
    "languages": [],        # [] = all languages
    "book_ids": [],         # explicit allow-list, [] = no restriction
    "limit": 0,             # 0 = unlimited; else cap total in-scope books
    "sample_per_lang": 0,   # 0 = off; else pick N lowest ids per language
    "embed_mode": collections_mod.DEFAULT_EMBED_MODE,   # off | lazy | all
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


def apply_desired_embed_mode(con, collection) -> str | None:
    """Make the collection's Tier-2 `embed_mode` (its TOML, else the default)
    the effective one. Returns the Tier-3 value it replaced, or None when
    nothing changed.

    Invariant 10: TOML = desired state, DB = runtime override that does NOT
    survive restart. The Library's embed-mode control writes the scope in the
    control table; without this, that override was permanent and the TOML
    value was parsed and only ever displayed — Tier 3 acting as desired
    state and Tier 2 decorative. The worker calls this at boot and whenever
    the TOML value changes; the override remains useful between those.
    """
    desired = collection.embed_mode_default or collections_mod.DEFAULT_EMBED_MODE
    scope = get_scope(con)
    current = scope.get("embed_mode")
    if current == desired:
        return None
    scope["embed_mode"] = desired
    set_scope(con, scope)
    return current

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

def read_only_connect(collection=None) -> sqlite3.Connection | None:
    """A cheap reader for polling paths. None when there is no manifest yet.

    connect() is not cheap: it runs the whole schema script and the ALTER
    TABLE migrations on every call, which is right for a process that is
    about to write and wrong for something asked twice a second. This opens
    read-only, runs no DDL, and takes no write lock on a manifest the
    pipeline threads are already contending for.

    check_same_thread=False because the caller is typically a streaming
    response, and consecutive iterations of a sync generator are not
    guaranteed the same worker thread. The connection is read-only and
    short-lived, so there is nothing to serialise.
    """
    if collection is None:
        collection = collections_mod.default_collection()
    if not collection.manifest_path.exists():
        return None
    # as_uri(), not f"file:{path}": a `#` or `?` in the path cut a raw URI
    # short, dropped mode=ro, and opened (creating) a different file
    con = sqlite3.connect(collection.manifest_path.resolve().as_uri() + "?mode=ro",
                          uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


WORKER_RATES_KEY = "worker_rates"
# How long a published sample stays believable. The worker republishes once
# per loop; past this it is reported as stale and counted as zero, because a
# frozen number on a live graph is worse than an honest gap.
WORKER_RATES_MAX_AGE_S = 20.0

def _publish_sample(con, key: str, payload) -> None:
    """Stamp a measurement into the control table for another process to
    read. Telemetry, never configuration — see publish_worker_rates."""
    set_control(con, key, json.dumps({"at": time.time(), "payload": payload}))


def _read_sample(con, key: str, max_age_s: float):
    """(payload, age) or (None, age) when absent, malformed or stale.
    Never raises: a bad reading must not break the page that shows it."""
    raw = get_control(con, key)
    if not raw:
        return None, None
    try:
        wrapper = json.loads(raw)
        age = max(0.0, time.time() - float(wrapper["at"]))
        payload = wrapper["payload"]
    except (ValueError, TypeError, KeyError):
        return None, None
    return (payload, age) if age <= max_age_s else (None, age)


# QUERY PRIORITY (A5) is the node's interactive claim now, in
# state/node.sqlite (app/dispatch.py, docs/notes/scheduling.md §12): one
# claim for every interactive level, cleared when the last call ends, read
# by the worker before each REQUEST rather than between documents.

WORKER_STATS_KEY = "worker_stats"
# Throughput samples age out more slowly than instantaneous rates: a
# collection that embedded steadily and then yielded the encoder to another
# collection has not stopped being measurable, and its figures are still the
# right basis for an ETA.
WORKER_STATS_MAX_AGE_S = 15 * 60.0


def publish_worker_stats(con, payload: dict) -> None:
    """Per-collection fetch/embed throughput from app/stats.py, for the web
    process to render. Same channel and same reasoning as
    publish_worker_rates: the work happens in the worker, the page is served
    by the web process, and a measurement is not desired state."""
    _publish_sample(con, WORKER_STATS_KEY, payload)


def read_worker_stats(con, max_age_s: float = WORKER_STATS_MAX_AGE_S):
    return _read_sample(con, WORKER_STATS_KEY, max_age_s)


def publish_worker_rates(con, payload: dict) -> None:
    """The worker's own per-class byte rates, for the web process to read.

    Bytes move in the WORKER process (acquisition, seeding); the Network page
    is served by the WEB process, and a token bucket is per-process state.
    Without this the graph shows only the web process's own transfers — which
    for a node that is downloading 80 GB is a flat zero line next to a
    saturated link.

    This is TELEMETRY, not configuration: a measurement of what happened,
    overwritten every loop, meaningless after a restart. CLAUDE.md invariant
    10 forbids writing desired STATE into the DB; an observation is not
    state, and it is deliberately not in NODE_CONTROL_KEYS — carrying a
    stale reading to a new owner would only publish a lie for one round.
    """
    set_control(con, WORKER_RATES_KEY,
                json.dumps({"at": time.time(), "rates": payload}))

def read_worker_rates(con, max_age_s: float = WORKER_RATES_MAX_AGE_S
                       ) -> tuple[dict | None, float | None]:
    """(rates, age_seconds), or (None, age) when there is no believable
    sample — no worker has ever published, the value is malformed, or it is
    older than max_age_s. Never raises: a bad reading must not be able to
    break the Network page."""
    raw = get_control(con, WORKER_RATES_KEY)
    if not raw:
        return None, None
    try:
        payload = json.loads(raw)
        age = max(0.0, time.time() - float(payload["at"]))
        rates = payload["rates"]
        if not isinstance(rates, dict):
            return None, age
    except (ValueError, TypeError, KeyError):
        return None, None
    return (rates, age) if age <= max_age_s else (None, age)

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

def apply_reset(con, stage: str, book_ids: list[int]) -> int | None:
    """Shared by app/api.py's /api/reset (default collection) and
    app/web.py's per-collection Library admin action — same SQL either way,
    just a different `con` (each collection's manifest.db has its own
    documents table under the same schema)."""
    where, params = "in_scope=1", []
    if book_ids:
        where += " AND CAST(native_id AS INTEGER) IN (%s)" % ",".join("?" * len(book_ids))
        params = list(book_ids)
    if stage == "errors":
        # STAGE errors only ('rdf: …' / 'fts: …' / 'embed: …' — the
        # retryable class). An UNPREFIXED error is a corpus fact written by
        # discovery ('no txt file': the mirror ships no text for this
        # entry), and clearing it sent the document through the pipeline to
        # fail again — except the retry re-recorded the same fact as
        # 'fts: no txt file', which the Library counts as BLOCKED. Observed
        # live: every press of "clear errors & retry" converted ~1,100
        # unindexable gutenberg entries into scary blocked ones, and
        # discovery could not relabel them back because their directories
        # had not changed (cursor skip). Retrying cannot change a corpus
        # fact; the card for unindexable documents says exactly that.
        prefixed = " OR ".join(f"error LIKE '{st}: %'" for st in ("rdf", "fts", "embed"))
        # ONLY THE ROWS THE RESET CHANGES: a stage error, or retries spent
        # without one. This was `error IS NULL OR …`, which matched every
        # healthy document too and rewrote them all — on pubmed 40M rows in
        # one transaction, minutes under the manifest's write lock: the
        # worker starved behind it (vector-lock timeouts, recorded as
        # errors) and the request could end before the reset did, so the
        # button read as doing nothing (2026-09-15). No index answers this,
        # so it is READ first, under no write lock, and only the rows found
        # are written, a few hundred per transaction.
        cond = f"{where} AND ((error IS NULL AND retries > 0) OR {prefixed})"
        rowids = [r[0] for r in con.execute(
            f"SELECT rowid FROM documents WHERE {cond}", params)]
        for i in range(0, len(rowids), 500):
            part = rowids[i:i + 500]
            con.execute(f"UPDATE documents SET error=NULL, retries=0 "
                        f"WHERE rowid IN ({','.join('?' * len(part))})", part)
        return len(rowids)
    elif stage in ("rdf", "fts", "embed"):
        con.execute(f"UPDATE documents SET {stage}_done=0 WHERE {where}", params)
        if stage == "rdf":
            con.execute(f"UPDATE documents SET fts_done=0, embed_done=0 WHERE {where}", params)
    else:
        raise ValueError(f"unknown stage: {stage}")

WORKER_STATE_KEY = "worker_state"
# Node-level processing modes. "acquire-only" exists because the two
# halves of this system have very different costs: moving bytes is nearly
# free, while rdf/fts/embed run every core hot — enough to heat a room.
# Turning embedding off per collection meant five edits AND losing each
# collection's own embed_mode; this keeps the corpus growing while the
# CPU-heavy stages stand down, and restores the previous state exactly.
WORKER_STATES = ("running", "acquire-only", "seed-only", "paused")

def worker_state(con) -> str:
    state = get_control(con, WORKER_STATE_KEY, "paused")
    return state if state in WORKER_STATES else "paused"

def set_worker_state(con, state: str):
    assert state in WORKER_STATES
    set_control(con, WORKER_STATE_KEY, state)

# Control keys that describe the NODE, not a collection. They live in
# `state/node.sqlite` (app/node_state.py, 07 §7.8); they used to live in
# the control table of whichever collection sorted first — an accident of
# sort order that paused the whole node when that collection was wiped or
# removed, and split the web from the worker when it was refused at boot.
# `scope` (embed_mode) and `embed_gate` are genuinely per-collection and
# stay in their manifests.
NODE_CONTROL_KEYS = (WORKER_STATE_KEY, BANDWIDTH_OVERRIDE_KEY)

# ---------------- events ----------------

#: stages whose lines are about the NODE, not about the collection whose
#: manifest stores them — see `log_event`'s `node` below
NODE_STAGES = frozenset({"sched", "share", "models"})   # share: the seeder and gossip lines cover every collection and name theirs in the message; models: a cold start (app/model_load.py) is the node's


def log_event(con, level, stage, message, doc_uid=None):
    """stdout line: `HH:MM:SS [<collection> level:stage #uid] message`. The
    collection comes from the connection (ManifestConnection), so no caller
    has to pass it and no line can omit it; connections opened some other
    way (tests, tools) simply print without it.

    A NODE_STAGES line says `node` instead. The fair-share lines cover every
    collection and are written through the first collection's manifest —
    the node has no event log of its own — so they printed as gutenberg's
    ("why is gutenberg logging about the other plugins?", operator,
    2026-09-18)."""
    stamp = time.strftime("%H:%M:%S")
    tag = f" #{doc_uid}" if doc_uid else ""
    who = "node" if stage in NODE_STAGES else getattr(con, "collection_name", "")
    prefix = f"{who} " if who else ""
    print(f"{stamp} [{prefix}{level}:{stage}{tag}] {message}", flush=True)
    try:
        con.execute(
            "INSERT INTO events(ts,level,stage,doc_uid,message) VALUES(?,?,?,?,?)",
            (int(time.time()), level, stage, doc_uid, message[:2000]))
        # keep the table bounded
        con.execute(
            "DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0) FROM events) - 5000")
    except sqlite3.OperationalError as e:
        # A logger must never crash its caller. Observed live under the
        # per-collection threads: a multi-minute discovery write stream held
        # the manifest's write lock past another writer's busy timeout, the
        # acquire thread's ERROR HANDLER then died in this very function,
        # and a survivable lock contention became a dead thread with a
        # stack trace. The stdout line above already carried the message.
        print(f"{stamp} [{prefix}warn:db] event not persisted ({e}): "
              f"[{level}:{stage}] {message[:200]}", flush=True)

def log_query(con, endpoint, query, mode, k, timings, n_results, books):
    con.execute(
        "INSERT INTO query_log(ts,endpoint,query,mode,k,t_catalog,t_fts,t_dense,"
        "t_rerank,t_llm,total_ms,n_results,books) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), endpoint, query[:500], mode, k,
         timings.get("catalog", 0), timings.get("fts", 0), timings.get("dense", 0),
         timings.get("rerank", 0), timings.get("llm", 0), timings.get("total", 0),
         n_results, json.dumps(books[:20])))
