"""A record whose only title is its vernacular one still has a title.

Measured on the live pubmed collection 2026-09-13: 771,527 documents —
1.9% of 39,994,988 — had no title AND no authors. They were not
editorials. Their `<ArticleTitle />` is empty and their
`<VernacularTitle>` is full, because NLM indexed the article without
supplying an English rendering of its name.

The cost was threefold: a blank row in the Library, nothing for the
catalog's FTS to match on, and an EMPTY "Title" chunk carried into the
vectors (pubmed's sections are [Title, Abstract], so a record with no
abstract had no text at all).

PMID 10506388 is the shape this is drawn from.
"""
from xml.etree import ElementTree as ET

import pytest

from app.plugins.pubmed import medline

VERNACULAR_ONLY = """<PubmedArticle><MedlineCitation>
  <PMID>10506388</PMID>
  <Article PubModel="Print">
    <Journal><Title>Zeitschrift fur Gerontologie und Geriatrie</Title>
      <JournalIssue><PubDate><Year>1999</Year><Month>Sep</Month></PubDate></JournalIssue>
    </Journal>
    <ArticleTitle />
    <Language>ger</Language>
    <VernacularTitle>Mitteilungen der Bundesarbeitsgemeinschaft Klinisch-Geriatrischer Einrichtungen e.V.</VernacularTitle>
  </Article>
</MedlineCitation></PubmedArticle>"""

BOTH_TITLES = """<PubmedArticle><MedlineCitation>
  <PMID>99</PMID>
  <Article PubModel="Print">
    <ArticleTitle>Effects of the thing.</ArticleTitle>
    <Language>ger</Language>
    <VernacularTitle>Wirkungen der Sache.</VernacularTitle>
  </Article>
</MedlineCitation></PubmedArticle>"""

NEITHER = """<PubmedArticle><MedlineCitation>
  <PMID>100</PMID>
  <Article PubModel="Print"><ArticleTitle /><Language>eng</Language></Article>
</MedlineCitation></PubmedArticle>"""


def _parse(xml):
    return medline.parse_article(ET.fromstring(xml))


def test_the_vernacular_title_is_the_title_when_there_is_no_other():
    got = _parse(VERNACULAR_ONLY)
    assert got["title"] == ("Mitteilungen der Bundesarbeitsgemeinschaft "
                            "Klinisch-Geriatrischer Einrichtungen e.V.")


def test_an_article_title_still_wins_when_there_is_one():
    """The fallback is a fallback. NLM's English rendering is the title
    wherever it supplied one, and the vernacular stays beside it."""
    assert _parse(BOTH_TITLES)["title"] == "Effects of the thing."


def test_the_vernacular_title_is_kept_either_way():
    """Promise 9: which element the title came from stays recoverable,
    and a record that has both said both."""
    full = medline.parse_full(ET.fromstring(BOTH_TITLES))
    assert full["extra"]["vernacular_title"] == "Wirkungen der Sache."
    used = medline.parse_full(ET.fromstring(VERNACULAR_ONLY))
    assert used["extra"]["vernacular_title"] == used["title"]


def test_no_title_anywhere_is_still_no_title():
    """Nothing is invented. A record the origin does not name stays
    unnamed rather than acquiring a derived one."""
    assert _parse(NEITHER)["title"] == ""


def test_the_titled_record_now_carries_text():
    """The point, end to end: pubmed's sections are [Title, Abstract], so
    a record with an empty title and no abstract went into the vectors
    with nothing in it at all."""
    from app.plugins.base import DocRef, RawDoc
    from app.plugins.pubmed.source import PubmedSource

    raw = RawDoc(ref=DocRef(source_id="pubmed", native_id="10506388", locator="x"),
                 raw_hash="0" * 32, data=VERNACULAR_ONLY.encode("utf-8"))
    doc = PubmedSource().parse(raw)
    assert doc.title
    assert any(body.strip() for _, body in doc.sections), \
        "a document with no text is a document no query can reach"


def test_the_parser_version_moved():
    """Invariant 3: this one changes `content_hash`, because 9.5 hashes
    [title, sections] and the title IS a section here. Bumped explicitly."""
    from app.plugins.pubmed.source import PubmedSource
    assert PubmedSource.PARSER_VERSION >= 7
