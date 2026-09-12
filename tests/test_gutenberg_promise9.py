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


def test_the_display_string_is_unchanged_by_the_role_work(meta):
    """Deliberately every agent, in the origin's order. Filtering to
    `creator` reads better and loses nothing -- the role is in
    `creators_detail` either way -- but it reinterprets who counts as an
    author and changes what every reader sees, which is not a side effect
    a fidelity fix gets to have."""
    assert meta["authors"] == "Twain, Mark; Kemble, E. W."


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
