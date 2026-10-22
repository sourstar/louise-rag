"""Unindexable documents must STAY unindexable — never bounce into
'blocked'.

Observed live on gutenberg: 1,111 entries whose mirror ships no text file
(epub/mobi/covers only) were correctly counted 'not indexable' — until the
operator pressed "clear errors & retry". The reset cleared discovery's
corpus-fact error along with the stage errors, the pipeline re-ran the
documents, fts failed with 'fts: no txt file' — a STAGE-prefixed error the
Library counts as BLOCKED — and discovery could not relabel them back
because their directories had not changed (cursor skip). Every press of
the button converted the whole set again.

Two rules pinned here:
1. "clear errors & retry" clears STAGE errors only; an unprefixed error is
   a corpus fact retrying cannot change.
2. A stage that discovers a corpus fact mid-run (plugins raise
   UnindexableDocument) records it UNPREFIXED, exactly as discovery does —
   so even a row that somehow reaches a stage settles back into the
   'not indexable' bucket instead of oscillating.
"""
import shutil
from pathlib import Path

from app import collections as collections_mod, db, discovery, web, worker

FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "gutenberg-mini" / "corpus"


def _collection(tmp_path):
    corpus = tmp_path / "corpus_g"      # isolated_config owns tmp_path/corpus
    shutil.copytree(FIXTURE_CORPUS, corpus)
    # one entry with files but NO text — the real shape of the 1,111
    nodir = corpus / "99999"
    nodir.mkdir()
    # NOT an .rdf: since 2026-09-11 the reading rule falls back to the
    # .rdf as a catalogue record, so a metadata-only book is readable. The
    # corpus fact this test is about is a directory with files and nothing
    # READABLE in any form — the 33 that hold only a converter log.
    (nodir / "pg99999.converter.log").write_text("failed", encoding="utf-8")
    c = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "g" / "manifest.db",
        fts_dir=tmp_path / "g" / "fts", lance_dir=tmp_path / "g" / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    return c, con


def _bucket(collection):
    web.invalidate_counts()
    counts = web._document_counts_query(collection)
    return counts["blocked"], counts["unindexable"]


def test_clear_errors_never_touches_corpus_facts(tmp_path, isolated_config):
    c, con = _collection(tmp_path)
    row = con.execute("SELECT error FROM documents WHERE uid='gutenberg:99999'").fetchone()
    assert row["error"] == "no epub, txt or rdf"   # discovery corpus fact
    # and a genuine stage error beside it
    con.execute("UPDATE documents SET error='fts: boom', retries=1 "
                "WHERE uid=(SELECT uid FROM documents WHERE error IS NULL LIMIT 1)")
    assert _bucket(c) == (1, 1)

    db.apply_reset(con, "errors", [])
    rows = {r["uid"]: r["error"] for r in con.execute(
        "SELECT uid, error FROM documents WHERE error IS NOT NULL")}
    assert rows == {"gutenberg:99999": "no epub, txt or rdf"}, \
        "the corpus fact must survive; the stage error must clear"
    assert _bucket(c) == (0, 1)


def test_a_stage_records_a_corpus_fact_unprefixed(tmp_path, isolated_config):
    """The healing path for rows already mislabeled: clear them once, let
    the stage re-run — it must settle them back into 'not indexable',
    never re-block them."""
    c, con = _collection(tmp_path)
    # the mislabeled shape observed live: stage-prefixed, so 'blocked'
    con.execute("UPDATE documents SET error='fts: no epub, txt or rdf for book 99999', "
                "retries=1, rdf_done=1 WHERE uid='gutenberg:99999'")
    assert _bucket(c) == (1, 0)

    db.apply_reset(con, "errors", [])             # clears it (stage-prefixed)
    row = con.execute("SELECT * FROM documents WHERE uid='gutenberg:99999'").fetchone()
    assert worker._execute_step(con, c, "fts", row) is True
    row = con.execute("SELECT error, retries FROM documents "
                      "WHERE uid='gutenberg:99999'").fetchone()
    assert row["error"] == "no epub, txt or rdf for book 99999", \
        "the stage must record the corpus fact unprefixed"
    assert row["retries"] == 0, "a corpus fact burns no retries"
    assert _bucket(c) == (0, 1)

    # and the cycle is CLOSED: another clear+count leaves it where it is
    db.apply_reset(con, "errors", [])
    row = con.execute("SELECT error FROM documents WHERE uid='gutenberg:99999'").fetchone()
    assert row["error"] == "no epub, txt or rdf for book 99999"


def _indexed_as_if_earlier(con, c, uid="gutenberg:99999"):
    """The pg673 shape (2026-10-22): a document indexed before its origin
    declared it not indexable — lexical chunks and a catalog row present."""
    from app import catalog, fts
    row = con.execute("SELECT rowid, native_id FROM documents WHERE uid=?", (uid,)).fetchone()
    con.execute("UPDATE documents SET error=NULL, lang='en', title='Webster markup', "
                "rdf_done=1 WHERE uid=?", (uid,))
    con.commit()
    fts.index_book("en", int(row["native_id"]), [
        {"id": f"{uid}:0:1", "chapter": None, "text": "<hw>A*ban\"don</hw> <def>To cast out</def>"}], c)
    catalog.backfill(con, c, budget_s=60)
    return int(row["native_id"])


