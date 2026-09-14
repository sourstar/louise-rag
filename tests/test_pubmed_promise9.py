"""Promise 9 for pubmed: nothing the origin says about an article is lost.

docs/01 promise 9, docs/06 6.4. Parser 5 published 16 of the ~103 things
a <PubmedArticle> asserts; parser 6 keeps the rest.

WHY THIS FILE IS A COVERAGE TABLE AND NOT A HANDFUL OF CASES. A dropped
field is invisible to every check the platform has: `extra` is outside
`content_hash` (which is [title, sections] alone), so a parser that stops
emitting one still passes `records`, still passes `records-vs-origin` --
it re-parses with the SAME parser and reproduces the same lossy record --
and still passes chunks, vectors-sample, calibration and signature. Only
a test can see it, which is why promise 9 gets one per field rather than
a review note.
"""
import xml.etree.ElementTree as ET

import pytest

from app.plugins.pubmed import medline

ARTICLE = """<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM" VersionID="2"
                   VersionDate="2019-03-12" IndexingMethod="Curated">
    <PMID Version="3">12345678</PMID>
    <DateCompleted><Year>2020</Year><Month>01</Month><Day>02</Day></DateCompleted>
    <DateRevised><Year>2026</Year><Month>07</Month><Day>15</Day></DateRevised>
    <Article PubModel="Print-Electronic">
      <Journal>
        <ISSN IssnType="Electronic">1178-2005</ISSN>
        <JournalIssue CitedMedium="Internet">
          <Volume>18</Volume><Issue>4</Issue>
          <PubDate><MedlineDate>1998 Spring</MedlineDate></PubDate>
        </JournalIssue>
        <Title>Journal of Widget Medicine</Title>
        <ISOAbbreviation>J Widget Med</ISOAbbreviation>
      </Journal>
      <ArticleTitle>On widgets and 10<sup>6</sup> cells.</ArticleTitle>
      <Pagination><MedlinePgn>1575-1586</MedlinePgn></Pagination>
      <ELocationID EIdType="doi" ValidYN="Y">10.1000/widget.2026</ELocationID>
      <ELocationID EIdType="pii" ValidYN="N">E77</ELocationID>
      <Abstract>
        <AbstractText Label="AIM" NlmCategory="OBJECTIVE">To study widgets.</AbstractText>
        <CopyrightInformation>(c) 2026 The Authors.</CopyrightInformation>
      </Abstract>
      <AuthorList CompleteYN="Y">
        <Author ValidYN="Y" EqualContrib="Y">
          <LastName>Curie</LastName><ForeName>Marie</ForeName>
          <Initials>M</Initials><Suffix>Jr</Suffix>
          <Identifier Source="ORCID">0000-0002-1825-0097</Identifier>
          <AffiliationInfo><Affiliation>Institut du Radium, Paris.</Affiliation>
            <Identifier Source="GRID">grid.1234.5</Identifier></AffiliationInfo>
        </Author>
        <Author ValidYN="Y"><CollectiveName>The Widget Study Group</CollectiveName></Author>
      </AuthorList>
      <Language>eng</Language>
      <Language>fre</Language>
      <DataBankList CompleteYN="Y">
        <DataBank><DataBankName>ClinicalTrials.gov</DataBankName>
          <AccessionNumberList><AccessionNumber>NCT00000000</AccessionNumber></AccessionNumberList>
        </DataBank>
      </DataBankList>
      <GrantList CompleteYN="N">
        <Grant><GrantID>R01-WIDGET</GrantID><Agency>NIH</Agency><Country>United States</Country></Grant>
      </GrantList>
      <PublicationTypeList>
        <PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D016441">Retracted Publication</PublicationType>
      </PublicationTypeList>
      <VernacularTitle>Sur les widgets.</VernacularTitle>
      <ArticleDate DateType="Electronic"><Year>2026</Year><Month>06</Month><Day>01</Day></ArticleDate>
    </Article>
    <MedlineJournalInfo>
      <Country>England</Country><MedlineTA>J Widget Med</MedlineTA>
      <NlmUniqueID>101999999</NlmUniqueID><ISSNLinking>1178-2005</ISSNLinking>
    </MedlineJournalInfo>
    <ChemicalList>
      <Chemical><RegistryNumber>059QF0KO0R</RegistryNumber>
        <NameOfSubstance UI="D014867">Water</NameOfSubstance></Chemical>
    </ChemicalList>
    <CitationSubset>IM</CitationSubset>
    <CommentsCorrectionsList>
      <CommentsCorrections RefType="RetractionIn">
        <RefSource>J Widget Med. 2027;19:1</RefSource><PMID Version="1">99999999</PMID>
      </CommentsCorrections>
    </CommentsCorrectionsList>
    <MeshHeadingList>
      <MeshHeading>
        <DescriptorName UI="D014481" MajorTopicYN="N" Type="Geographic" AutoHM="Y">United States</DescriptorName>
        <QualifierName UI="Q000453" MajorTopicYN="Y" AutoHM="N">epidemiology</QualifierName>
      </MeshHeading>
      <MeshHeading><DescriptorName UI="D006801" MajorTopicYN="N">Humans</DescriptorName></MeshHeading>
    </MeshHeadingList>
    <KeywordList Owner="NOTNLM">
      <Keyword MajorTopicYN="N">COPD</Keyword>
    </KeywordList>
    <CoiStatement>The authors declare none.</CoiStatement>
    <GeneSymbolList><GeneSymbol>BRCA1</GeneSymbol></GeneSymbolList>
    <SupplMeshList><SupplMeshName Type="Disease" UI="C000001">Widgetosis</SupplMeshName></SupplMeshList>
    <OtherID Source="NLM">PMC7777777</OtherID>
    <SpaceFlightMission>STS-1</SpaceFlightMission>
    <GeneralNote Owner="NLM">A note from the indexer.</GeneralNote>
    <PersonalNameSubjectList>
      <PersonalNameSubject><LastName>Pasteur</LastName><ForeName>Louis</ForeName>
        <Initials>L</Initials><Suffix>Sr</Suffix></PersonalNameSubject>
    </PersonalNameSubjectList>
    <NumberOfReferences>42</NumberOfReferences>
    <InvestigatorList>
      <Investigator ValidYN="Y"><LastName>Kabir</LastName><ForeName>Iqbal</ForeName>
        <Initials>I</Initials><Identifier Source="ORCID">0000-0001-0000-0001</Identifier>
        <AffiliationInfo><Affiliation>ICDDR,B.</Affiliation></AffiliationInfo></Investigator>
    </InvestigatorList>
    <!-- WHERE MEDLINE PUTS IT: a child of MedlineCitation, not of Article.
         This fixture had it inside Article, and parser 7 read it only
         there — so the test passed while no real record kept one. -->
    <OtherAbstract Language="fre" Type="Publisher">
      <AbstractText>Etude des widgets.</AbstractText>
    </OtherAbstract>
  </MedlineCitation>
  <PubmedData>
    <History>
      <PubMedPubDate PubStatus="received"><Year>2023</Year><Month>1</Month><Day>11</Day>
        <Hour>9</Hour><Minute>5</Minute></PubMedPubDate>
      <PubMedPubDate PubStatus="accepted"><Year>2023</Year><Month>7</Month><Day>3</Day></PubMedPubDate>
    </History>
    <PublicationStatus>ppublish</PublicationStatus>
    <ArticleIdList>
      <ArticleId IdType="pubmed">12345678</ArticleId>
      <ArticleId IdType="pmc">PMC10377554</ArticleId>
      <ArticleId IdType="pii">398816</ArticleId>
    </ArticleIdList>
    <ReferenceList><Title>References</Title>
      <Reference><Citation>Someone et al. 1999.</Citation>
        <ArticleIdList><ArticleId IdType="pubmed">11111111</ArticleId>
          <ArticleId IdType="doi">10.1/x</ArticleId></ArticleIdList>
      </Reference>
      <ReferenceList><Title>Supplementary</Title>
        <Reference><Citation>Nested 2001.</Citation></Reference>
      </ReferenceList>
    </ReferenceList>
  </PubmedData>
</PubmedArticle>"""


