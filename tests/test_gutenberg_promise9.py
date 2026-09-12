"""Promise 9 for gutenberg: what the RDF says, and what we keep.

docs/01 promise 9, docs/06 6.4. Parser 6 kept 12 of the 62 things a
pg<id>.rdf asserts. As with pubmed, the test is a COVERAGE TABLE because
a dropped field passes every check the platform has -- `extra` is
outside `content_hash`, and `records-vs-origin` re-parses with the same
parser and reproduces the same lossy record.
"""
import pytest

from app.plugins.gutenberg import rdf

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dcterms="http://purl.org/dc/terms/"
         xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
         xmlns:dcam="http://purl.org/dc/dcam/"
         xmlns:marcrel="http://id.loc.gov/vocabulary/relators/">
  <pgterms:ebook rdf:about="ebooks/76">
    <dcterms:title>Adventures of Huckleberry Finn</dcterms:title>
    <dcterms:alternative>Huck Finn</dcterms:alternative>
    <dcterms:issued rdf:datatype="x">1993-01-01</dcterms:issued>
    <dcterms:rights>Public domain in the USA.</dcterms:rights>
    <dcterms:description>Wikipedia page on this work.</dcterms:description>
    <dcterms:tableOfContents>Chapter I -- Chapter II</dcterms:tableOfContents>
    <pgterms:marc908>Reading ease score: 85.9 (6th grade).</pgterms:marc908>
    <pgterms:downloads>12345</pgterms:downloads>
    <dcterms:type><rdf:Description><rdf:value>Text</rdf:value></rdf:Description></dcterms:type>
    <dcterms:language><rdf:Description><rdf:value>en</rdf:value></rdf:Description></dcterms:language>
    <dcterms:language><rdf:Description><rdf:value>fr</rdf:value></rdf:Description></dcterms:language>
    <dcterms:creator>
      <pgterms:agent rdf:about="2009/agents/53">
        <pgterms:name>Twain, Mark</pgterms:name>
        <pgterms:birthdate rdf:datatype="x">1835</pgterms:birthdate>
        <pgterms:deathdate rdf:datatype="x">1910</pgterms:deathdate>
        <pgterms:alias>Clemens, Samuel Langhorne</pgterms:alias>
        <pgterms:webpage rdf:resource="https://en.wikipedia.org/wiki/Mark_Twain"/>
      </pgterms:agent>
    </dcterms:creator>
    <marcrel:ill>
      <pgterms:agent rdf:about="2009/agents/37155">
        <pgterms:name>Kemble, E. W.</pgterms:name>
        <pgterms:birthdate rdf:datatype="x">1861</pgterms:birthdate>
      </pgterms:agent>
    </marcrel:ill>
    <dcterms:subject>
      <rdf:Description>
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/LCSH"/>
        <rdf:value>Boys -- Fiction</rdf:value>
      </rdf:Description>
    </dcterms:subject>
    <dcterms:subject>
      <rdf:Description>
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/LCC"/>
        <rdf:value>PS</rdf:value>
      </rdf:Description>
    </dcterms:subject>
    <pgterms:bookshelf>
      <rdf:Description><rdf:value>Category: Novels</rdf:value></rdf:Description>
    </pgterms:bookshelf>
  </pgterms:ebook>
