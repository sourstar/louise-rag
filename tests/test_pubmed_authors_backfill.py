"""PubMed §9.5 compliance: the parser's three corrected fields, the bulk
authors reader, and the backfill for the development collection.

Parser version 4 got three §9.5 fields wrong and the origin carried all
three all along (measured on the node, 2026-09-12, over 39,994,988
records):

    creators   always []            AuthorList is on 99.0% of articles
    lang       hardcoded "en"       <Language> is on 100%, and 9.6% of
                                    this corpus is NOT English
    date       always None          PubDate is on 100%

Version 5 fixes them. A collection carrying them is a different
collection — `parser.version` is inside `collection_id` — and the
operator's decision (2026-09-12) is that the corrected collections are
v1.0 ROOTS built on a corpus or a pre-embedded corpus, not corrective
sisters of the development ones.

The backfill here serves the DEVELOPMENT collection's display until the
v1.0 root replaces it; it also builds the container packs the rebuild
needs, so it is not throwaway work.
"""
import hashlib
from pathlib import Path

import pytest

from app import collections as collections_mod, db
from app.plugins.base import DiscoverState
from app.plugins.pubmed import SOURCE, pack

from tools import backfill_pubmed_authors as backfill

ARTICLE = """  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">{pmid}</PMID>
      <Article PubModel="Print">
        <Journal>
          <ISOAbbreviation>J Test</ISOAbbreviation>
          <JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue>
        </Journal>
        <Language>ger</Language>
        <ArticleTitle>{title}</ArticleTitle>
        <Abstract><AbstractText>An abstract for {pmid}.</AbstractText></Abstract>
        {authors}
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName UI="D1">Testing</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
  </PubmedArticle>
"""

AUTHOR_LIST = """<AuthorList CompleteYN="Y">
          <Author ValidYN="Y"><LastName>Curie</LastName><Initials>M</Initials></Author>
          <Author ValidYN="Y"><LastName>Meitner</LastName><Initials>L</Initials></Author>
          <Author ValidYN="Y"><CollectiveName>The Widget Study Group</CollectiveName></Author>
        </AuthorList>"""


def _container(tmp_path: Path) -> Path:
    """One container: two authored articles, one with no AuthorList at
    all, and a DeleteCitation. `fetch()` verifies the md5 sidecar, so the
    fixture writes one. The articles are `ger` to catch a `lang` that is
    hardcoded rather than parsed."""
    body = (ARTICLE.format(pmid="40000001", title="Authored one.", authors=AUTHOR_LIST)
            + ARTICLE.format(pmid="40000002", title="Authored two.", authors=AUTHOR_LIST)
            + ARTICLE.format(pmid="40000003", title="Anonymous editorial.", authors="")
            + "  <DeleteCitation><PMID>40000009</PMID></DeleteCitation>\n")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<PubmedArticleSet>\n'
           + body + "</PubmedArticleSet>\n")
    corpus = tmp_path / "corpus"
    corpus.mkdir(exist_ok=True)
    path = corpus / "pubmed26n0001.xml"
    path.write_text(xml, encoding="utf-8")
    digest = hashlib.md5(path.read_bytes()).hexdigest()
    (corpus / "pubmed26n0001.xml.md5").write_text(f"{digest}  {path.name}\n")
    return path


def _collection(tmp_path: Path, corpus: Path) -> collections_mod.Collection:
    return collections_mod.Collection(
        name="pubmed", plugin="pubmed", profile=None, embed_mode_default=None,
        corpus_dir=corpus,
        manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts",
        lance_dir=tmp_path / "lancedb",
    )


@pytest.fixture(autouse=True)
def _fresh_pack_caches():
    pack.reset_caches()
    yield
    pack.reset_caches()


# ---------------- the bulk reader ----------------

def test_record_authors_is_keyed_on_pmid_and_skips_what_has_none(tmp_path):
    """Keyed on pmid because that is what the manifest can look up:
    `documents` has a UNIQUE index on (source, native_id) and none on
    `path`, so matching the `<container>::<index>` locator would scan
    40M rows per record."""
    got = pack.record_authors(_container(tmp_path))
    assert got == {
        "40000001": "Curie M; Meitner L; The Widget Study Group",
        "40000002": "Curie M; Meitner L; The Widget Study Group",
    }
    assert "40000003" not in got, "no AuthorList upstream is not ours to invent"
    assert "40000009" not in got, "a DeleteCitation carries no pmid column"


