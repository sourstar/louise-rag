"""A CLOSED pack: `pack build --until` keeps what existed at the origin on
the cutoff date — gutenberg by release date, pubmed by DateRevised — and
a resume under another cutoff is refused."""
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app import collections as collections_mod, db, discovery
from app.pack import build as pack_build, cutoff, manifest as manifest_mod
from app.plugins.pubmed import medline


def test_normalise_and_rules():
    assert cutoff.normalise("2026-3-7") == "2026-03-07"
    assert cutoff.normalise("2026") == "2026-01-01"
    assert cutoff.normalise("2026-06") == "2026-06-01"
    assert cutoff.normalise("junk") is None and cutoff.normalise(None) is None
    assert cutoff.check_until("2026-06-30") == "2026-06-30"
    with pytest.raises(ValueError):
        cutoff.check_until("June 2026")
    # gutenberg's cutoff key is `extra.issued` — PG's release date for its
    # own edition. NOT §9.5's `date`, which is the WORK's publication date
    # and is null here because PG does not carry it.
    g = {"date": None, "extra": {"issued": "2026-07-01"}}
    assert cutoff.excluded("gutenberg", g, "2026-06-30").startswith("after cutoff")
    assert cutoff.excluded("gutenberg", {"date": None, "extra": {"issued": "2026-06-30"}},
                           "2026-06-30") is None
    assert "no cutoff date" in cutoff.excluded("gutenberg", {"date": None, "extra": {}},
                                               "2026-06-30")
    p = {"date": "1999-05", "extra": {"revised": "2026-08-01"}}
    assert cutoff.excluded("pubmed", p, "2026-06-30").startswith("after cutoff")
    assert cutoff.excluded("pubmed", {"date": "1999", "extra": {}},
                           "2026-06-30") is None, "fallback: the publication date"
    assert cutoff.excluded("wikipedia", {"extra": {"revised": "2030-01-01"}}, "2026-06-30") is None
    assert cutoff.excluded("gutenberg", g, None) is None


def test_a_null_publication_date_does_not_exclude_the_whole_corpus():
    """THE TRAP THIS NEARLY WALKED INTO. `date` became the work's
    publication date, which PG does not have, so it is null for every
    gutenberg record. `record_key` used to read `date` — so a dated
    snapshot would have found "no cutoff date" on all 79,320 of them and
    excluded the entire corpus. The key reads `extra.issued` now."""
    rec = {"date": None, "extra": {"issued": "2006-02-22", "bookshelves": ""}}
    assert cutoff.record_key("gutenberg", rec) == "2006-02-22"
    assert cutoff.excluded("gutenberg", rec, "2026-06-30") is None, \
        "a 2006 release is inside a 2026 snapshot, null `date` notwithstanding"


def test_medline_parses_the_revised_date():
    xml = """<PubmedArticle><MedlineCitation><PMID>1</PMID>
      <DateCompleted><Year>2020</Year><Month>1</Month><Day>2</Day></DateCompleted>
      <DateRevised><Year>2026</Year><Month>07</Month><Day>15</Day></DateRevised>
      <Article><ArticleTitle>T</ArticleTitle><Journal><JournalIssue><PubDate><Year>1999</Year></PubDate></JournalIssue></Journal></Article>
    </MedlineCitation></PubmedArticle>"""
    meta = medline.parse_article(ET.fromstring(xml))
    assert meta["revised"] == "2026-07-15" and meta["date"] == "1999"
    xml2 = xml.replace("<DateRevised><Year>2026</Year><Month>07</Month><Day>15</Day></DateRevised>", "")
    assert medline.parse_article(ET.fromstring(xml2))["revised"] == "2020-01-02"


def _stamp_issued(corpus_dir: Path, dates: dict[int, str]) -> None:
    for book_id, date in dates.items():
        rdf = corpus_dir / str(book_id) / f"pg{book_id}.rdf"
        text = rdf.read_text(encoding="utf-8")
        assert "dcterms:issued" not in text
        text = text.replace("<dcterms:title>", f"<dcterms:issued>{date}</dcterms:issued>\n    <dcterms:title>", 1)
        rdf.write_text(text, encoding="utf-8")


def test_gutenberg_pack_until_keeps_only_books_released_by_the_cutoff(isolated_config, tmp_path, corpus_dir):
    _stamp_issued(corpus_dir, {10001: "2025-12-01", 10002: "2026-06-30", 10003: "2026-07-01"})
    d = tmp_path / "g"
    origin = collections_mod.Collection(
        name="g", plugin="gutenberg", profile=None, embed_mode_default=None, corpus_dir=corpus_dir,
        manifest_path=d / "manifest.db", fts_dir=d / "fts", lance_dir=d / "lancedb")
    con = db.connect(origin)
    discovery.sync_collection(con, origin)
    con.close()
    pack_dir = tmp_path / "packs" / "g-2026-06"
    r = pack_build.build(origin, pack_dir, until="2026-06-30", records_per_shard=1, sign=False,
                         log=lambda *_: None)
    assert r.records == 2 and r.skipped == 1
    m = manifest_mod.load(pack_dir)
    assert m["snapshot"] == "2026-06-30" and m["closed"] is True
    assert m["origin"]["cutoff"] == {"until": "2026-06-30", "rule": "issued <= until"}
    uids = {s["ids"][0] for s in m["shards"]}
    assert uids == {"gutenberg:10001", "gutenberg:10002"}
    import sqlite3
    b = sqlite3.connect(pack_dir / ".build" / "build.db")
    reason = b.execute("SELECT reason FROM skipped WHERE uid='gutenberg:10003'").fetchone()[0]
    assert reason.startswith("cutoff: after cutoff: 2026-07-01 > 2026-06-30")
    # the same directory refuses another cutoff; the same cutoff resumes
    with pytest.raises(pack_build.BuildError, match="different cutoff"):
        pack_build.build(origin, pack_dir, until="2026-12-31", sign=False, log=lambda *_: None)
    with pytest.raises(pack_build.BuildError, match="different cutoff"):
        pack_build.build(origin, pack_dir, sign=False, log=lambda *_: None)
    assert pack_build.build(origin, pack_dir, until="2026-06-30", records_per_shard=1, sign=False,
                            log=lambda *_: None).records == 2
    # a record with no release date is excluded, never silently kept
    open_dir = tmp_path / "packs" / "g-open"
    assert pack_build.build(origin, open_dir, sign=False, log=lambda *_: None).records == 3
    assert manifest_mod.load(open_dir)["closed"] is False
