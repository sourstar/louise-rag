"""Cross-process serialization of vector-table writes.

THE RACE. LanceDB has no cross-call transaction and enforces no primary key,
so a write is `delete(book_id = N)` followed by `add(rows)` — two independent
operations. Two writers interleaving on the same document produce DUPLICATE
rows in every ordering that is not cleanly serialized:

    Id Ld Ia La   ->  L's delete precedes I's add; both row sets survive
    Ld Id La Ia   ->  symmetric
    Id Ld La Ia   ->  one complete set, then the other appended onto it
    Ld Id Ia La   ->  symmetric

Duplicates are not benign: the same chunk_id appears twice in retrieval, so it
contributes twice to the RRF fuse and outranks honest results.

WHY A THREADING LOCK IS USELESS HERE. The web process, the worker process and
the CLI are separate OS processes. app/fts.py's module-level lock has exactly
this limitation. SQLite is the only coordination point they share, and
db.connect() already sets busy_timeout=15000, so contenders block rather than
fail.

`BEGIN IMMEDIATE` takes a real RESERVED lock on the collection's manifest, held
for the duration of the delete+add pair. Both the import path AND the local
embed path must take it — a lock only one writer respects protects nothing.
"""
import sqlite3
from contextlib import contextmanager


class VectorWriteBusy(RuntimeError):
    """Another process is writing this collection's vector table and did not
    release within the busy timeout."""


@contextmanager
def vector_write_lock(con):
    """Serialize a vector-table write across processes.

    Held across Lance I/O, which is why the transaction body must stay small:
    open the table, delete, add, done. Do not fetch, embed, or hash inside it.

    Re-entrant in the sense that matters: if the caller already holds a write
    transaction, this yields without nesting (SQLite has no nested BEGIN), so
    an importer that has already opened one is not deadlocked by a helper that
    also asks for the lock.
    """
    if con.in_transaction:
        yield
        return
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        raise VectorWriteBusy(
            f"could not acquire the vector write lock: {e}") from None
    try:
        yield
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        con.execute("COMMIT")