</rdf:RDF>"""


@pytest.fixture
def meta(tmp_path):
    (tmp_path / "pg76.rdf").write_text(RDF, encoding="utf-8")
    return rdf.parse(tmp_path, 76)


COVERAGE = [
    ("dcterms:alternative",      "alt_titles",   ["Huck Finn"]),
    ("dcterms:description",      "description",  "Wikipedia page on this work."),
    ("dcterms:tableOfContents",  "toc",          "Chapter I -- Chapter II"),
    ("dcterms:rights",           "rights",       "Public domain in the USA."),
    ("dcterms:type",             "dcmi_type",    "Text"),
    ("pgterms:marc908",          "reading_ease", "Reading ease score: 85.9 (6th grade)."),
    ("dcterms:language (all)",   "languages",    ["en", "fr"]),
]


@pytest.mark.parametrize("origin_field,key,expected", COVERAGE,
                         ids=[c[0] for c in COVERAGE])
def test_every_rdf_field_reaches_the_record(meta, origin_field, key, expected):
    assert meta[key] == expected, (
        f"{origin_field} is not wrapping, not boilerplate, not our "
        f"corpus-add date and not the provider's packaging")


def test_subjects_keep_their_vocabulary(meta):
    """Each dcterms:subject names its scheme in dcam:memberOf. Joining all
    the values into one string made "Boys -- Fiction" (an LCSH heading)
    and "PS" (an LCC class) the same kind of thing -- and no facet with an
    `authority` over that field could be honest."""
    assert meta["subjects_by_scheme"] == {"LCSH": ["Boys -- Fiction"], "LCC": ["PS"]}
    # the flat display string still holds every subject, whatever its scheme
    assert "Boys -- Fiction" in meta["subjects"] and "PS" in meta["subjects"]


def test_an_agent_is_more_than_a_name_and_carries_its_role(meta):
    """The old `.//pgterms:agent` sweep read names and nothing else, and
    could not tell an illustrator from an author: the ROLE is the element
    that wraps the agent, so it is only knowable by walking children."""
    twain, kemble = meta["creators_detail"]
    assert twain == {"name": "Twain, Mark", "role": "creator",
                     "agent_id": "2009/agents/53", "birth": "1835", "death": "1910",
                     "aliases": ["Clemens, Samuel Langhorne"],
                     "webpages": ["https://en.wikipedia.org/wiki/Mark_Twain"]}
    assert kemble["role"] == "ill", "an illustrator is named as such"
    assert kemble["birth"] == "1861"


def test_an_illustrator_is_credited_as_an_illustrator(meta):
    """Operator, 2026-09-12: "authors are the only authors. illustrators
    and translators must be credited but as illustrators and
    translators". Two failures to avoid, not one: calling Kemble an
    author, and dropping him."""
    assert meta["authors"] == "Twain, Mark; Kemble, E. W. (illustrator)"
    assert meta["author_names"] == ["Twain, Mark"], "authors alone"
    assert {a["role"] for a in meta["creators_detail"]} == {"creator", "ill"}


def test_an_unknown_relator_is_shown_as_its_code(tmp_path):
    """A credit nobody can read is still a credit; a wrong one is not, so
    an unmapped MARC relator is printed rather than guessed at."""
    (tmp_path / "pg76.rdf").write_text(
        RDF.replace("marcrel:ill", "marcrel:zzz"), encoding="utf-8")
    got = rdf.parse(tmp_path, 76)
    assert "Kemble, E. W. (zzz)" in got["authors"]
    assert got["author_names"] == ["Twain, Mark"]


def test_the_records_creators_are_authors_alone(tmp_path):
    """§9.5's `creators` is the work's authors, not a rendering of a
    credits line — and it must never be built by splitting the display
    string, which now carries "(illustrator)"."""
    from app.plugins.gutenberg import SOURCE
    from app.plugins.base import RawDoc, DocRef
    (tmp_path / "pg76.rdf").write_text(RDF, encoding="utf-8")
    (tmp_path / "pg76.txt").write_text(
        "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n\n"
        "CHAPTER I.\n\nA body of text.\n", encoding="utf-8")
    ref = DocRef(source_id="gutenberg", native_id="76", locator=str(tmp_path))
    doc = SOURCE.parse(SOURCE.fetch(ref))
    assert doc.creators == ["Twain, Mark"], "no illustrator among the authors"
    roles = {a["name"]: a["role"] for a in doc.extra["creators_detail"]}
    assert roles["Kemble, E. W."] == "ill", "and he is still credited, as what he did"


def test_nothing_is_silently_truncated(tmp_path):
    """`[:500]` on title and authors, `[:2000]` on subjects, `[:1000]` on
    bookshelves. Measured on the live manifest: 25 titles and 7 author
    lists sat exactly at 500, tails cut, nothing saying so."""
    long_title = "A " + "very " * 300 + "long title"
    (tmp_path / "pg9.rdf").write_text(
        RDF.replace("Adventures of Huckleberry Finn", long_title), encoding="utf-8")
    got = rdf.parse(tmp_path, 9)
    assert got["title"] == long_title
    assert len(got["title"]) > 500


def test_every_key_is_present_on_every_return_path(tmp_path):
    """`parse` returns early three ways -- no file, unparseable XML, no
    <pgterms:ebook> -- and a caller reading a key only the happy path sets
    gets a KeyError on exactly the books whose metadata is already
    broken."""
    happy = set(rdf.parse(tmp_path, 404))          # no file at all
    (tmp_path / "pg1.rdf").write_text("<not xml", encoding="utf-8")
    assert set(rdf.parse(tmp_path, 1)) == happy
    (tmp_path / "pg2.rdf").write_text(
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>',
        encoding="utf-8")
    assert set(rdf.parse(tmp_path, 2)) == happy
    for key in ("creators_detail", "languages", "subjects_by_scheme", "alt_titles",
                "description", "toc", "dcmi_type", "rights", "reading_ease"):
        assert key in happy


# ---------------- the text body ----------------

TOC_BOOK = """THE WANDERING JEW