@pytest.fixture(scope="module")
def full():
    return medline.parse_full(ET.fromstring(ARTICLE))


#: (what the origin asserts, where it must land, a value proving it arrived).
#: EVERY ROW WAS A FIELD PARSER 5 DISCARDED. Adding a row is how a newly
#: noticed origin field becomes a requirement; deleting one needs an
#: argument from promise 9's four categories, in the commit message.
COVERAGE = [
    ("MedlineCitation/@Status",              "status",             "MEDLINE"),
    ("MedlineCitation/@Owner",               "owner",              "NLM"),
    ("MedlineCitation/@VersionID",           "version_id",         "2"),
    ("MedlineCitation/@VersionDate",         "version_date",       "2019-03-12"),
    ("MedlineCitation/@IndexingMethod",      "indexing_method",    "Curated"),
    ("PMID/@Version",                        "pmid_version",       "3"),
    ("Article/@PubModel",                    "pub_model",          "Print-Electronic"),
    ("Article/VernacularTitle",              "vernacular_title",   "Sur les widgets."),
    ("Article/Pagination/MedlinePgn",        "pagination",         "1575-1586"),
    ("Journal/Title",                        "journal_title",      "Journal of Widget Medicine"),
    ("Journal/ISSN",                         "issn",               "1178-2005"),
    ("Journal/ISSN/@IssnType",               "issn_type",          "Electronic"),
    ("JournalIssue/Volume",                  "volume",             "18"),
    ("JournalIssue/Issue",                   "issue",              "4"),
    ("JournalIssue/@CitedMedium",            "cited_medium",       "Internet"),
    ("PubDate/MedlineDate",                  "pub_date_raw",       "1998 Spring"),
    ("MedlineJournalInfo/MedlineTA",         "medline_ta",         "J Widget Med"),
    ("MedlineJournalInfo/NlmUniqueID",       "nlm_unique_id",      "101999999"),
    ("MedlineJournalInfo/ISSNLinking",       "issn_linking",       "1178-2005"),
    ("MedlineJournalInfo/Country",           "country",            "England"),
    ("DateCompleted",                        "date_completed",     "2020-01-02"),
    ("DateRevised",                          "revised",            "2026-07-15"),
    ("Article/ArticleDate",                  "article_date",       "2026-06-01"),
    ("PublicationStatus",                    "publication_status", "ppublish"),
    ("Abstract/CopyrightInformation",        "copyright",          "(c) 2026 The Authors."),
    ("CoiStatement",                         "coi_statement",      "The authors declare none."),
]