def test_a_collective_name_is_an_author(tmp_path):
    """Consortium bylines are the author for a good part of the corpus."""
    assert "The Widget Study Group" in pack.record_authors(_container(tmp_path))["40000001"]


# ---------------- the backfill ----------------

def _seed(con, pmids, authors=""):
    for pmid in pmids:
        con.execute(
            "INSERT INTO documents (uid, source, native_id, path, authors, title) "
            "VALUES (?,?,?,?,?,?)",
            (f"pubmed:{pmid}", "pubmed", pmid, f"/c/x.xml::0", authors, "t"))


def test_backfill_fills_authors_and_is_idempotent(tmp_path):
    container = _container(tmp_path)
    col = _collection(tmp_path, container.parent)
    con = db.connect(collection=col)
    _seed(con, ["40000001", "40000002", "40000003"])

    updated, skipped, seen = backfill.run(con, col)
    assert (updated, skipped, seen) == (2, 0, 2)
    got = dict(con.execute("SELECT native_id, authors FROM documents").fetchall())
    assert got["40000001"] == "Curie M; Meitner L; The Widget Study Group"
    assert got["40000002"] == "Curie M; Meitner L; The Widget Study Group"
    assert got["40000003"] == "", "no upstream authors stays empty, not invented"

    # a second run recognises the container as done and writes nothing
    assert backfill.run(con, col) == (0, 1, 2)


def test_backfill_dry_run_writes_nothing(tmp_path):
    container = _container(tmp_path)
    col = _collection(tmp_path, container.parent)
    con = db.connect(collection=col)
    _seed(con, ["40000001"])

    backfill.run(con, col, dry_run=True)
    assert con.execute("SELECT authors FROM documents").fetchone()["authors"] == ""


def test_backfill_does_not_overwrite_without_force(tmp_path):
    """An operator-corrected byline is not ours to stamp over."""
    container = _container(tmp_path)
    col = _collection(tmp_path, container.parent)
    con = db.connect(collection=col)
    _seed(con, ["40000001"], authors="Hand Corrected")
    _seed(con, ["40000002"])

    backfill.run(con, col, force=True)
    got = dict(con.execute("SELECT native_id, authors FROM documents").fetchall())
    assert got["40000001"] == "Curie M; Meitner L; The Widget Study Group", \
        "--force is explicit and does overwrite"

    con.execute("UPDATE documents SET authors='Hand Corrected' WHERE native_id='40000001'")
    backfill.run(con, col)
    assert con.execute("SELECT authors FROM documents WHERE native_id='40000001'"
                       ).fetchone()["authors"] == "Hand Corrected"


def test_the_skip_probe_does_not_mistake_an_absent_row_for_done(tmp_path):
    """A pmid the pack has authors for may have no manifest row at all
    (out of scope, never imported). That is no evidence of completion —
    treating it as such would silently skip the whole container."""
    container = _container(tmp_path)
    col = _collection(tmp_path, container.parent)
    con = db.connect(collection=col)
    assert backfill.already_done(con, "pubmed", ["40000001"]) is False
    _seed(con, ["40000001"])
    assert backfill.already_done(con, "pubmed", ["40000001"]) is False
    con.execute("UPDATE documents SET authors='X' WHERE native_id='40000001'")
    assert backfill.already_done(con, "pubmed", ["40000001"]) is True