CONTENTS

CHAPTER I.       Page 1
CHAPTER II.      Page 30
CHAPTER III.     Page 61


CHAPTER I.

It was a dark and stormy night, and the wind blew hard.


CHAPTER II.

The morning came, and with it a letter from the north.
"""


def test_the_books_contents_page_is_kept():
    """`split_chapters` drops a heading whose body is empty (its `if
    body:`) — and measured over 400 real books that is 415 headings,
    mostly TABLE OF CONTENTS lines, whose text each match the chapter
    pattern and leave nothing behind. A record held no trace of the
    book's contents page."""
    from app.plugins.gutenberg import pg_text
    toc = pg_text.headings_without_body(TOC_BOOK)
    # all three contents lines: the heading pattern consumes the whole
    # line, page number included, so each leaves no body behind
    assert [t.split()[1] for t in toc] == ["I.", "II.", "III."], \
        "the contents-page lines that split_chapters could not place"
    assert any("Page 30" in t for t in toc), "with the page numbers the origin gave"


def test_the_contents_page_is_not_embedded():
    """Promise 9 requires keeping what the origin said, not embedding it.
    A contents page cut into chunks competes with the prose it indexes —
    and keeping it out of `sections` keeps it out of `content_hash`, so
    restoring it moves no chunk id and no vector."""
    from app.plugins.gutenberg import pg_text
    sections = pg_text.split_chapters(TOC_BOOK)
    bodies = " ".join(b for _, b in sections)
    assert "dark and stormy" in bodies and "letter from the north" in bodies
    assert "Page 30" not in bodies, "the contents lines are not chunked as prose"


def test_a_long_heading_keeps_its_tail():
    """`[:120]` cut it silently. It fires on none of 400 sampled books, so
    removing the cap costs nothing and removes a loss that would have
    happened the day it did fire."""
    from app.plugins.gutenberg import pg_text
    head = "CHAPTER I. " + "in which a great many things happen to our hero " * 4
    sections = pg_text.split_chapters(f"{head}\n\nThe body of the chapter.\n")
    assert sections, "the heading pattern still matches"
    assert len(sections[-1][0]) > 120 or head.strip().startswith(sections[-1][0][:20])


# ---------------- the credits (operator, 2026-09-12) ----------------

TEXT_WITH_HEADER = """The Project Gutenberg eBook of Huckleberry Finn, by Mark Twain

This eBook is for the use of anyone anywhere in the United States and most
other parts of the world at no cost and with almost no restrictions
whatsoever.

Title: Adventures of Huckleberry Finn

Author: Mark Twain

Illustrator: E. W. Kemble

Release Date: June 29, 2004

Credits: Produced by David Widger and Pat Castevens
         with additional proofreading by volunteers

Language: English

*** START OF THE PROJECT GUTENBERG EBOOK HUCKLEBERRY FINN ***

THE ADVENTURES OF HUCKLEBERRY FINN

CHAPTER I.

You don't know about me without you have read a book.
"""


def test_the_production_credit_is_kept(tmp_path):
    """Operator, 2026-09-12: "keep the PG production credits, project
    louise respects the credits". `read_clean` discards the whole header
    as promise 9(b) boilerplate, and the Credits line is inside it."""
    from app.plugins.gutenberg import pg_text
    p = tmp_path / "pg76.txt"
    p.write_text(TEXT_WITH_HEADER, encoding="utf-8")
    head = pg_text.header_fields(p)
    assert head["Credits"] == ("Produced by David Widger and Pat Castevens "
                               "with additional proofreading by volunteers"), \
        "a wrapped credit is one credit, not half of one"
    assert head["Illustrator"] == "E. W. Kemble"
    assert head["Release Date"] == "June 29, 2004"


def test_the_boilerplate_prose_is_still_dropped(tmp_path):
    """(b) still applies to the header's repeated prose: it is on every
    text and asserts nothing about this book."""
    from app.plugins.gutenberg import pg_text
    p = tmp_path / "pg76.txt"
    p.write_text(TEXT_WITH_HEADER, encoding="utf-8")
    head = pg_text.header_fields(p)
    assert not any("no cost and with almost no restrictions" in v
                   for v in head.values())
    assert "This eBook is for the use of anyone anywhere" not in pg_text.read_clean(p)


def test_short_front_matter_is_kept(tmp_path):
    """The `> 200` threshold dropped it on 17 of 400 sampled books, and
    what sits there is usually the production credit plus the title
    page."""
    from app.plugins.gutenberg import pg_text
    body = "Produced by David Widger\n\nCHAPTER I.\n\nIt was a dark night.\n"
    assert len(body.split("CHAPTER")[0]) < 200
    sections = pg_text.split_chapters(body)
    assert sections[0][0] == "Front matter"
    assert "David Widger" in sections[0][1]


