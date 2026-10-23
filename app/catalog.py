"""The catalog leg's index: every document's title, authors and subjects,
in tantivy beside the collection's text index (`index/_catalog`, 07 §7.8's
"the text index … and the facet values").

The catalog lived in an SQLite FTS5 table, and FTS5 ranks by scoring EVERY
match before it keeps the top k. On pubmed's 40M records (2026-09-18) that
made the leg cost 4.0 s for "aspirin platelet aggregation" (15,078 matches),
9.1 s for "cancer treatment" and 57.3 s for "cancer" (1,476,347): a
per-search cost that grew with the corpus. tantivy's top-k skips postings
that cannot reach the top, and measured on 4M pubmed records it answered in
1.2–2.9 ms, at 96 bytes a record.

Writers, one at a time per collection (`_lock`):
- the rdf stage, for the documents it extracts, committed BEFORE the
  manifest marks them (invariant 2: a crash in between re-runs the batch,
  and delete-then-insert on `rowid` keeps it idempotent);
- the index maintainer's backfill, which walks the documents already
  extracted in bounded rowid windows until it has covered them all. It
  reads each window INSIDE the lock, so it can never write back metadata
  older than what the rdf stage has just committed.

Until a collection's backfill is complete, `search` returns None and the
catalog leg is empty (the SQLite FTS5 catalog that answered meanwhile was
dropped on 2026-09-19, db.drop_catalog_fts). The state file (`STATE`)
lives inside the index directory, so an index that is removed takes its
"complete" with it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import tantivy

from . import config

#: Bump when the schema or the analyzer changes: an index built under
#: another version is rebuilt from scratch by the backfill.
CATALOG_VERSION = 1
STATE = "louise-catalog.json"
FIELDS = ("title", "authors", "subjects")

_lock = threading.Lock()                      # guards the dicts below
_write_locks: dict[str, threading.Lock] = {}  # index path -> one writer at a time
_indexes: dict[str, tantivy.Index] = {}      # the worker's index handles, by path
_writers: dict = {}                          # and its one writer per index, by path
_readers: dict[str, tantivy.Index] = {}      # reading handles, by path#generation


def forget(collection) -> None:
    """Drop the handles on this collection's catalog index — for a
    collection that left the node, so one re-created at the same path never
    writes through a handle on deleted files (fts.forget, 2026-10-13)."""
    base = str(path_for(collection))
    with _lock:
        for table in (_indexes, _writers, _readers, _write_locks):
            for k in [k for k in table if str(k) == base or str(k).startswith(base + "#")]:
                table.pop(k, None)


def path_for(collection) -> Path:
    return Path(collection.fts_dir) / "_catalog"


def _schema() -> tantivy.Schema:
    b = tantivy.SchemaBuilder()
    b.add_integer_field("rowid", stored=True, indexed=True, fast=True)
    for field in FIELDS:
        b.add_text_field(field, stored=False, tokenizer_name="catalog_tok")
    b.add_text_field("lang", stored=False, tokenizer_name="raw")
    return b.build()


def _analyzer() -> tantivy.TextAnalyzer:
    """What FTS5's default `unicode61` tokenizer did: split on anything that
    is not a letter or a digit, fold case and diacritics. No stemming — the
    catalog never had any."""
    return (tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
            .filter(tantivy.Filter.remove_long(64))
            .filter(tantivy.Filter.lowercase())
            .filter(tantivy.Filter.ascii_fold())
            .build())


def _open(path: Path) -> tantivy.Index:
    idx = tantivy.Index(_schema(), path=str(path))
    idx.register_tokenizer("catalog_tok", _analyzer())
    return idx


def _reader_index(collection, generation) -> tantivy.Index | None:
    """A reader's handle — the web's, another process — keyed on the index's
    GENERATION, so a rebuilt index is never read through a handle to the
    files it replaced. A reader never creates an index."""
    path = path_for(collection)
    key = f"{path}#{generation}"
    with _lock:
        idx = _readers.get(key)
        if idx is None:
            if not (path / "meta.json").is_file():
                return None
            for old in [k for k in _readers if k.startswith(f"{path}#")]:
                del _readers[old]
            idx = _readers[key] = _open(path)
        return idx


def _write_lock(collection) -> threading.Lock:
    with _lock:
        return _write_locks.setdefault(str(path_for(collection)), threading.Lock())


def read_state(collection) -> dict:
    try:
        state = json.loads((path_for(collection) / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if state.get("version") == CATALOG_VERSION else {}


def _write_state(collection, state: dict) -> None:
    path = path_for(collection) / STATE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({**state, "version": CATALOG_VERSION}), encoding="utf-8")
    os.replace(tmp, path)


def count(collection) -> int:
    """The entries in this collection's catalog index, complete or not
    (0 when it has none) — for tests and tools, through a handle of its own."""
    path = path_for(collection)
    if not (path / "meta.json").is_file():
        return 0
    idx = _open(path)
    idx.reload()
    return idx.searcher().num_docs


def ready(collection) -> bool:
    return bool(read_state(collection).get("complete"))


def _put(writer, rows) -> int:
    """(rowid, title, authors, subjects, lang) rows: delete-then-insert on
    the rowid, so a row written twice is there once."""
    n = 0
    for rowid, title, authors, subjects, lang in rows:
        writer.delete_documents("rowid", int(rowid))
        writer.add_document(tantivy.Document(
            rowid=int(rowid), title=title or "", authors=authors or "",
            subjects=subjects or "", lang=lang or ""))
        n += 1
    return n


def _writer(collection):
    """The worker's ONE writer for this collection's catalog, created on
    first use and kept: each batch adds and commits, and tantivy merges its
    segments on the writer's own threads. A writer per batch had to wait for
    those merges before it could let go of the directory — inside the rdf
    stage's manifest transaction, which every other writer of the manifest
    waits on. Called under the write lock."""
    path = path_for(collection)
    with _lock:
        writer = _writers.get(str(path))
        if writer is None:
            path.mkdir(parents=True, exist_ok=True)
            idx = _indexes.get(str(path)) or _open(path)
            _indexes[str(path)] = idx
            writer = _writers[str(path)] = idx.writer(heap_size=128_000_000)
        return writer


def _drop_writer(collection) -> None:
    """Let go of the directory (a fresh start removes it). Under the write lock."""
    path = str(path_for(collection))
    with _lock:
        writer = _writers.pop(path, None)
        _indexes.pop(path, None)
    if writer is not None:
        writer.wait_merging_threads()        # consumes it: the directory's lock goes


def release_under(directory) -> int:
    """Let go of every handle this process holds under a collection's
    directory — its writer (each under its write lock, so a commit in flight
    finishes first), its index and its readers. `hold collection forget`
    removes the directory; a collection made again under the same name gets
    the same path, and the worker's cached writer, open on files that no
    longer exist, refused every write: "Failed to open file for write …
    index/_catalog/.tmp…" on the re-made `louise-stop` (2026-10-07).
    Returns how many writers were released."""
    root = str(Path(directory))

    def under(p: str) -> bool:
        return p == root or p.startswith(root.rstrip("/") + "/")
    with _lock:
        paths = sorted({p for p in list(_writers) + list(_indexes) if under(p)})
        writers = [(_writers.pop(p, None), _write_locks.get(p)) for p in paths]
        for p in paths:
            _indexes.pop(p, None)
        for k in [k for k in _readers if under(k.split("#", 1)[0])]:
            del _readers[k]
    released = 0
    for writer, lock in writers:
        if writer is None:
            continue
        try:
            if lock is not None:
                with lock:
                    writer.wait_merging_threads()
            else:
                writer.wait_merging_threads()
        except Exception:   # noqa: BLE001 — a writer on deleted files may fail to close; it is let go
            pass
        released += 1
    return released


def close_all() -> None:
    """Let go of every writer this process holds — for tests, whose
    collections come and go by the hundred. The worker keeps its writers
    until it exits.

    Each under ITS WRITE LOCK: a thread still inside a commit holds the
    writer, and taking it from under that thread raised "Already borrowed"
    (the node's suite, 2026-09-20: a pipeline thread of a collection the
    test had just removed was still committing its rdf batch at teardown).
    Waiting for the lock lets that commit finish first."""
    with _lock:
        writers = [(writer, _write_locks.get(path)) for path, writer in _writers.items()]
        _writers.clear()
        _indexes.clear()
        _readers.clear()
    for writer, lock in writers:
        if lock is None:
            writer.wait_merging_threads()
            continue
        with lock:
            writer.wait_merging_threads()


def _commit(collection, rows) -> int:
    writer = _writer(collection)
    n = _put(writer, rows)
    writer.commit()
    return n


def write(collection, rows) -> int:
    """The rdf stage's write: these documents' catalog entries, committed
    before it returns (the caller marks them afterwards). Any failure is
    `CatalogUnavailable`, which the step runner treats as a deferral."""
    rows = list(rows)
    if not rows:
        return 0
    try:
        with _write_lock(collection):
            return _commit(collection, rows)
    except Exception as e:  # noqa: BLE001 — never the document's fault
        raise CatalogUnavailable(f"catalog index: {type(e).__name__}: {e}") from e


def delete_rows(collection, rowids) -> int:
    """These documents' catalog entries gone, committed before it returns —
    a document the origin now declares not indexable (app/unindexable.py).
    Any failure is `CatalogUnavailable`, a deferral like `write`'s."""
    rowids = [int(r) for r in rowids]
    if not rowids:
        return 0
    try:
        with _write_lock(collection):
            writer = _writer(collection)
            for rowid in rowids:
                writer.delete_documents("rowid", rowid)
            writer.commit()
    except Exception as e:  # noqa: BLE001 — never the document's fault
        raise CatalogUnavailable(f"catalog index: {type(e).__name__}: {e}") from e
    return len(rowids)