@pytest.mark.parametrize("origin_field,extra_key,expected", COVERAGE,
                         ids=[c[0] for c in COVERAGE])
def test_every_origin_field_reaches_the_record(full, origin_field, extra_key, expected):
    """One row per thing the origin asserts. Parser 5 dropped all of these
    and no check could see it."""
    assert full["extra"].get(extra_key) == expected, (
        f"{origin_field} is not wrapping, not boilerplate, not our "
        f"corpus-add date and not the provider's packaging -- promise 9 "
        f"admits no category for dropping it")


def test_the_list_valued_fields_arrive_whole(full):
    e = full["extra"]
    assert e["ids"] == {"pubmed": "12345678", "pmc": "PMC10377554",
                        "pii": "398816", "doi": "10.1000/widget.2026"}
    assert {p["name"] for p in e["publication_types"]} == {
        "Journal Article", "Retracted Publication"}
    assert e["keywords"] == [{"term": "COPD", "major": "N", "owner": "NOTNLM"}]
    assert e["chemicals"] == [{"name": "Water", "ui": "D014867",
                               "registry_number": "059QF0KO0R"}]
    assert e["suppl_mesh"] == [{"name": "Widgetosis", "ui": "C000001", "type": "Disease"}]
    assert e["gene_symbols"] == ["BRCA1"]
    assert e["citation_subsets"] == ["IM"]
    assert e["space_flight_missions"] == ["STS-1"]
    assert e["general_notes"] == [{"note": "A note from the indexer.", "owner": "NLM"}]
    assert e["personal_name_subjects"] == [{"last": "Pasteur", "fore": "Louis",
                                            "initials": "L", "suffix": "Sr"}]
    assert e["other_ids"] == [{"source": "NLM", "id": "PMC7777777"}]
    assert e["languages"] == ["eng", "fre"]
    assert e["grants"] == [{"id": "R01-WIDGET", "agency": "NIH",
                            "country": "United States"}]
    assert e["databanks"] == [{"name": "ClinicalTrials.gov",
                               "accessions": ["NCT00000000"]}]
    assert e["history"] == [{"status": "received", "date": "2023-01-11", "time": "09:05"},
                            {"status": "accepted", "date": "2023-07-03"}]
    assert e["references"] == [
        {"citation": "Someone et al. 1999.", "pmid": "11111111",
         "ids": {"pubmed": "11111111", "doi": "10.1/x"}, "list_title": "References"},
        {"citation": "Nested 2001.", "list_title": "References › Supplementary"}]
    assert e["other_abstracts"] == [
        {"language": "fre", "type": "Publisher", "text": "Etude des widgets."}]