def test_a_stage_that_meets_a_corpus_fact_withdraws_the_document_from_every_index(
        tmp_path, isolated_config):
    from app import catalog, fts
    c, con = _collection(tmp_path)
    book = _indexed_as_if_earlier(con, c)
    assert len(fts.book_chunks("en", book, c)) == 1
    assert [h["book_id"] for h in catalog.search(con, c, "Webster markup", 10)] == [book]
    row = con.execute("SELECT * FROM documents WHERE uid='gutenberg:99999'").fetchone()
    assert worker._execute_step(con, c, "fts", row) is True
    assert con.execute("SELECT error FROM documents WHERE uid='gutenberg:99999'").fetchone()[0] \
        == "no epub, txt or rdf for book 99999"
    assert fts.book_chunks("en", book, c) == [], "the lexical leg no longer holds it"
    assert catalog.search(con, c, "Webster markup", 10) == [], "nor the catalog leg"


def test_an_index_that_cannot_be_written_defers_and_records_nothing(
        tmp_path, isolated_config, monkeypatch):
    from app import unindexable
    c, con = _collection(tmp_path)
    _indexed_as_if_earlier(con, c)

    def busy(*a, **k):
        raise RuntimeError("index busy")
    monkeypatch.setattr(unindexable, "withdraw", busy)
    row = con.execute("SELECT * FROM documents WHERE uid='gutenberg:99999'").fetchone()
    assert worker._execute_step(con, c, "fts", row) is False
    assert con.execute("SELECT error FROM documents WHERE uid='gutenberg:99999'").fetchone()[0] is None


def test_the_command_withdraws_at_once_and_refuses_a_document_that_parses(
        tmp_path, isolated_config, monkeypatch, capsys):
    import json as _json
    from app import fts, louise
    from app.hold import collection as col, records as rec
    c, con = _collection(tmp_path)
    book = _indexed_as_if_earlier(con, c)
    monkeypatch.setattr(col, "resolve", lambda root, ref: ("g", None))
    monkeypatch.setattr(rec, "local_collection", lambda root, name, codex: c)
    monkeypatch.setattr(louise, "_take_write_lock", lambda *a, **k: None)
    assert louise.main(["dev", "unindexable", "g", "gutenberg:99999"]) == 0
    assert len(fts.book_chunks("en", book, c)) == 1, "a dry run writes nothing"
    assert louise.main(["dev", "unindexable", "g", "gutenberg:99999", "--yes"]) == 0
    printed = capsys.readouterr().out
    out = _json.loads(printed[printed.rindex("\n{"):])
    assert out["withdrawn"] and out["error"] == "no epub, txt or rdf for book 99999"
    assert fts.book_chunks("en", book, c) == []
    assert con.execute("SELECT error, embed_done FROM documents WHERE uid='gutenberg:99999'"
                       ).fetchone()[:] == ("no epub, txt or rdf for book 99999", 0)
    readable = con.execute("SELECT uid FROM documents WHERE error IS NULL LIMIT 1").fetchone()[0]
    capsys.readouterr()
    assert louise.main(["dev", "unindexable", "g", readable, "--yes"]) != 0
    assert "parses under this image" in capsys.readouterr().err
    assert con.execute("SELECT error FROM documents WHERE uid=?", (readable,)).fetchone()[0] is None


def test_a_withdrawn_book_comes_back_by_itself_when_its_origin_drops_the_note(
        tmp_path, isolated_config):
    """READMISSION (operator, 2026-10-22): a book withdrawn because its
    origin declared it broken returns with no human step once a release
    drops the note — the RDF is inside `origin_hash` since parser 9, so
    discovery sees the change, clears the corpus-fact error and resets the
    stage marks, and the ordinary stages index it again. The stages read the
    text through `read_chapters`, not `parse`: the refusal has to be there."""
    import os
    from app import catalog, fts
    c, con = _collection(tmp_path)
    uid, book = "gutenberg:10001", 10001
    rdf = c.corpus_dir / "10001" / "pg10001.rdf"
    clean = rdf.read_text(encoding="utf-8")

    def stages():
        for stage in ("rdf", "fts"):
            row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
            if row["error"] is None:
                worker._execute_step(con, c, stage, row)

    def lands(text, bump):
        # as a release lands: staged, then renamed into the book's directory
        # (the scan's signature is the directory's mtime, source.py)
        tmp = rdf.with_suffix(".part")
        tmp.write_text(text, encoding="utf-8")
        st = rdf.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime + bump))
        os.replace(tmp, rdf)
        d = rdf.parent.stat()
        os.utime(rdf.parent, (d.st_atime, d.st_mtime + bump))
        discovery.sync_collection(con, c)

    def state():
        err = con.execute("SELECT error FROM documents WHERE uid=?", (uid,)).fetchone()[0]
        listed = [h["book_id"] for h in catalog.search(con, c, "Study Fixtures", 10) or []]
        return err, len(fts.book_chunks("en", book, c)), book in listed

    stages()
    catalog.backfill(con, c, budget_s=60)
    assert state() == (None, state()[1], True) and state()[1] > 0, "indexed as usual"
    lands(clean.replace("</pgterms:ebook>", "<dcterms:description>This book's encoding is broken, "
                        "and will not be fixed.</dcterms:description></pgterms:ebook>", 1), 10)
    stages()
    error, chunks, listed = state()
    assert error and "encoding broken" in error, error
    assert (chunks, listed) == (0, False), "withdrawn from the lexical and catalog legs"
    lands(clean, 20)
    assert state()[0] is None, "the note gone: discovery clears the corpus fact by itself"
    stages()
    error, chunks, listed = state()
    assert error is None and chunks > 0 and listed, "and the ordinary stages index it again"