_WINDOW_SQL = ("SELECT rowid, title, authors, subjects, lang FROM documents "
               "WHERE rowid > ? AND rowid <= ? AND rdf_done = 1")


def rewrite(collection, rows) -> int:
    """The catalog rewritten from `rows` — every live document's (rowid,
    title, authors, subjects, lang) — and nothing else, so an erased
    document's title leaves the disk and not only the results. The catalog
    stores none of its text fields, so it cannot be rebuilt from itself:
    the caller reads the rows from the manifest. Why a rewrite and not a
    delete is `fts.rewrite_without`'s docstring. For an erasable collection
    only (02 §2.9). Returns the documents written."""
    rows = list(rows)
    with _write_lock(collection):
        writer = _writer(collection)
        writer.delete_all_documents()
        n = _put(writer, rows)
        writer.commit()
        writer.garbage_collect_files()
    return n


def _fresh_start(collection) -> dict:
    """An index of this version from nothing: the old files go, under the
    write lock, and a new GENERATION tells readers to reopen."""
    import shutil
    path = path_for(collection)
    with _write_lock(collection):
        _drop_writer(collection)
        # MOVED ASIDE, then removed (Perugia's gate, 2026-10-23, under load:
        # "OSError: [Errno 39] Directory not empty: …/index/_catalog" — a file
        # written into the directory while rmtree emptied it). A rename is
        # atomic whatever happens inside; what is left of the old one is
        # removed best-effort, now and at the next fresh start. Its name
        # starts with "_", so fts.existing_groups never takes it for a group.
        for old in path.parent.glob(f"{path.name}.old-*"):
            shutil.rmtree(old, ignore_errors=True)
        if path.is_dir():
            aside = path.with_name(f"{path.name}.old-{time.time_ns()}")
            os.replace(path, aside)
            shutil.rmtree(aside, ignore_errors=True)
        _writer(collection)
        state = {"cursor": 0, "complete": False, "generation": time.time_ns()}
        _write_state(collection, state)
    return state


