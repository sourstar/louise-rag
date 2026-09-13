"""The dedup reaches retrieval: one work, one hit.

app/work_dedup.py decided WHICH editions compete; this is the wiring that
lets it see them. Three things had to exist for that:

  - `documents.work` — the author AGENT id, the LCSH heading set and
    whether the origin marked this edition a translation. An additive
    ALTER plus an rdf re-derive (`rdf_done=0`), never a rebuild: it is
    local manifest state, in no record, moving no hash.
  - gutenberg's parser filling it from the MARC relator roles.
  - the dedup running BEFORE the rerank, so the page stays full.

Operator, 2026-09-12: "if there is the original language version of a
book, do not consider the translated versions in the search ... otherwise
they just saturate the results returned."
"""
import json
import sqlite3

import pytest

from app import plugin_io, retrieval


class FakeCollection:
    name = "gutenberg"
    plugin = "gutenberg"


def _doc(con, native_id, lang, author, subjects, translated):
    con.execute(
        "INSERT INTO documents(uid, source, native_id, lang, work) VALUES (?,?,?,?,?)",
        (f"gutenberg:{native_id}", "gutenberg", str(native_id), lang,
         json.dumps({"author": author, "subjects": subjects,
                     "translated": translated})))


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, source TEXT, "
              "native_id TEXT, lang TEXT, work TEXT)")
    # Madame Bovary: the French original and three translations
    _doc(c, 2413, "fr", "agents/109", ["French fiction"], False)
    _doc(c, 2414, "en", "agents/109", ["French fiction"], True)
    _doc(c, 2415, "de", "agents/109", ["French fiction"], True)
    _doc(c, 2416, "es", "agents/109", ["French fiction"], True)
    return c


def _hits(*ids):
    return [{"book_id": i, "chunk_id": f"{i}:0", "text": "x"} for i in ids]


def test_the_original_wins_and_the_translations_go(con):
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2413, 2414, 2415, 2416), "madame bovary")
    assert [h["book_id"] for h in got] == [2413]


def test_order_is_untouched_for_what_survives(con):
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2416, 2413, 2414), "madame bovary")
    assert [h["book_id"] for h in got] == [2413]


def test_asking_for_a_translation_keeps_that_one(con):
    """"French translation of..." — that one, and the original it is a
    translation OF. The other twelve languages are the noise the rule
    exists to remove, even here."""
    got = retrieval._attach_work_fields(
        con, FakeCollection(), _hits(2413, 2414, 2415, 2416),
        "the German translation of Madame Bovary")
    assert sorted(h["book_id"] for h in got) == [2413, 2415]


def test_asking_about_the_translations_generally_keeps_them_all(con):
    """"how is the main character called in the various translations" —
    no language named, so every one of them is the answer."""
    got = retrieval._attach_work_fields(
        con, FakeCollection(), _hits(2413, 2414, 2415, 2416),
        "how is the main character called in the various translations of this book")
    assert len(got) == 4


def test_with_no_original_held_english_is_the_best_alternative(con):
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2414, 2415, 2416), "madame bovary")
    assert [h["book_id"] for h in got] == [2414]


def test_a_document_with_no_work_row_is_never_suppressed(con):
    """A guess about what a book IS must never cost a reader the book."""
    con.execute("INSERT INTO documents(uid, source, native_id, lang, work) "
                "VALUES ('gutenberg:99', 'gutenberg', '99', 'fr', NULL)")
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2413, 2414, 99), "madame bovary")
    assert sorted(h["book_id"] for h in got) == [99, 2413]


def test_an_unreadable_work_row_is_no_key_not_a_broken_page(con):
    con.execute("INSERT INTO documents(uid, source, native_id, lang, work) "
                "VALUES ('gutenberg:98', 'gutenberg', '98', 'fr', 'not json{')")
    got = retrieval._attach_work_fields(con, FakeCollection(), _hits(98), "x")
    assert [h["book_id"] for h in got] == [98]


def test_different_works_by_one_author_are_not_one_work(con):
    """The key is (author, LCSH set). Salammbô is also Flaubert and is not
    an edition of Madame Bovary."""
    _doc(con, 1290, "fr", "agents/109", ["Historical fiction"], False)
    _doc(con, 1291, "en", "agents/109", ["Historical fiction"], True)
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2413, 2414, 1290, 1291), "flaubert")
    assert sorted(h["book_id"] for h in got) == [1290, 2413]


def test_no_hits_costs_no_query(con):
    class Boom:
        def execute(self, *a):
            raise AssertionError("an empty hit list must not touch the manifest")
    assert retrieval._attach_work_fields(Boom(), FakeCollection(), [], "x") == []


# ---- what fills the column ----------------------------------------------

def test_work_identity_needs_both_halves():
    """Either missing yields no key, and no key never suppresses."""
    assert plugin_io.work_identity({"work_author": "", "work_subjects": ["A"]}) == ""
    assert plugin_io.work_identity({"work_author": "a/1", "work_subjects": []}) == ""


def test_work_identity_round_trips():
    got = json.loads(plugin_io.work_identity(
        {"work_author": "agents/109", "work_subjects": ["French fiction"],
         "translated": True}))
    assert got == {"author": "agents/109", "subjects": ["French fiction"],
                   "translated": True}


def test_a_plugin_that_says_nothing_gets_no_key():
    """pubmed, wikipedia: they name none of the three keys, and nothing
    about their hits changes."""
    assert plugin_io.work_identity(
        {"title": "t", "authors": "a", "lang": "en", "subjects": "s",
         "bookshelves": ""}) == ""


def test_a_manifest_without_the_work_column_still_searches(con):
    """`work` is added by an ALTER in `db.connect`, so a manifest opened
    some other way — an older process still running mid-deploy — has no
    such column. This is on the path of EVERY query: losing the dedup
    costs a reader some duplicate translations, raising costs them the
    search."""
    con.execute("DROP TABLE documents")
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, source TEXT, "
                "native_id TEXT, lang TEXT)")          # the pre-migration shape
    got = retrieval._attach_work_fields(con, FakeCollection(),
                                        _hits(2413, 2414), "madame bovary")
    assert [h["book_id"] for h in got] == [2413, 2414], "every hit, never none"
