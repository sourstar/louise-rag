"""Gutenberg parser 7 — the RDF metadata the fidelity audit found dropped
(2026-09-14, invariant 11). Records only: none of this enters sections or
content_hash, so none of it moves a vector.

Every test here fails on parser 6.
"""
from pathlib import Path

from app.plugins.gutenberg import rdf

RDF = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dcterms="http://purl.org/dc/terms/"
         xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
         xmlns:marcrel="http://id.loc.gov/vocabulary/relators/">
  <pgterms:ebook rdf:about="ebooks/76">
    <dcterms:title>The Fables of Aesop
Selected, Told Anew</dcterms:title>
    <dcterms:title>A Second Title</dcterms:title>
    <dcterms:creator>
      <pgterms:agent rdf:about="2009/agents/53">
        <pgterms:name>Twain, Mark</pgterms:name>
        <pgterms:birthdate rdf:datatype="http://www.w3.org/2001/XMLSchema#integer">1835</pgterms:birthdate>
      </pgterms:agent>
    </dcterms:creator>
    <marcrel:ill rdf:resource="2009/agents/53"/>
    <marcrel:edt>
      <pgterms:agent rdf:about="2009/agents/99">
        <pgterms:name>Kemble, E. W.</pgterms:name>
      </pgterms:agent>
    </marcrel:edt>
    <dcterms:creator rdf:resource="2009/agents/99"/>
    <marcrel:trl rdf:resource="2009/agents/777"/>
    <dcterms:license rdf:resource="license"/>
  </pgterms:ebook>
</rdf:RDF>
"""


def _meta(tmp_path: Path) -> dict:
    d = tmp_path / "76"
    d.mkdir()
    (d / "pg76.rdf").write_text(RDF, encoding="utf-8")
    return rdf.parse(d, 76)


def test_a_role_given_by_reference_is_credited(tmp_path: Path):
    """Twain is written inline as the creator and referred to as the
    illustrator: the second role used to vanish."""
    got = {(a.get("name"), a["role"]) for a in _meta(tmp_path)["creators_detail"]}
    assert ("Twain, Mark", "creator") in got
    assert ("Twain, Mark", "ill") in got


def test_a_creator_given_by_reference_is_an_author(tmp_path: Path):
    """Where the reference was the CREATOR, the book's author was missing
    from `creators` altogether."""
    assert _meta(tmp_path)["author_names"] == ["Twain, Mark", "Kemble, E. W."]


def test_a_reference_the_file_does_not_describe_keeps_its_id_and_role(tmp_path: Path):
    entries = _meta(tmp_path)["creators_detail"]
    assert {"role": "trl", "agent_id": "2009/agents/777"} in entries
    assert not any(a.get("agent_id") == "license" for a in entries), \
        "only credit-bearing elements are agents"


def test_the_record_carries_the_titles_and_the_credits(tmp_path: Path):
    """Through the real parse path: what rdf.parse now keeps must reach the
    record, or it is still dropped."""
    from app.plugins.base import DocRef
    from app.plugins.gutenberg.source import GutenbergSource
    d = tmp_path / "76"
    d.mkdir()
    (d / "pg76.rdf").write_text(RDF, encoding="utf-8")
    src = GutenbergSource()
    doc = src.parse(src.fetch(DocRef(source_id="gutenberg", native_id="76", locator=str(d))))
    assert doc.creators == ["Twain, Mark", "Kemble, E. W."]
    assert doc.extra["title_raw"] == "The Fables of Aesop\nSelected, Told Anew"
    assert doc.extra["other_titles"] == ["A Second Title"]
    assert ("Twain, Mark", "ill") in {(a.get("name"), a["role"])
                                      for a in doc.extra["creators_detail"]}
    assert doc.extra["form"] == "rdf"


def test_every_title_is_kept_and_the_first_in_the_origins_own_form(tmp_path: Path):
    meta = _meta(tmp_path)
    assert meta["title"] == "The Fables of Aesop Selected, Told Anew"
    assert meta["title_raw"] == "The Fables of Aesop\nSelected, Told Anew", \
        "the title / subtitle boundary the origin asserts"
    assert meta["other_titles"] == ["A Second Title"]
