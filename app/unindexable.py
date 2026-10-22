"""A document indexed earlier that its origin now declares NOT INDEXABLE
leaves every index at once.

gutenberg's parser 10 (2026-10-22) made pg673 — the 1913 Webster in raw
GCIDE markup, whose catalogue entry says "This book's encoding is broken" —
an `UnindexableDocument`. Writing that error takes the document out of every
selection, but the lexical index still held its 13,003 chunks and the
catalog its title, and the Ask page searches both: an error alone left the
markup in the results. `withdraw` removes the document's lexical entries,
its vectors and its catalog row; the worker calls it where a stage meets the
corpus fact, BEFORE the error is written, so a stop in between leaves a
document that raises again and is withdrawn again (idempotent), never one
marked withdrawn that still answers.
"""
from __future__ import annotations

from typing import Any


def withdraw(con, collection, uid: str) -> dict[str, Any]:
    """`uid`'s lexical entries, vectors and catalog row gone. A failure to
    write an index raises (the caller defers: nothing is recorded on the
    document)."""
    from app import catalog, embeddings, fts
    row = con.execute("SELECT rowid, native_id, lang FROM documents WHERE uid=?", (uid,)).fetchone()
    if row is None:
        return {"uid": uid, "withdrawn": False}
    try:
        book = int(row["native_id"])
    except (TypeError, ValueError):
        book = None
    if book is not None:
        fts.delete_book(row["lang"] or "und", book, collection)
        embeddings.delete_book(book, collection, lock_con=con)
    catalog.delete_rows(collection, [row["rowid"]])
    return {"uid": uid, "withdrawn": True, "book_id": book}