def test_the_backfill_touches_only_authors(tmp_path):
    """`updated_at` on this table tracks PIPELINE progress, and genesis's
    candidate scan feeds it into ShardDemand.oldest, which orders which
    archive shard is fetched next. A display backfill has no business
    moving that for 40M rows."""
    container = _container(tmp_path)
    col = _collection(tmp_path, container.parent)
    con = db.connect(collection=col)
    con.execute(
        "INSERT INTO documents (uid, source, native_id, path, authors, title, "
        "updated_at, embed_done, fts_done) VALUES (?,?,?,?,?,?,?,?,?)",
        ("pubmed:40000001", "pubmed", "40000001", "/c/x.xml::0", "", "t", 1234, 1, 1))

    backfill.run(con, col)
    row = con.execute("SELECT authors, updated_at, embed_done, fts_done "
                      "FROM documents").fetchone()
    assert row["authors"] == "Curie M; Meitner L; The Widget Study Group"
    assert row["updated_at"] == 1234, "updated_at is a pipeline signal, not ours"
    assert (row["embed_done"], row["fts_done"]) == (1, 1), "no stage is re-armed"


# ---------------- §9.5 compliance of the record line ----------------

def _parsed(tmp_path, pmid="40000001"):
    container = _container(tmp_path)
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=container.parent))
               if r.native_id == pmid and r.extra.get("kind") == "article")
    return SOURCE.parse(SOURCE.fetch(ref))


def test_creators_are_parsed_in_the_origins_order(tmp_path):
    """§9.5: "list of strings, in the origin's order; [] when none"."""
    assert _parsed(tmp_path).creators == [
        "Curie M", "Meitner L", "The Widget Study Group"]
    assert _parsed(tmp_path, "40000003").creators == [], \
        "no AuthorList upstream is not ours to invent"


def test_lang_comes_from_the_origin_not_a_constant(tmp_path):
    """It was hardcoded "en" for all 39,994,988 records while 9.6% of the
    corpus is German, Russian, French, Chinese, Japanese…"""
    assert _parsed(tmp_path).language == "de", "ger -> de, and NOT 'en'"


def test_medline_three_letter_codes_map_to_the_iso_639_1_form(tmp_path):
    """MEDLINE writes ISO 639-2/B, §9.5 wants 639-1. The two alphabets
    disagree exactly where it matters — ger/fre/chi/dut/cze/rum against
    deu/fra/zho/nld/ces/ron — so the raw code cannot be passed through.
    Every code below was observed in this corpus (360,000-article sample).
    """
    from xml.etree import ElementTree as ET
    from app.plugins.pubmed import medline
    observed = {
        "eng": "en", "ger": "de", "rus": "ru", "fre": "fr", "ita": "it",
        "spa": "es", "chi": "zh", "jpn": "ja", "pol": "pl", "cze": "cs",
        "por": "pt", "dut": "nl", "dan": "da", "hun": "hu", "swe": "sv",
        "nor": "no", "bul": "bg", "rum": "ro", "fin": "fi", "slo": "sk",
        "ukr": "uk", "heb": "he", "tur": "tr", "hrv": "hr", "srp": "sr",
        "kor": "ko", "afr": "af", "ice": "is", "ind": "id", "gre": "el",
        "tha": "th", "lat": "la", "lit": "lt", "slv": "sl",
        # no two-letter code exists, so §9.5's 639-3 branch keeps them
        "und": "und", "mul": "mul",
    }
    for code, want in observed.items():
        elem = ET.fromstring(
            "<PubmedArticle><MedlineCitation><Article>"
            f"<Language>{code}</Language></Article></MedlineCitation></PubmedArticle>")
        assert medline.language(elem) == want, code

    # absent, empty, or a code we have no entry for: "und", never a guess
    for xml in ("<PubmedArticle><MedlineCitation><Article/></MedlineCitation></PubmedArticle>",
                "<PubmedArticle><MedlineCitation><Article><Language></Language>"
                "</Article></MedlineCitation></PubmedArticle>",
                "<PubmedArticle><MedlineCitation><Article><Language>zzz</Language>"
                "</Article></MedlineCitation></PubmedArticle>"):
        assert medline.language(ET.fromstring(xml)) == "und"


def test_the_first_language_wins_when_an_article_lists_several(tmp_path):
    """0.37% of this corpus carries more than one <Language> and §9.5's
    `lang` is a single string; MEDLINE puts the primary first."""
    from xml.etree import ElementTree as ET
    from app.plugins.pubmed import medline
    elem = ET.fromstring(
        "<PubmedArticle><MedlineCitation><Article>"
        "<Language>fre</Language><Language>eng</Language>"
        "</Article></MedlineCitation></PubmedArticle>")
    assert medline.language(elem) == "fr"