def backfill(con, collection, *, budget_s: float | None = None,
             window: int | None = None, may_continue=lambda: True) -> dict | None:
    """Cover the documents already extracted, one rowid WINDOW at a time —
    a bounded range of the table's own b-tree, whatever share of it is
    extracted — until `budget_s` is spent. Returns what it did, or None when
    there was nothing to do (complete already, or an empty manifest)."""
    budget_s = config.CATALOG_BACKFILL_BUDGET_S if budget_s is None else budget_s
    window = config.CATALOG_BACKFILL_WINDOW if window is None else window
    state = read_state(collection)
    if state.get("complete"):
        return None
    top = con.execute("SELECT max(rowid) FROM documents").fetchone()[0] or 0
    if top == 0:
        return None
    if not state:
        state = _fresh_start(collection)
    cursor = int(state.get("cursor", 0))
    t0 = time.monotonic()
    written = windows = 0
    while cursor < top and time.monotonic() - t0 < budget_s and may_continue():
        end = min(cursor + window, top)
        with _write_lock(collection):
            # read INSIDE the lock: the rdf stage's newer rows cannot be
            # overwritten by an older read of them
            rows = con.execute(_WINDOW_SQL, (cursor, end)).fetchall()
            written += _commit(collection, rows)
            cursor = end
            state = {**state, "cursor": cursor, "top": top}
            _write_state(collection, state)
        windows += 1
    if cursor >= top:
        state = {**state, "complete": True}
        _write_state(collection, state)
    return {"windows": windows, "written": written, "cursor": cursor, "top": top,
            "complete": bool(state.get("complete")),
            "secs": round(time.monotonic() - t0, 1)}


