"""doc_keys population — the generic hook that tags a document with the
entities its own text mentions, so retrieval.search() can later boost/filter
on them. See docs/architecture-v2.md §4.

Not inside any CorpusSource's parse(): parse() isn't in the live ingestion
path today (worker.py calls plugin-specific read_metadata/read_chapters
instead, to preserve lazy per-stage I/O). This module is called from
worker.py's stage_fts, which already has chunk text in hand — CanonicalDoc.keys
stays available for a future plugin with genuinely structured native entity
metadata; this is the generic path for everything else.
"""
from .plugins.base import EntityResolver, RefLookup


def tag_document(con, uid: str, title: str, chunk_texts: list[str],
                  resolver: EntityResolver, refs: RefLookup) -> list[str]:
    """Delete-then-insert into doc_keys for `uid` (CLAUDE.md invariant 2).
    Returns the entity ids written (possibly empty)."""
    text = (title or "") + "\n" + "\n".join(chunk_texts)
    entity_ids = sorted({e.entity_id for e in resolver.resolve(text, refs)})
    con.execute("DELETE FROM doc_keys WHERE uid=?", (uid,))
    if entity_ids:
        con.executemany(
            "INSERT INTO doc_keys(uid, entity_id) VALUES (?,?)",
            [(uid, eid) for eid in entity_ids])
    return entity_ids