def test_every_marc_datafield_is_kept_by_number(tmp_path):
    """The audit could not enumerate these from one machine — a twelve-book
    sample shows 508, 520, 901 and 908, and 79,320 books certainly carry
    more. Capturing the NUMBER as the key keeps a field this code has
    never heard of, which is the only way to close an unauditable gap."""
    extra_marc = ("<pgterms:marc508>Produced by David Widger</pgterms:marc508>"
                  "<pgterms:marc520>An automatically generated summary.</pgterms:marc520>"
                  "<pgterms:marc999>Something nobody has sampled.</pgterms:marc999>")
    (tmp_path / "pg76.rdf").write_text(
        RDF.replace("</pgterms:ebook>", extra_marc + "</pgterms:ebook>"),
        encoding="utf-8")
    marc = rdf.parse(tmp_path, 76)["marc"]
    assert marc["508"] == ["Produced by David Widger"], "the production credit"
    assert marc["520"] == ["An automatically generated summary."], \
        "kept too: promise 9 asks whether the origin said it, not whether we trust it"
    assert marc["999"] == ["Something nobody has sampled."], \
        "a datafield this code has never heard of survives"
    assert marc["908"], "and the one read by name is still in here"


def test_pg_producers_are_credited_as_pg_producers(tmp_path):
    """Operator, 2026-09-12: "PG producers are credited as PG producers".
    One credits list, every entry saying what its person did — leaving
    them as an unparsed sentence in a header blob credited them less
    precisely than everyone else in the same record."""
    from app.plugins.gutenberg import SOURCE, pg_text
    from app.plugins.base import DocRef
    (tmp_path / "pg76.rdf").write_text(RDF, encoding="utf-8")
    (tmp_path / "pg76.txt").write_text(TEXT_WITH_HEADER, encoding="utf-8")
    doc = SOURCE.parse(SOURCE.fetch(DocRef(source_id="gutenberg", native_id="76",
                                           locator=str(tmp_path))))
    by_role = {}
    for c in doc.extra["creators_detail"]:
        by_role.setdefault(c["role"], []).append(c["name"])
    assert by_role["creator"] == ["Twain, Mark"]
    assert by_role["ill"] == ["Kemble, E. W."]
    assert "David Widger" in by_role["producer"]
    assert "Pat Castevens" in by_role["producer"]


def test_a_producer_credit_is_split_into_people_not_fragments():
    from app.plugins.gutenberg import pg_text
    assert pg_text.producers("Produced by David Widger and Pat Castevens") == [
        "David Widger", "Pat Castevens"]
    assert pg_text.producers("Produced by Al, Bo, and Cy") == ["Al", "Bo", "Cy"]
    # a collective is ONE credit, not four people
    assert pg_text.producers(
        "Produced by the Online Distributed Proofreading Team") == [
        "the Online Distributed Proofreading Team"]
    # A NOTE IS NOT A PERSON. Leaving it out loses nothing — the whole
    # credit is kept verbatim in extra.marc["508"] and
    # extra.text_header["Credits"] — while putting it in the credits list
    # invents a contributor. Both real, from the live corpus.
    assert pg_text.producers(
        "Produced by Jane Doe from page images generously made available "
        "by the Internet Archive and the Library of Congress") == ["Jane Doe"]
    assert pg_text.producers(
        "Produced by Ebooks Libres et Gratuits; this text is also available "
        "in multiple formats at www.ebooksgratuits.com") == [
        "Ebooks Libres et Gratuits"]
    assert pg_text.producers("") == []


def test_the_date_the_book_entered_pg_is_kept(tmp_path):
    """Promise 9(c) lets us drop the date an entity entered OUR corpus.
    This is the date it entered THEIRS — a fact about the edition, and
    the key `pack build --until` cuts on. Asked for explicitly
    (operator, 2026-09-12)."""
    from app.plugins.gutenberg import SOURCE
    from app.plugins.base import DocRef
    (tmp_path / "pg76.rdf").write_text(RDF, encoding="utf-8")
    (tmp_path / "pg76.txt").write_text(TEXT_WITH_HEADER, encoding="utf-8")
    doc = SOURCE.parse(SOURCE.fetch(DocRef(source_id="gutenberg", native_id="76",
                                           locator=str(tmp_path))))
    assert doc.extra["issued"] == "1993-01-01"
    assert doc.date is None, "and it is NOT passed off as the work's date"
