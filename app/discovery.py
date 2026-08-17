"""Corpus discovery — idempotent per-collection sync against the manifest.

Cheap gate: (mtime, size) from the plugin's discover() pass. Only when that
changes do we call fetch() to hash content; only when the hash actually
differs do we reset downstream stage flags for that one document. A
no-change run is just a stat scan — discover() never touches file content.
"""
import time

from . import db
from .plugins.base import DiscoverState, DocRef


def sync_collection(con, collection) -> dict:
    t0 = time.time()
    plugin = collection.plugin_instance()
    root = collection.corpus_dir
    if not root.is_dir():
        db.log_event(con, "error", "discover", f"corpus dir not found: {root}")
        return {"error": f"corpus dir not found: {root}"}

    state = DiscoverState(corpus_root=root)
    seen = 0
    # De-duplicate by uid, keeping the LAST ref seen: a source may legitimately
    # yield the same uid more than once within one discover() sweep (e.g.
    # PubMed's updatefiles revising a baseline record, or a delete tombstone
    # arriving after the record it retracts). Comparing every duplicate
    # against a gate the previous duplicate just rewrote thrashes forever and
    # breaks idempotence (CLAUDE.md invariant 2); processing containers in
    # sorted/chronological order and keeping the last ref per uid gives
    # correct "last write wins" delta semantics instead. For Gutenberg this
    # is a no-op — one document never appears twice in one pass.
    #
    # Trade-off, honestly noted: this buffers every ref for the pass before
    # writing anything, so a very large discover() (millions of records)
    # holds them all in memory and commits no progress until the whole scan
    # finishes. Acceptable at fixture/dev scale; revisit if/when the
    # acquisition layer (P0.4) needs true streaming delta sync.
    by_uid: dict[str, DocRef] = {}
    for ref in plugin.discover(state):
        seen += 1
        by_uid[f"{ref.source_id}:{ref.native_id}"] = ref

    added = changed = missing = 0
    for i, (uid, ref) in enumerate(by_uid.items(), start=1):
        if ref.error:
            missing += 1
            con.execute(
                "INSERT INTO documents(uid,source,native_id,path,error,updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET error=excluded.error",
                (uid, ref.source_id, ref.native_id, ref.locator, ref.error,
                 int(time.time())))
        else:
            txt_name = ref.extra.get("txt_name")
            row = con.execute(
                "SELECT mtime,size,txt_hash FROM documents WHERE uid=?", (uid,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO documents(uid,source,native_id,path,txt_name,mtime,size,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (uid, ref.source_id, ref.native_id, ref.locator, txt_name,
                     ref.mtime, ref.size, int(time.time())))
                added += 1
            elif row["mtime"] != ref.mtime or row["size"] != ref.size:
                raw = plugin.fetch(ref)
                if raw.raw_hash != row["txt_hash"]:
                    con.execute(
                        "UPDATE documents SET path=?,txt_name=?,mtime=?,size=?,txt_hash=?,"
                        "rdf_done=0,fts_done=0,embed_done=0,summary_done=0,error=NULL,"
                        "retries=0,updated_at=? WHERE uid=?",
                        (ref.locator, txt_name, ref.mtime, ref.size, raw.raw_hash,
                         int(time.time()), uid))
                    changed += 1
                else:
                    con.execute("UPDATE documents SET mtime=?,size=? WHERE uid=?",
                                (ref.mtime, ref.size, uid))
        if i % 2000 == 0:
            con.execute("COMMIT") if con.in_transaction else None

    apply_scope(con)
    stats = {"seen": seen, "added": added, "changed": changed,
             "missing_txt": missing, "secs": round(time.time() - t0, 1)}
    db.log_event(con, "info", "discover", f"discovery done {stats}")
    return stats


def apply_scope(con):
    """Recompute documents.in_scope from the current scope config.

    native_id is TEXT (generic across plugins); Gutenberg's ids are numeric,
    so ordering/comparison here casts to INTEGER to match pre-refactor
    int-`id` behavior exactly. A future non-numeric-id plugin will need a
    per-plugin ordering hook — out of scope for P0.1 (single Gutenberg
    collection).
    """
    scope = db.get_scope(con)
    con.execute("UPDATE documents SET in_scope=1")
    if scope["languages"]:
        marks = ",".join("?" for _ in scope["languages"])
        con.execute(
            f"UPDATE documents SET in_scope=0 WHERE lang IS NOT NULL "
            f"AND lang NOT IN ({marks})", scope["languages"])
    if scope["book_ids"]:
        marks = ",".join("?" for _ in scope["book_ids"])
        con.execute(
            f"UPDATE documents SET in_scope=0 WHERE CAST(native_id AS INTEGER) "
            f"NOT IN ({marks})", scope["book_ids"])
    if scope["sample_per_lang"]:
        n = int(scope["sample_per_lang"])
        con.execute("""
            UPDATE documents SET in_scope=0 WHERE in_scope=1 AND uid NOT IN (
              SELECT uid FROM (
                SELECT uid, ROW_NUMBER() OVER (
                  PARTITION BY lang ORDER BY CAST(native_id AS INTEGER)) rn
                FROM documents WHERE in_scope=1) WHERE rn<=?)""", (n,))
    if scope["limit"]:
        con.execute("""
            UPDATE documents SET in_scope=0 WHERE in_scope=1 AND uid NOT IN (
              SELECT uid FROM documents WHERE in_scope=1
              ORDER BY CAST(native_id AS INTEGER) LIMIT ?)""",
            (int(scope["limit"]),))
