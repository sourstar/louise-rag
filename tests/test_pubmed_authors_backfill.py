"""PubMed authors: the display hook, the bulk reader, and the backfill.

`PubmedSource.parse()` has set `creators=[]` since the first commit, so
`plugin_io.metadata`'s fallback computed `authors=""` and all 39,994,988
pubmed documents displayed no author (measured on the node, 2026-09-12)
while the origin XML carried them all along.

The fix is DISPLAY ONLY, and the test that matters most here is
`test_the_records_do_not_move` — the collection is built, checked and
signed, and the whole point of hotfixing `documents.authors` instead of
`parse()` is that the records keep their bytes.
"""
import hashlib
from pathlib import Path

import pytest

from app import collections as collections_mod, db, plugin_io
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
    all, and a DeleteCitation. Written with an md5 sidecar because
    read_metadata verifies it, as the path it replaces does."""
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


# ---------------- the display hook ----------------

def test_read_metadata_supplies_authors(tmp_path):
    container = _container(tmp_path)
    meta = SOURCE.read_metadata(Path(f"{container}::0"), 40000001)
    assert meta["authors"] == "Curie M; Meitner L; The Widget Study Group"


def test_read_metadata_changes_nothing_but_authors(tmp_path):
    """The no-regression pin. `plugin_io.metadata` now takes the hook
    instead of the canonical_doc fallback, so every OTHER field it
    returns must be what the fallback returned — a hook that quietly
    dropped the mesh subjects would trade one empty column for another.
    """
    container = _container(tmp_path)
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=container.parent))
               if r.native_id == "40000001" and r.extra.get("kind") == "article")
    doc = SOURCE.parse(SOURCE.fetch(ref))
    fallback = {"title": doc.title, "authors": "; ".join(doc.creators),
                "lang": doc.language, "subjects": "|".join(doc.subjects),
                "bookshelves": str(doc.extra.get("bookshelves", ""))}

    hook = SOURCE.read_metadata(Path(ref.locator), 40000001)
    assert set(hook) == set(fallback), "same keys, or the worker's INSERT breaks"
    for key in ("title", "lang", "subjects", "bookshelves"):
        assert hook[key] == fallback[key], f"{key} moved"
    assert fallback["authors"] == "" and hook["authors"], "authors is the only change"


def test_read_metadata_tolerates_a_container_level_ref(tmp_path):
    """discover() yields one ref per FILE as well as per record; it has
    no record to read and never had metadata."""
    container = _container(tmp_path)
    meta = SOURCE.read_metadata(container, 0)
    assert meta["authors"] == "" and meta["title"] == ""


def test_read_metadata_still_verifies_the_md5_sidecar(tmp_path):
    """The fallback reached the record through fetch(), which verifies
    the sidecar. A display hook that skipped it would be a quietly
    weaker path to the same bytes."""
    container = _container(tmp_path)
    (container.parent / "pubmed26n0001.xml.md5").write_text(f"{'0' * 32}  {container.name}\n")
    pack.reset_caches()
    with pytest.raises(Exception):
        SOURCE.read_metadata(Path(f"{container}::0"), 40000001)


# ---------------- the integrity pin ----------------

def test_the_records_do_not_move(tmp_path):
    """THE POINT OF THE WHOLE APPROACH. pubmed is built, checked and
    signed; `hold records build` reads `plugin_io.canonical_doc`, never
    `plugin_io.metadata`. So `creators` stays empty in the RECORDS and
    every content_hash keeps its value — correcting the records would
    bump PARSER_VERSION into collection_id and fork the collection,
    which is a separate decision.
    """
    container = _container(tmp_path)
    ref = next(r for r in SOURCE.discover(DiscoverState(corpus_root=container.parent))
               if r.native_id == "40000001" and r.extra.get("kind") == "article")
    doc = SOURCE.parse(SOURCE.fetch(ref))
    assert doc.creators == [], (
        "parse() must keep dropping the authors — populating it here is what "
        "forks the collection, and the hotfix exists to avoid exactly that")


def test_plugin_io_prefers_the_hook(tmp_path):
    """The wiring itself: nothing else changed, so if `metadata` did not
    prefer `read_metadata` the backfill would be the only fix and new
    documents would still scan in blank."""
    container = _container(tmp_path)
    row = {"path": f"{container}::0", "native_id": "40000001"}
    assert plugin_io.metadata(SOURCE, row)["authors"] == \
        "Curie M; Meitner L; The Widget Study Group"


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
