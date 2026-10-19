"""Marking a document EMBEDDED in the manifest — one place for what three
writers did inline (the worker's embed stage twice, the old coordinator's
contribute) and one did not at all.

`share contribute`'s build (`hold.vectors_build.build`) wrote vectors and
never touched `documents`: the node had 372 in-scope documents with every
chunk in Lance and `embed_done = 0` (München, 2026-10-19) — the worker
would embed them again under `all local`, the Library counted them as
pending, a query's lazy path could re-embed them. A stage that writes a
document's last vector marks the document, AFTER the write (invariant 2:
marking first is the one ordering that loses data); and a document is
complete when every chunk the layers derive from its record has a vector
— `complete_uids` asks the vector table, never `documents.chunks`.
"""
from __future__ import annotations

import time
from typing import Callable, Iterable, Sequence

from app import config


def mark_embedded(con, collection, uids: Iterable[str], *, synced: bool = True) -> int:
    """`embed_done = 1`, this `EMBED_VERSION`, `updated_at` now, for each uid;
    its imported-provenance rows go (the chunks are this peer's now) and
    its queue entry with them. `synced`: the rows just written carry a
    versioned collection's columns (the worker's writes do; a build through
    `embeddings.upsert_chunks` does not) — only then are they marked
    synced (app/validity.py). Returns how many rows the UPDATE touched."""
    from app import validity as validity_mod
    from app.bundles import ledger as bundle_ledger
    now = int(time.time())
    ids = [str(u) for u in uids]
    touched = 0
    for uid in ids:
        touched += con.execute(
            "UPDATE documents SET embed_done=1, embed_version=?, updated_at=? WHERE uid=?",
            (config.EMBED_VERSION, now, uid)).rowcount
        bundle_ledger.clear_verification(con, uid)
        con.execute("DELETE FROM embed_queue WHERE uid=?", (uid,))
    if synced and ids and validity_mod.declared(collection) is not None:
        native = []
        for u in ids:
            try:
                native.append(int(str(u).split(":", 1)[1]))
            except (IndexError, ValueError):
                continue
        if native:
            validity_mod.mark_synced(con, native)
    return touched


def complete_uids(codex, records: Sequence[dict],
                  vectors_for: Callable[[list[str]], dict[str, list]]) -> list[str]:
    """The uids among `records` whose every chunk has a vector here — the
    chunks through `layers.chunks_for_document` (invariant 1), the vectors
    through the caller's lookup. A record the layers give no chunk is
    complete (there is nothing to embed; the worker marks it done the same
    way), one with a chunk missing is not."""
    from app import layers as layers_mod
    per_uid: dict[str, list[str]] = {}
    for rec in records:
        uid = rec["uid"]
        row = {"uid": uid, "native_id": rec.get("native_id", 0)}
        sections = [tuple(s) for s in rec.get("sections") or []]
        per_uid[uid] = [c["id"] for c in layers_mod.chunks_for_document(codex, row, sections)]
    wanted = [i for ids in per_uid.values() for i in ids]
    held = vectors_for(wanted) if wanted else {}
    held = held or {}
    return [uid for uid, ids in per_uid.items() if all(i in held for i in ids)]


def mark_complete(con, collection, codex, records: Sequence[dict],
                  vectors_for: Callable[[list[str]], dict[str, list]], *,
                  only_unmarked: bool = True) -> int:
    """`complete_uids`, marked. `only_unmarked`: the rows with `embed_done =
    0` only (a reconcile that leaves the worker's own marks and their
    `updated_at` alone)."""
    uids = complete_uids(codex, records, vectors_for)
    if not uids:
        return 0
    if only_unmarked:
        pending: list[str] = []
        for i in range(0, len(uids), 500):
            part = uids[i:i + 500]
            got = con.execute(f"SELECT uid FROM documents WHERE embed_done=0 AND uid IN "
                              f"({','.join('?' * len(part))})", part).fetchall()
            pending.extend(r["uid"] for r in got)
        uids = pending
    if not uids:
        return 0
    return mark_embedded(con, collection, uids, synced=False)
