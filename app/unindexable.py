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


def withdraw_many(con, collection, uids) -> int:
    """`withdraw` for many documents, one commit per index rather than one
    per document (the commit is what costs: fts.delete_books) — what the
    origin RETRACTED in one discovery batch (06 §6.1's deletion signal,
    app/discovery.py): a day of PubMed's update files can delete hundreds.
    Same rule as `withdraw`: a failure to write an index raises, and the
    caller defers. Returns how many documents were found."""
    from app import catalog, embeddings, fts
    uids = sorted(set(uids))
    rows = []
    for i in range(0, len(uids), 900):
        part = uids[i:i + 900]
        rows += con.execute("SELECT rowid, native_id, lang FROM documents WHERE uid IN "
                            f"({','.join('?' * len(part))})", part).fetchall()
    by_group: dict[str, list[int]] = {}
    books: list[int] = []
    for row in rows:
        try:
            book = int(row["native_id"])
        except (TypeError, ValueError):
            continue
        books.append(book)
        lang = row["lang"] or "und"
        if lang == fts.MULTILINGUAL:
            fts.delete_book(lang, book, collection)      # its sections may sit in several groups
        else:
            by_group.setdefault(fts.group_for(lang), []).append(book)
    for group, ids in sorted(by_group.items()):
        fts.delete_books(group, ids, collection)
    if books:
        embeddings.delete_books(books, collection, lock_con=con)
    if rows:
        catalog.delete_rows(collection, [r["rowid"] for r in rows])
    return len(rows)
