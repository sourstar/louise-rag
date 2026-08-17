"""Wikipedia plugin — multistream addressing, wikitext cleaning, namespace
and redirect handling, and the full worker-stage path over a synthesized
two-stream dump with its index (built by the fixture, exactly the real
layout: concatenated independent bz2 streams + offset:id:title index).
"""
import bz2
from pathlib import Path

import pytest

from app import collections as collections_mod, db, discovery
from app.plugins.base import DiscoverState
from app.plugins.wikipedia import SOURCE
from app.plugins.wikipedia.source import split_locator
from app.plugins.wikipedia.wikitext import to_sections

ARTICLE_MOON = """'''The Moon''' is [[Earth]]'s only natural satellite.

== Orbit ==
The Moon orbits Earth every 27.3 days.<ref>Some citation</ref>

== References ==
* citation noise that must not become a chunk
"""

ARTICLE_CHEESE = """'''Cheese''' is a [[dairy product]] made from milk.

== Production ==
Milk is curdled using [[rennet]] or acid.
"""

REDIRECT = "#REDIRECT [[The Moon]]"


def _page(pid: int, title: str, text: str, redirect: bool = False) -> str:
    red = f'<redirect title="target" />' if redirect else ""
    return (f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n"
            f"    <id>{pid}</id>\n    {red}\n"
            f"    <revision>\n      <id>{pid}00</id>\n"
            f"      <text>{text}</text>\n    </revision>\n  </page>\n")


@pytest.fixture
def corpus(tmp_path) -> Path:
    """simplewiki/latest/... layout with two bz2 streams and a real index."""
    d = tmp_path / "wiki-corpus" / "simplewiki" / "latest"
    d.mkdir(parents=True)
    stream1 = bz2.compress(
        (_page(1, "The Moon", ARTICLE_MOON)
         + _page(2, "Moon (disambiguation)", REDIRECT, redirect=True)).encode())
    stream2 = bz2.compress(
        (_page(3, "Cheese", ARTICLE_CHEESE)
         + _page(4, "Category:Foods", "category page text")).encode())
    dump = d / "simplewiki-latest-pages-articles-multistream.xml.bz2"
    dump.write_bytes(stream1 + stream2)
    index_lines = [
        f"0:1:The Moon",
        f"0:2:Moon (disambiguation)",
        f"{len(stream1)}:3:Cheese",
        f"{len(stream1)}:4:Category:Foods",
    ]
    index = d / "simplewiki-latest-pages-articles-multistream-index.txt.bz2"
    index.write_bytes(bz2.compress(("\n".join(index_lines) + "\n").encode()))
    return tmp_path / "wiki-corpus"


def test_discover_streams_the_index_with_stream_bounds(corpus):
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    by_id = {r.native_id: r for r in refs}
    # Category: namespace filtered at discover; articles + redirect kept
    assert set(by_id) == {"1", "2", "3"}
    dump, start, end = split_locator(by_id["1"].locator)
    assert start == 0 and end > 0, "first stream must know where it ends"
    _, start3, end3 = split_locator(by_id["3"].locator)
    assert start3 == end and end3 == 0, "last stream runs to EOF"
    assert by_id["1"].extra["lang"] == "en"


def test_fetch_decompresses_only_one_stream_and_parse_cleans_wikitext(corpus):
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=corpus))}
    doc = SOURCE.parse(SOURCE.fetch(refs["3"]))
    assert doc.uid == "wikipedia:3" and doc.title == "Cheese"
    headings = [h for h, _ in doc.sections]
    assert headings == ["", "Production"]
    lead = doc.sections[0][1]
    assert "dairy product" in lead and "[[" not in lead, "markup must be stripped"

    moon = SOURCE.parse(SOURCE.fetch(refs["1"]))
    assert all(h != "References" for h, _ in moon.sections), \
        "boilerplate sections must not become chunks"
    assert "27.3 days" in dict(moon.sections)["Orbit"]


def test_redirects_yield_no_sections(corpus):
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=corpus))}
    doc = SOURCE.parse(SOURCE.fetch(refs["2"]))
    assert doc.sections == []


def test_worker_stages_run_end_to_end(tmp_path, corpus, isolated_config):
    from app import worker
    collection = collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    assert con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3

    row = con.execute("SELECT * FROM documents WHERE uid='wikipedia:1'").fetchone()
    worker.stage_rdf(con, row, collection)
    refreshed = con.execute("SELECT * FROM documents WHERE uid='wikipedia:1'").fetchone()
    assert refreshed["title"] == "The Moon" and refreshed["rdf_done"] == 1

    chunks = worker._load_chunks(refreshed, collection)
    assert chunks and "natural satellite" in chunks[0]["text"]


def test_missing_index_is_an_honest_error_ref(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "nowiki-latest-pages-articles-multistream.xml.bz2").write_bytes(
        bz2.compress(b"<page></page>"))
    refs = list(SOURCE.discover(DiscoverState(corpus_root=d)))
    assert len(refs) == 1 and "index missing" in refs[0].error


def test_wikitext_edge_cases():
    assert to_sections("") == []
    # template-heavy text still yields clean prose
    sections = to_sections("{{Infobox|a=b}}\nSome '''bold''' prose about {{convert|5|km}}.")
    assert sections and "bold" in sections[0][1]
    assert "{{" not in sections[0][1]
