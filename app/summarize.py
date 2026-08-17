"""Map-reduce book summaries, cached in the manifest DB.

P0.1 runs a single (Gutenberg) collection; book_id stays the public int
identifier (native_id), resolved to a uid internally via the default
collection. Multi-collection summary lookups are P0.5's job.
"""
import json
import time
from pathlib import Path
from . import collections as collections_mod, config, db, llm, plugin_io

MAP_PROMPT = (
    "Summarize this chapter of the book \"{title}\" in 4-6 sentences, in English, "
    "preserving key events, arguments, entities and places.\n\nChapter: {chapter}\n\n{text}")
REDUCE_PROMPT = (
    "You are given per-chapter summaries of the book \"{title}\" by {authors}. "
    "Write a coherent overall summary (300-500 words, in English): plot or main "
    "argument, structure, notable themes, and why the book matters.\n\n{parts}")

def _uid(book_id: int) -> str:
    return f"{collections_mod.default_collection().plugin}:{book_id}"

def get_cached(con, book_id: int):
    row = con.execute("SELECT * FROM summaries WHERE uid=?", (_uid(book_id),)).fetchone()
    return dict(row) if row else None

def summarize_book(con, book_id: int, force: bool = False) -> dict:
    if not force:
        cached = get_cached(con, book_id)
        if cached:
            return cached
    uid = _uid(book_id)
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    if row is None:
        raise ValueError(f"book {book_id} not in manifest")
    plugin = collections_mod.default_collection().plugin_instance()
    text = plugin_io.full_text(plugin, row)
    title = row["title"] or f"Book #{book_id}"
    authors = row["authors"] or "unknown"

    # small books: single pass
    if len(text) < 45_000:
        summary = llm.chat([{"role": "user", "content": REDUCE_PROMPT.format(
            title=title, authors=authors, parts=text[:45_000])}], max_tokens=1100)
        chapter_summaries = []
    else:
        chapters = plugin.split_text(text)
        chapter_summaries = []
        for ch_title, body in chapters[:120]:
            s = llm.chat([{"role": "user", "content": MAP_PROMPT.format(
                title=title, chapter=ch_title, text=body[:24_000])}], max_tokens=400)
            chapter_summaries.append({"chapter": ch_title, "summary": s})
        parts = "\n\n".join(f"{c['chapter']}: {c['summary']}" for c in chapter_summaries)
        summary = llm.chat([{"role": "user", "content": REDUCE_PROMPT.format(
            title=title, authors=authors, parts=parts[:48_000])}], max_tokens=1100)

    con.execute(
        "INSERT INTO summaries(uid,model,created_at,summary,chapter_summaries) "
        "VALUES(?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET model=excluded.model,"
        "created_at=excluded.created_at,summary=excluded.summary,"
        "chapter_summaries=excluded.chapter_summaries",
        (uid, config.CHAT_MODEL, int(time.time()), summary,
         json.dumps(chapter_summaries)))
    con.execute("UPDATE documents SET summary_done=1 WHERE uid=?", (uid,))
    db.log_event(con, "info", "summary", f"summarized '{title}'", uid)
    return get_cached(con, book_id)
