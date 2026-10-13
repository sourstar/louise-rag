#!/usr/bin/env bash
# restore-normattiva-1861-manifest.sh — put the CARRIED manifest back into
# normattiva-1861 (2026-10-14, RULE 1). Run on the node:
#   ssh root@node bash -s < deploy/16f/restore-normattiva-1861-manifest.sh
#
# What went wrong (STATUS 2026-10-14): `dev carry --rebuilt --copy` deleted the
# empty target manifest; two seconds later the worker, on "collection added",
# created a fresh one despite the carry's lock; the carry saw a manifest
# "present" and skipped the copy. The vector table WAS carried (1,833,119 rows),
# the manifest was not: every document restarted at rdf/fts/embed 0 and the
# worker began redoing all of it (embedding by reuse, ~2 documents/s).
#
# The restore, with both collections paused and their locks taken here:
#   1. remember which documents the wrong run indexed (fts_done=1): their text
#      index rows were rewritten with whatever validity the wrong manifest had
#      then, so they are indexed once more after the restore;
#   2. the wrong manifest set aside (state/manifest.sqlite.wrong-<ts>), the
#      old collection's manifest checkpointed and copied in, re-pinned to the
#      new Codex (carry._repin);
#   3. those documents' fts_done reset; every validity row marked unsynced, so
#      the maintainer rewrites the vector table's validity columns the wrong
#      run may have written from a partial version table;
#   4. `dev carry --reconcile` again (the copy has no reconcile state): every
#      document whose content_hash did not move keeps its flags and vectors.
# On resume, discovery adds the 23,610 older versions and relocates the rest.
set -euo pipefail
W=ix-louise-rag-worker-1
L="docker exec $W python -m app.louise"

echo "=== 0. both paused"
$L node pause normattiva-1861
$L node pause normattiva
sleep 20                                   # the worker finishes its unit and lets the locks go

echo "=== 1-3. the manifest"
docker exec -i "$W" python - <<'PY'
import shutil, sqlite3, time
from pathlib import Path
from app.hold import carry, collection as col, lock as lock_mod

root = Path("/data/collections")
locks = [lock_mod.take(lock_mod.state_dir(root, n), n, "restore-normattiva-1861-manifest", exit_code=2)
         for n in ("normattiva", "normattiva-1861")]
try:
    _, src = col.resolve(root, "normattiva")
    _, to = col.resolve(root, "normattiva-1861")
    old_m = root / "normattiva" / "state" / "manifest.sqlite"
    new_m = root / "normattiva-1861" / "state" / "manifest.sqlite"

    con = sqlite3.connect(new_m, timeout=60)
    indexed = [r[0] for r in con.execute("SELECT uid FROM documents WHERE fts_done=1")]
    before = con.execute("SELECT COUNT(*), SUM(rdf_done), SUM(fts_done), SUM(embed_done) FROM documents").fetchone()
    con.close()
    print("wrong manifest: docs, rdf, fts, embed =", before, "| re-index after restore:", len(indexed))

    carry._checkpoint(new_m)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    for suffix in ("", "-wal", "-shm"):
        p = Path(f"{new_m}{suffix}")
        if p.exists():
            p.rename(Path(f"{new_m}.wrong-{stamp}{suffix}"))

    carry._checkpoint(old_m)
    tmp = new_m.with_name(".manifest.sqlite.restore")
    shutil.copy2(old_m, tmp)
    tmp.rename(new_m)
    cleared = carry._repin(new_m, src, to)

    con = sqlite3.connect(new_m, timeout=60, isolation_level=None)
    con.execute("BEGIN IMMEDIATE")
    for i in range(0, len(indexed), 500):
        part = indexed[i:i + 500]
        con.execute(f"UPDATE documents SET fts_done=0 WHERE uid IN ({','.join('?' * len(part))})", part)
    con.execute("UPDATE validity SET synced=0")
    con.execute("DELETE FROM control WHERE key=?", (carry.RECONCILE_KEY,))
    con.execute("COMMIT")
    after = con.execute("SELECT COUNT(*), SUM(rdf_done), SUM(fts_done), SUM(embed_done) FROM documents").fetchone()
    con.close()
    print("restored: docs, rdf, fts, embed =", after, "| coverage rows cleared:", cleared)
finally:
    for lk in locks:
        lk.release()
PY

echo "=== 4. reconcile against the snapshots"
$L dev carry normattiva normattiva-1861 --reconcile --yes

echo "=== 5. resume the new one only (normattiva stays paused)"
$L node resume normattiva-1861
sleep 90
docker logs --since 3m "$W" 2>&1 | grep -i "normattiva-1861" | grep -i "discover\|relocat\|embed" | tail -6
echo "expect: discovery added 23610; embed lines with chunks embedded only for new versions"
