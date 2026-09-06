"""`pack adopt` — the origin collection hands over to its packed twin
(pack-format.md §6.1: "turn the origin off and migrate the content").

What moves, and why it is safe WITHOUT re-embedding or re-admission:

  - both collections are pinned to the SAME space (checked here: the
    same config id and model hash — same encoder, same chunker layers,
    same cleaning), and the packed twin yields the same uids and chunk
    ids as the origin (the packed plugin's contract, tested), so a row of
    the origin's vector table is exactly a row of the twin's;
  - the vector table is COPIED (Lance directory), never moved: the
    origin stays intact until the operator retires it;
  - documents present in both get the origin's embed marks; provenance
    rows (imported chunks) come along, and every chunk the origin
    embedded locally is recorded as adopted — a later verify-on-use sees
    who made it;
  - with --fts the tantivy index directory is copied too and fts marks
    follow (same index pin: same chunk ids, same tokenizer); rdf marks are
    NOT copied — the twin re-parses metadata from the pack (fast).

Preconditions the caller owns: nothing writes to either collection while
this runs (stop the worker or retire the origin's TOML), and the twin's
Lance directory holds no table yet.
"""
import json
import shutil
import time
from pathlib import Path

from .. import db, embeddings


class AdoptError(RuntimeError):
    pass


def _pinned_space(con) -> dict | None:
    row = con.execute("SELECT space_id, components FROM space WHERE id=1").fetchone()
    if row is None:
        return None
    return {"space_id": row["space_id"],
            "components": json.loads(row["components"]) if row["components"] else None}


def _same_space(origin_con, packed_con) -> tuple[str, str]:
    from ..spaces import SpaceComponents, _config_id
    a, b = _pinned_space(origin_con), _pinned_space(packed_con)
    if a is None:
        raise AdoptError("the origin collection has no pinned space")
    if b is None:
        raise AdoptError("the packed collection has no pinned space yet — start the worker once "
                         "so it pins (boot validation), then stop it and adopt")
    ca, cb = a["components"], b["components"]
    if ca and cb:
        if _config_id(SpaceComponents(**ca)) != _config_id(SpaceComponents(**cb)):
            raise AdoptError("the two collections are pinned to different spaces (chunking/"
                             "cleaning/dtype differ) — adoption would mix spaces (invariant 1)")
        if ca.get("embed_model_hash") != cb.get("embed_model_hash") and not (ca.get("weak") or cb.get("weak")):
            raise AdoptError("the two collections are pinned to different encoders")
    elif a["space_id"] != b["space_id"]:
        raise AdoptError("the two collections are pinned to different spaces")
    return a["space_id"], b["space_id"]


def adopt(origin, packed, *, fts: bool = False, log=print) -> dict:
    if origin.manifest_path == packed.manifest_path:
        raise AdoptError("origin and packed are the same collection")
    origin_con, packed_con = db.connect(origin), db.connect(packed)
    try:
        _same_space(origin_con, packed_con)
        src_lance = Path(origin.lance_dir)
        dst_lance = Path(packed.lance_dir)
        if not (src_lance / f"{embeddings.TABLE}.lance").is_dir():
            raise AdoptError(f"{origin.name} has no vector table at {src_lance}")
        if (dst_lance / f"{embeddings.TABLE}.lance").exists():
            raise AdoptError(f"{packed.name} already has a vector table at {dst_lance} — "
                             f"adoption fills an EMPTY one")
        n_packed = packed_con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if n_packed == 0:
            raise AdoptError(f"{packed.name} has discovered no documents yet — let the worker "
                             f"scan the pack first")

        # 1. the vector table: one directory copy (Lance is self-contained)
        t0 = time.monotonic()
        dst_lance.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_lance / f"{embeddings.TABLE}.lance", dst_lance / f"{embeddings.TABLE}.lance")
        embeddings._dbs.pop(str(dst_lance), None)
        n_vectors = embeddings._conn(packed).open_table(embeddings.TABLE).count_rows()
        log(f"[pack adopt] copied the vector table: {n_vectors} rows in {time.monotonic() - t0:.0f}s")

        # 2. marks + provenance for documents present in both
        now = int(time.time())
        packed_uids = {r["uid"] for r in packed_con.execute("SELECT uid FROM documents")}
        embedded = origin_con.execute(
            "SELECT uid, embed_version FROM documents WHERE embed_done=1 AND error IS NULL").fetchall()
        adopted_docs, missing = 0, 0
        packed_con.execute("BEGIN IMMEDIATE")
        try:
            for row in embedded:
                if row["uid"] not in packed_uids:
                    missing += 1
                    continue
                packed_con.execute(
                    "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? WHERE uid=?",
                    (row["embed_version"], now, row["uid"]))
                adopted_docs += 1
            # provenance: imported chunks keep their producer; the rest were
            # the origin's own — recorded as adopted from it
            for prov in origin_con.execute("SELECT chunk_id, uid, state, producer, bundle_id, cosine "
                                           "FROM chunk_verification"):
                if prov["uid"] in packed_uids:
                    packed_con.execute(
                        "INSERT OR REPLACE INTO chunk_verification(chunk_id,uid,state,producer,"
                        "bundle_id,cosine,checked_at) VALUES(?,?,?,?,?,?,?)",
                        (prov["chunk_id"], prov["uid"], prov["state"], prov["producer"],
                         prov["bundle_id"], prov["cosine"], now))
            db.set_control(packed_con, "adopted_from", json.dumps(
                {"collection": origin.name, "manifest": str(origin.manifest_path),
                 "vectors": n_vectors, "documents": adopted_docs, "at": now}))
            if fts:
                for r in origin_con.execute("SELECT uid, fts_version FROM documents WHERE fts_done=1"):
                    if r["uid"] in packed_uids:
                        packed_con.execute("UPDATE documents SET fts_done=1, fts_version=? WHERE uid=?",
                                           (r["fts_version"], r["uid"]))
            packed_con.execute("COMMIT")
        except BaseException:
            packed_con.execute("ROLLBACK")
            raise
        log(f"[pack adopt] marked {adopted_docs} documents embedded ({missing} embedded origin "
            f"documents are not in the pack)")

        # 3. the text index directory, optionally
        did_fts = False
        if fts:
            src_fts, dst_fts = Path(origin.fts_dir), Path(packed.fts_dir)
            if src_fts.is_dir() and any(src_fts.iterdir()):
                if dst_fts.is_dir() and any(dst_fts.iterdir()):
                    raise AdoptError(f"{packed.name} already has a text index at {dst_fts}")
                shutil.copytree(src_fts, dst_fts, dirs_exist_ok=True)
                did_fts = True
                log(f"[pack adopt] copied the text index from {src_fts}")
        db.log_event(packed_con, "info", "control",
                     f"adopted {n_vectors} vectors for {adopted_docs} documents from {origin.name!r}"
                     + (" and its text index" if did_fts else ""))
        return {"vectors": n_vectors, "documents": adopted_docs, "missing": missing, "fts": did_fts}
    finally:
        origin_con.close()
        packed_con.close()
