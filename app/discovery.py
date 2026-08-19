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

    state = DiscoverState(corpus_root=root,
                           state_dir=collection.manifest_path.parent)
    seen = 0
    # STREAMING with last-write-wins BY ORDER. A source may yield the same
    # uid more than once in one sweep (PubMed updatefiles revising a baseline
    # record; a delete tombstone after the record it retracts) — plugins
    # enumerate containers in sorted/chronological order, so applying refs in
    # arrival order gives the same final state the old buffered de-dup did:
    # the later ref hits the ordinary mtime/size→hash change gate and wins.
    # The buffer itself had to go: a cold-cursor scan of full PubMed is ~26M
    # refs — the "revisit at millions of records" moment the previous
    # docstring named, arrived. Progress now commits as the scan streams
    # (WAL autocommit), which also makes an interrupted giant scan resumable
    # in effect: rows written so far stand, the cursor file is only advanced
    # by the plugin for containers it finished enumerating.
    added = changed = missing = container_errors = 0
    for i, ref in enumerate(plugin.discover(state), start=1):
        uid = f"{ref.source_id}:{ref.native_id}"
        if ref.scope == "container":
            # A container that could not be enumerated is a scan REPORT, not
            # a document (DocRef.scope). Storing it as one minted a
            # filename-keyed row nothing ever removed, which an operator's
            # "clear errors & retry" then fed to the rdf stage forever.
            # Logging it keeps it visible (the System page merges every
            # collection's events) and lets a later clean scan heal it by
            # simply not reporting it again.
            container_errors += 1
            db.log_event(con, "warn", "discover",
                         f"container not enumerable: {ref.error}", None)
            continue      # a scan report is not a document, so it is not "seen"
        seen += 1
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
    if container_errors:
        stats["container_errors"] = container_errors
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
