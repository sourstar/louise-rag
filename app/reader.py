"""The document reader behind an Ask hit (STATUS 5j).

A click on a hit opens the WHOLE document — the matched passage scrolled
into view — without ever touching the corpus file on the click: a PubMed
container costs 13–20 s to open, and the text is already in the index
stores. The lexical index stores every chunk's body (tantivy, `stored`),
so a document is its chunks in seq order from there; a document that is
embedded but not (yet) re-indexed falls back to its Lance rows, which
carry the same text. Nothing else is read.

The reference line is generic (title, authors, language, source id) with
two source-specific touches: PubMed's journal and date come from the
per-container pack (16 ms, no XML parse), and every source gets a link to
its origin page where one exists.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from . import collections as collections_mod, db, embeddings, fts

# origin pages per source plugin: what a reader clicks to see the real thing
ORIGIN = {
    "pubmed": lambda row: f"https://pubmed.ncbi.nlm.nih.gov/{row['native_id']}/",
    "gutenberg": lambda row: f"https://www.gutenberg.org/ebooks/{row['native_id']}",
    "wikipedia": lambda row: (f"https://{row['lang'] or 'en'}.wikipedia.org/wiki/"
                              f"{quote((row['title'] or '').replace(' ', '_'))}"
                              if row.get("title") else None),
    "embedded": lambda row: (f"https://{row['lang'] or 'en'}.wikipedia.org/wiki/"
                             f"{quote((row['title'] or '').replace(' ', '_'))}"
                             if row.get("title") else None),
}


def collection_by_name(name: str):
    for c in collections_mod.load_all():
        if c.name == name:
            return c
    return None


def document(collection, book_id: int) -> dict | None:
    """Header + chunks for one document, or None when the manifest does
    not know it. `chunks` is [] when neither index store holds text yet
    (not indexed: the card says so)."""
    con = db.connect(collection)
    row = con.execute(
        "SELECT uid, source, native_id, path, lang, title, authors, subjects, "
        "fts_done, embed_done FROM documents WHERE CAST(native_id AS INTEGER)=? "
        "OR native_id=? LIMIT 1", (book_id, str(book_id))).fetchone()
    if row is None:
        return None
    row = dict(row)
    # asked of the index regardless of fts_done: a re-index in progress
    # (a chunker change, `reprocess lexical index`) clears the mark but the
    # entries stay searchable until rewritten — and a hit that came from
    # them must open (observed live on PubMed after the 2026-08-26 re-pin)
    chunks = _chunks_from_fts(collection, row, book_id)
    source_store = "lexical index"
    if not chunks:
        chunks = _chunks_from_lance(collection, book_id)
        source_store = "vector store" if chunks else None
    origin = ORIGIN.get(row["source"])
    header = {
        "uid": row["uid"], "source": row["source"], "native_id": row["native_id"],
        "title": row.get("title") or f"#{row['native_id']}",
        "authors": row.get("authors") or "", "lang": row.get("lang") or "",
        "subjects": row.get("subjects") or "",
        "origin_url": origin(row) if origin else None,
        "reference": _reference_line(collection, row),
        "indexed": bool(row.get("fts_done")), "embedded": bool(row.get("embed_done")),
        "text_from": source_store,
    }
    return {"header": header, "chunks": chunks}


def _chunks_from_fts(collection, row: dict, book_id: int) -> list[dict]:
    try:
        raw = fts.book_chunks(fts.group_for(row.get("lang") or "en"), int(book_id), collection)
    except Exception:   # noqa: BLE001 — a missing index group is "not indexed", not an error
        return []
    return _ordered(raw)


def _chunks_from_lance(collection, book_id: int) -> list[dict]:
    try:
        conn = embeddings._conn(collection)
        if embeddings.TABLE not in conn.table_names():
            return []
        tbl = conn.open_table(embeddings.TABLE)
        rows = (tbl.search().where(embeddings._book_filter(book_id))
                .select(["id", "seq", "chapter", "text"]).limit(100_000).to_list())
    except Exception:   # noqa: BLE001
        return []
    return _ordered([{"chunk_id": r["id"], "seq": r.get("seq"), "chapter": r.get("chapter") or "",
                      "text": r.get("text") or ""} for r in rows])


def _seq_of(chunk: dict) -> int:
    if chunk.get("seq") is not None:
        try:
            return int(chunk["seq"])
        except (TypeError, ValueError):
            pass
    # chunk ids are {uid}:{seq}:{version}
    parts = str(chunk.get("chunk_id", "")).rsplit(":", 2)
    try:
        return int(parts[-2]) if len(parts) == 3 else 0
    except ValueError:
        return 0


def _ordered(chunks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in sorted(chunks, key=_seq_of):
        cid = c.get("chunk_id", "")
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"chunk_id": cid, "seq": _seq_of(c), "chapter": c.get("chapter") or "",
                    "text": c.get("text") or ""})
    return out


def _reference_line(collection, row: dict) -> str:
    """A bibliographic line a person can cite: authors, title, (year,
    journal for PubMed), language and source id."""
    bits = []
    if row.get("authors"):
        bits.append(row["authors"])
    bits.append(row.get("title") or f"#{row['native_id']}")
    if row["source"] == "pubmed":
        extra = _pubmed_extra(row)
        if extra.get("journal"):
            bits.append(extra["journal"])
        if extra.get("date"):
            bits.append(str(extra["date"]))
    if row.get("lang"):
        bits.append(f"({row['lang']})")
    bits.append(f"{row['source']}:{row['native_id']}")
    return " — ".join(bits)


def _pubmed_extra(row: dict) -> dict:
    try:
        from .plugins.pubmed import pack
        from .plugins.pubmed.source import split_locator
        container, index = split_locator(row["path"])
        if index is None or not Path(container).is_file():
            return {}
        return pack.record_fields(Path(container), index)
    except Exception:   # noqa: BLE001 — the line degrades to title/authors
        return {}