def test_parser_8_keeps_what_parser_7_dropped(full):
    """The 2026-09-14 sample check: 180,000 real records over six baseline
    files, every element and attribute counted, each of these found
    dropped. Records only — `sections` is untouched, so none of the
    archive's vectors moves."""
    e = full["extra"]
    assert e["investigators"] == [{
        "last": "Kabir", "fore": "Iqbal", "initials": "I", "valid": "Y",
        "identifiers": {"orcid": "0000-0001-0000-0001"}, "affiliations": ["ICDDR,B."]}]
    assert e["elocation_ids"] == [
        {"type": "doi", "id": "10.1000/widget.2026", "valid": "Y"},
        {"type": "pii", "id": "E77", "valid": "N"}], \
        "the pii the merged map discarded, and the flag saying it is wrong"
    assert e["ids"]["pii"] == "398816", "the merged map is what it was"
    assert {"type": "pmc", "id": "PMC10377554"} in e["article_ids"]
    assert e["abstract_parts"] == [{"label": "AIM", "nlm_category": "OBJECTIVE"}]
    assert e["authors_complete"] == "Y" and e["grants_complete"] == "N" \
        and e["databanks_complete"] == "Y"
    assert e["number_of_references"] == "42"
    assert e["article_date_type"] == "Electronic"
    assert e["title_xml"] == "On widgets and 10<sup>6</sup> cells."
    assert full["title"] == "On widgets and 106 cells.", \
        "the flattened title — what `sections` holds — does not move"
    us = next(m for m in e["mesh"] if m["descriptor"] == "United States")
    assert us["type"] == "Geographic" and us["auto_hm"] == "Y"
    assert full["authors_full"][0]["affiliation_ids"] == [
        {"affiliation": "Institut du Radium, Paris.", "source": "GRID", "id": "grid.1234.5"}]


def test_a_retraction_is_visible(full):
    """The collection declares `erasure: retractable`, and parser 5 left a
    retracted article indistinguishable from a primary study: both halves
    of the story -- the publication type and the link to the notice --
    were dropped."""
    assert "Retracted Publication" in {p["name"] for p in full["extra"]["publication_types"]}
    assert full["extra"]["comments_corrections"] == [
        {"ref_type": "RetractionIn", "source": "J Widget Med. 2027;19:1",
         "pmid": "99999999", "pmid_version": "1"}]


def test_subjects_carry_the_mesh_qualifier(full):
    """"Neoplasms/genetics", not "Neoplasms". 9.5's own pubmed example
    writes it that way, and this plugin declares `subjects` a TREE facet
    with separator "/" -- which had nothing to split while only
    DescriptorName was read."""
    assert full["subjects"] == ["United States/epidemiology", "Humans"]
    from app.plugins.pubmed import SOURCE
    facet = next(f for f in SOURCE.default_facets if f["field"] == "subjects")
    assert facet["separator"] == "/" and facet["kind"] == "tree"
    assert any(facet["separator"] in s for s in full["subjects"]), \
        "the facet declares a hierarchy; the field must be able to express one"


def test_the_qualifier_uis_and_major_flags_survive_too(full):
    """A "/"-joined string cannot carry them, so they are in extra.mesh."""
    us = next(m for m in full["extra"]["mesh"] if m["descriptor"] == "United States")
    assert us["ui"] == "D014481" and us["major"] == "N"
    assert us["qualifiers"] == [{"name": "epidemiology", "ui": "Q000453", "major": "Y",
                                 "auto_hm": "N"}]


def test_an_author_is_more_than_a_surname(full):
    """ORCID and affiliation are the origin asserting who the author IS.
    9.5 makes `creators` a list of strings, so they need objects."""
    first = full["authors_full"][0]
    assert first["identifiers"] == {"orcid": "0000-0002-1825-0097"}
    assert first["affiliations"] == ["Institut du Radium, Paris."]
    assert first["fore"] == "Marie" and first["suffix"] == "Jr"
    assert first["equal_contrib"] == "Y"
    assert full["authors_full"][1]["collective"] == "The Widget Study Group"


def test_both_dates_survive_where_parser_5_kept_one(full):
    """`revised` used to `break` on the first of DateRevised/DateCompleted,
    so whenever both were present one was lost AND the consumer could not
    tell which it had."""
    assert full["extra"]["revised"] == "2026-07-15"
    assert full["extra"]["date_completed"] == "2020-01-02"


def test_the_origins_own_pubdate_string_is_kept(full):
    """9.5's `date` cannot express "1998 Spring", so it holds the year --
    and the origin's own string is kept beside it rather than rounded
    away."""
    assert medline.publication_date(ET.fromstring(ARTICLE)) == "1998"
    assert full["extra"]["pub_date_raw"] == "1998 Spring"


def test_an_empty_field_is_omitted_not_written_as_empty(full):
    """A sparse record stays small: `extra` carries what the origin said,
    not a null for everything it did not."""
    sparse = medline.parse_full(ET.fromstring(
        "<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>"
        "<ArticleTitle>T</ArticleTitle></Article></MedlineCitation></PubmedArticle>"))
    assert "coi_statement" not in sparse["extra"]
    assert "references" not in sparse["extra"]
    assert all(v not in ("", None, [], {}) for v in sparse["extra"].values())
