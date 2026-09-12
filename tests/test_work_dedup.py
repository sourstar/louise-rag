"""One work, one hit — and the cases where that rule must not fire.

Operator, 2026-09-12: a famous novel and its translations are separate
Gutenberg books, so a search returns it five times and "they just
saturate the results returned".
"""
import pytest

from app import work_dedup


def hit(title, lang, *, author="a:1", subjects=("Fiction",), translated=False):
    return {"title": title, "lang": lang, "work_author": author,
            "work_subjects": subjects, "translated": translated}


# ---------------- is the query about the translations? ----------------

@pytest.mark.parametrize("query", [
    "is this book translated in this language",
    "how is the main character called in the various translations of the book",
    "is the fidelity of the original text kept in the translation in this language",
    "who was the translator of Madame Bovary",
    "quale traduzione italiana è più fedele",
    "gibt es eine deutsche Übersetzung",
    "各種翻訳での主人公の名前は",
])
def test_a_query_about_translations_keeps_them(query):
    """The operator's own examples, plus the same question in the
    languages the picker offers."""
    assert work_dedup.mentions_translation(query) is True


@pytest.mark.parametrize("query", [
    "the whale and the sea",
    "chi è il protagonista di Moby Dick",
    "was passiert am Ende des Romans",
    "",
])
def test_an_ordinary_query_does_not(query):
    """Including one merely WRITTEN in another language — the trigger
    that would misfire on every short foreign query."""
    assert work_dedup.mentions_translation(query) is False


def test_a_word_that_merely_contains_a_term_does_not_trip_it():
    assert work_dedup.mentions_translation("translationese in poetry") is False
    assert work_dedup.mentions_translation("Mr Translator-Smith's diary") is True, \
        "a hyphen is a word boundary; this really does say translator"


# ---------------- the suppression ----------------

def test_the_original_wins_and_the_translations_step_aside():
    hits = [hit("Titus Andronicus", "en"),
            hit("Titus Andronicus", "nl", translated=True),
            hit("Titus Andronicus", "fi", translated=True)]
    got = work_dedup.prefer_originals(hits, "titus andronicus")
    assert [h["lang"] for h in got] == ["en"]


def test_english_is_the_best_alternative_when_the_original_is_absent():
    hits = [hit("Dorian Gray", "nl", translated=True),
            hit("Dorian Gray", "en", translated=True),
            hit("Dorian Gray", "fi", translated=True)]
    got = work_dedup.prefer_originals(hits, "dorian gray")
    assert [h["lang"] for h in got] == ["en"]


def test_with_neither_an_original_nor_english_nothing_is_hidden():
    """A reader is left with what exists rather than with nothing."""
    hits = [hit("Dorian Gray", "nl", translated=True),
            hit("Dorian Gray", "fi", translated=True)]
    got = work_dedup.prefer_originals(hits, "dorian gray")
    assert len(got) == 2


def test_a_query_about_translations_suppresses_nothing():
    hits = [hit("Titus Andronicus", "en"),
            hit("Titus Andronicus", "nl", translated=True)]
    got = work_dedup.prefer_originals(
        hits, "how is the main character called in the various translations")
    assert len(got) == 2


# ---------------- what must NOT be suppressed ----------------

def test_a_periodicals_issues_all_survive():
    """THE CASE THAT SHAPED THE RULE. Measured over ~6,000 real books,
    (author, LCSH set) groups genuine translation sets correctly — and
    also lumps fifteen issues of Chambers's Journal into one "work",
    because a periodical's issues share an author and a subject heading.
    Every issue is an ORIGINAL, so requiring an explicit translator
    credit leaves them all standing: the weak key can only ever hide
    something the origin itself called a translation."""
    issues = [hit(f"Chambers's Journal, No. {n}", "en") for n in range(1, 16)]
    issues.append(hit("Vasárnapi Könyv 1914", "hu"))   # swept in by the key
    got = work_dedup.prefer_originals(issues, "chambers journal")
    assert len(got) == 16, "nothing here is marked a translation, so nothing goes"