def search(con, collection, query: str, limit: int, langs=()) -> list[dict] | None:
    """The top `limit` documents whose title, authors or subjects hold
    EVERY word of `query` (FTS5's rule), best first, as `catalog_search`
    returns them — or None while this collection's catalog is incomplete.
    Only the `limit` rows returned are read from the manifest."""
    state = read_state(collection)
    if not state.get("complete"):
        return None
    idx = _reader_index(collection, state.get("generation"))
    if idx is None:
        return None
    words = [w for w in query.replace("'", " ").split() if w.isalnum()]
    if not words:
        return []
    idx.reload()
    q = idx.parse_query(" ".join(words), list(FIELDS), conjunction_by_default=True)
    if langs:
        schema = idx.schema
        only = tantivy.Query.boolean_query(
            [(tantivy.Occur.Should, tantivy.Query.term_query(schema, "lang", lang))
             for lang in langs])
        q = tantivy.Query.boolean_query([(tantivy.Occur.Must, q), (tantivy.Occur.Must, only)])
    searcher = idx.searcher()
    rowids = [searcher.doc(addr)["rowid"][0] for _score, addr in searcher.search(q, limit).hits]
    if not rowids:
        return []
    found = {r["rowid"]: r for r in con.execute(
        "SELECT rowid, native_id, title, authors, subjects, lang FROM documents "
        f"WHERE rowid IN ({','.join('?' * len(rowids))})", rowids)}
    out = []
    for rowid in rowids:                 # tantivy's order; a row gone since is dropped
        r = found.get(rowid)
        if r is None:
            continue
        out.append({"book_id": int(r["native_id"]), "title": r["title"],
                    "authors": r["authors"], "subjects": (r["subjects"] or "")[:300],
                    "lang": r["lang"]})
    return out


class CatalogUnavailable(RuntimeError):
    """The catalog index could not be written at this moment. A DEFERRAL:
    the documents stay unmarked and are retried; nothing is recorded on
    them (CLAUDE.md, "deferral is not failure")."""