def test_date_is_one_of_the_three_admitted_forms(tmp_path):
    """§9.5: `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. A month arrives as "Nov"
    or as "11"; a range ("Nov-Dec") or a season ("Spring") resolves to the
    year alone rather than to an invented month."""
    from xml.etree import ElementTree as ET
    from app.plugins.pubmed import medline

    def date_of(inner):
        return medline.publication_date(ET.fromstring(
            "<PubmedArticle><MedlineCitation><Article><Journal><JournalIssue>"
            f"<PubDate>{inner}</PubDate></JournalIssue></Journal>"
            "</Article></MedlineCitation></PubmedArticle>"))

    assert date_of("<Year>1953</Year><Month>Nov</Month><Day>15</Day>") == "1953-11-15"
    assert date_of("<Year>1953</Year><Month>11</Month>") == "1953-11"
    assert date_of("<Year>1953</Year>") == "1953"
    assert date_of("<MedlineDate>1953 Nov-Dec</MedlineDate>") == "1953"
    assert date_of("<MedlineDate>1998 Spring</MedlineDate>") == "1998"
    assert date_of("<Year>1953</Year><Month>Nov-Dec</Month>") == "1953", \
        "a range is not a month; the year alone is the honest answer"
    assert date_of("") is None
    assert date_of("<Year>53</Year>") is None, "a 2-digit year is not YYYY"


def test_the_record_line_carries_exactly_9_5s_keys(tmp_path):
    """§9.5 admits "exactly these keys, written in this order". `layer` and
    `chunks` were two more, in neither §9.5's table nor 03 §3.2."""
    from app.pack import records as records_mod
    assert records_mod.RECORD_KEYS == (
        "uid", "native_id", "lang", "title", "creators", "date", "subjects",
        "keys", "sections", "extra", "origin", "content_hash", "origin_hash")
    assert "layer" not in records_mod.RECORD_KEYS
    assert "chunks" not in records_mod.RECORD_KEYS
    # shape 2 inserts `figures` between `sections` and `extra`, and nothing else
    assert records_mod.RECORD_KEYS_2 == (
        "uid", "native_id", "lang", "title", "creators", "date", "subjects",
        "keys", "sections", "figures", "extra", "origin", "content_hash",
        "origin_hash")


def test_the_corrected_fields_move_no_chunk_and_no_vector(tmp_path):
    """content_hash is BLAKE2b-128 over [title, sections] ALONE (§9.5), and
    a chunk id is `<uid>:<seq>:<chunker version>`. So correcting creators,
    lang and date moves no content_hash, no records_root, no chunk id and
    no vector — which is why the v1.0 root can stand on the pre-embedded
    corpus instead of re-embedding 39.9M abstracts at 20 chunks/s.
    """
    from app.pack import records as records_mod
    doc = _parsed(tmp_path)
    title, sections = doc.title, [list(s) for s in doc.sections]

    # two records over the SAME text: one as parser 4 left it, one as
    # parser 5 writes it
    v4 = {"uid": doc.uid, "native_id": "40000001", "lang": "en", "title": title,
          "creators": [], "date": None, "subjects": doc.subjects, "keys": [],
          "sections": sections, "extra": {}, "origin": "c.xml#0",
          "content_hash": records_mod.content_hash(title, sections),
          "origin_hash": "0" * 32}
    v5 = dict(v4, lang=doc.language, creators=doc.creators, date=doc.date,
              content_hash=records_mod.content_hash(title, sections))

    assert v4["lang"] != v5["lang"] and v4["creators"] != v5["creators"] \
        and v4["date"] != v5["date"], "the fixture must actually differ"
    assert v4["content_hash"] == v5["content_hash"], (
        "content_hash is over [title, sections] alone — the three corrected "
        "fields are outside it, so no chunk id and no vector moves")
    # the published lines DO differ, which is the shard's transport root
    assert records_mod.canonical_line(v4) != records_mod.canonical_line(v5)