def test_a_hit_missing_the_work_fields_is_never_suppressed():
    """A guess about what a book IS must not cost a reader the book."""
    hits = [hit("Titus Andronicus", "en"),
            {"title": "Titus Andronicus", "lang": "nl", "translated": True}]
    assert len(work_dedup.prefer_originals(hits, "titus")) == 2

    no_subjects = [hit("X", "en"), hit("X", "nl", subjects=(), translated=True)]
    assert len(work_dedup.prefer_originals(no_subjects, "x")) == 2


def test_two_different_works_by_one_author_do_not_group():
    hits = [hit("Book One", "en", subjects=("Adventure",)),
            hit("Book Two", "de", subjects=("Poetry",), translated=True)]
    assert len(work_dedup.prefer_originals(hits, "book")) == 2


def test_order_and_surviving_hits_are_untouched():
    hits = [hit("Other", "en", author="a:2", subjects=("Science",)),
            hit("Titus", "en"),
            hit("Titus", "nl", translated=True),
            hit("Third", "en", author="a:3", subjects=("History",))]
    got = work_dedup.prefer_originals(hits, "anything")
    assert [h["title"] for h in got] == ["Other", "Titus", "Third"]
    assert got[1] is hits[1], "a surviving hit is the same object, unmodified"


def test_an_empty_result_set_is_fine():
    assert work_dedup.prefer_originals([], "q") == []


# ---------------- "French translation of ..." ----------------

def test_naming_a_language_narrows_to_that_translation():
    """Operator, 2026-09-12: "Or of course 'French translation of…'". It
    already counted as a query about translations; what it also wants is
    the FRENCH one, not all thirteen."""
    hits = [hit("Madame Bovary", "fr"),
            hit("Madame Bovary", "en", translated=True),
            hit("Madame Bovary", "de", translated=True),
            hit("Madame Bovary", "it", translated=True)]
    got = work_dedup.prefer_originals(hits, "English translation of Madame Bovary")
    assert [h["lang"] for h in got] == ["fr", "en"], \
        "the asked-for translation, and the original it translates"


def test_two_named_languages_are_a_comparison():
    hits = [hit("Faust", "de"),
            hit("Faust", "en", translated=True),
            hit("Faust", "fr", translated=True)]
    got = work_dedup.prefer_originals(
        hits, "compare the English and French translations of Faust")
    assert len(got) == 3, "neither is preferred; the reader wanted both"


def test_a_language_named_without_translation_is_not_a_translation_query():
    """"books in French" is a scope, not a question about translations,
    so the ordinary rule applies and the original still wins."""
    assert work_dedup.named_language("French novels about the sea") == "fr"
    hits = [hit("Titus", "en"), hit("Titus", "nl", translated=True)]
    assert len(work_dedup.prefer_originals(hits, "French novels about the sea")) == 1


@pytest.mark.parametrize("query,code", [
    ("French translation of Madame Bovary", "fr"),
    ("la traduzione italiana", "it"),
    ("die deutsche Übersetzung", "de"),
    ("is there a Japanese translation", "ja"),
    ("how is it translated", None),
    ("", None),
])
def test_named_language_reads_the_name(query, code):
    assert work_dedup.named_language(query) == code


def test_an_unnamed_languages_books_are_never_hidden_by_the_narrowing():
    """The name table does not carry every language's adjective for every
    other language. A miss costs only the narrowing, never the books:
    the query still counts as being about translations, so all of them
    show."""
    hits = [hit("X", "de"), hit("X", "fr", translated=True),
            hit("X", "en", translated=True)]
    got = work_dedup.prefer_originals(hits, "traduzione francese di X")
    assert len(got) == 3, "no single language recognised, so nothing is dropped"
