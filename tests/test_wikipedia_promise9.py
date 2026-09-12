"""Promise 9 for wikipedia: what the page says, and what we keep.

docs/01 promise 9, docs/06 6.4. Parser 1 published the prose and nothing
else, because `strip_code` deletes every template and tag. Measured on
3,000 non-redirect enwiki pages, that discarded:

    [[Category:...]]        93.0%      {{Authority control}}   64.9%
    <ref> citations         89.6%      {{Infobox ...}}         51.4%
    {{cite ...}}            89.0%      {{convert|...}} etc     35.2%
    {{Short description}}   86.1%      tables                  29.7%
    == External links ==    73.9%      {{Coord}}               13.1%
"""
import pytest

from app.plugins.wikipedia import wikitext

PAGE = """{{Short description|A town in the north}}
{{Infobox settlement
| name = Northtown
| population = 12345
| founded = 1819
}}
Northtown is a town.<ref>Smith, ''A History'', 1999.</ref> The river is
{{convert|5|km}} long.

== History ==
It began in 1819.<ref name=a>Jones 2001</ref>

== Geography ==
{{Coord|36|42|N|3|13|E|type:city}}
It is flat.

== See also ==
* [[Southtown]]

== References ==
<references/>

== External links ==
* [https://example.org/northtown Official site]

[[Category:Towns]]
[[Category:1819 establishments|Northtown]]
{{Authority control}}
"""


@pytest.fixture(scope="module")
def got():
    return wikitext.parse_all(PAGE)


def test_categories_become_subjects(got):
    """Operator, 2026-09-12: populate them, verbatim. The sortkey after a
    "|" is a collation hint for the category page, not a subject."""
    assert got["categories"] == ["Towns", "1819 establishments"]


def test_the_infobox_is_kept_field_by_field(got):
    """51.4% of enwiki pages carry one, median 18 parameters, and all of
    them were dropped."""
    ib = got["infoboxes"][0]
    assert ib["template"].lower().startswith("infobox")
    assert ib["fields"]["name"] == "Northtown"
    assert ib["fields"]["population"] == "12345"
    assert ib["fields"]["founded"] == "1819"


def test_citations_are_kept(got):
    """89.6% of pages cite something; a citation is what the article says
    its claim rests on."""
    assert any("Smith" in r and "1999" in r for r in got["references"])
    assert any("Jones 2001" in r for r in got["references"])


def test_the_apparatus_leaves_the_prose_but_not_the_record(got):
    """References/External links/See also stay out of the CHUNKS, where
    they are citation noise — the reason they were dropped — and are kept
    in `extra` instead of discarded."""
    # "References" is absent on purpose: its body is only a
    # `<references/>` placeholder, and the citations it renders are the
    # inline <ref> tags, which `references()` keeps. An empty section has
    # nothing of its own to preserve.
    assert set(got["apparatus"]) == {"See also", "External links"}
    assert "Southtown" in got["apparatus"]["See also"]
    assert got["references"], "the citations themselves are kept"
    prose = " ".join(b for _, b in got["sections"])
    assert "Southtown" not in prose, "still not chunked as prose"


def test_a_citation_does_not_end_up_inside_the_sentence(got):
    """`strip_code` removes the <ref> TAG but keeps its contents, so a
    citation was glued mid-sentence — "Northtown is a town.Smith, ''A
    History'', 1999. The river is…" — and that went into the chunks and
    the vectors. Nothing is lost by taking it out of the prose: every
    citation is in `references`."""
    lead = got["sections"][0][1]
    assert "Northtown is a town." in lead
    assert "Smith" not in lead, "the citation is not in the sentence"
    assert any("Smith" in r for r in got["references"]), "it is in references"


def test_the_short_description_and_coordinates(got):
    assert got["short_description"] == "A town in the north"
    assert got["coordinates"] and "36" in got["coordinates"][0]


def test_external_links_are_kept(got):
    urls = [e["url"] for e in got["external_links"]]
    assert "https://example.org/northtown" in urls


def test_authority_control_records_only_its_presence(got):
    """It looks like the perfect source of 6.5 canonical ids and carries
    NONE: 573 of 574 occurrences in an 800-page sample have zero
    parameters, because the identifiers live in Wikidata and the template
    only points at them. Recording presence is all there is to record."""
    assert got["has_authority_control"] is True


def test_an_inline_data_template_stays_in_its_sentence(got):
    """`strip_code` deletes a template whole, so "the river is
    {{convert|5|km}} long" became "the river is  long" — a sentence the
    origin never wrote."""
    lead = got["sections"][0][1]
    assert "5 km long" in lead.replace("\n", " ")


def test_a_section_body_does_not_repeat_its_own_heading():
    """PRE-EXISTING, and found by reading real output rather than by a
    test: `strip_code` returns the body with a leading newline before the
    heading text, so `body.startswith(title)` was False on every section
    and the removal never ran. The final `.strip()` then tidied the
    newline away, hiding the duplicate."""
    secs = dict(wikitext.to_sections(PAGE))
    assert secs["History"].startswith("It began"), secs["History"]
    assert not secs["History"].startswith("History")


def test_a_redirect_is_still_a_redirect():
    assert wikitext.is_redirect("#REDIRECT [[Elsewhere]]") is True
    assert wikitext.is_redirect(PAGE) is False
