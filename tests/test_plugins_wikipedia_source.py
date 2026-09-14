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
from app.plugins.wikipedia.source import native_id_of, split_locator
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


def nid(page_id: int, lang: str = "en") -> str:
    """The native id of an English page read from the dump (parser 4)."""
    return str(native_id_of(lang, page_id))


def _page(pid: int, title: str, text: str, redirect: bool = False) -> str:
    red = f'<redirect title="target" />' if redirect else ""
    return (f"  <page>\n    <title>{title}</title>\n    <ns>0</ns>\n"
            f"    <id>{pid}</id>\n    {red}\n"
            f"    <revision>\n      <id>{pid}00</id>\n"
            f"      <text>{text}</text>\n    </revision>\n  </page>\n")


@pytest.fixture
def corpus(tmp_path) -> Path:
    """enwiki/latest/... layout with two bz2 streams and a real index."""
    d = tmp_path / "wiki-corpus" / "enwiki" / "latest"
    d.mkdir(parents=True)
    stream1 = bz2.compress(
        (_page(1, "The Moon", ARTICLE_MOON)
         + _page(2, "Moon (disambiguation)", REDIRECT, redirect=True)).encode())
    stream2 = bz2.compress(
        (_page(3, "Cheese", ARTICLE_CHEESE)
         + _page(4, "Category:Foods", "category page text")).encode())
    dump = d / "enwiki-latest-pages-articles-multistream.xml.bz2"
    dump.write_bytes(stream1 + stream2)
    index_lines = [
        f"0:1:The Moon",
        f"0:2:Moon (disambiguation)",
        f"{len(stream1)}:3:Cheese",
        f"{len(stream1)}:4:Category:Foods",
    ]
    index = d / "enwiki-latest-pages-articles-multistream-index.txt.bz2"
    index.write_bytes(bz2.compress(("\n".join(index_lines) + "\n").encode()))
    return tmp_path / "wiki-corpus"


def test_discover_streams_the_index_with_stream_bounds(corpus):
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus)))
    by_id = {r.native_id: r for r in refs}
    # Category: namespace filtered at discover; articles + redirect kept
    assert set(by_id) == {nid(1), nid(2), nid(3)}
    dump, start, end = split_locator(by_id[nid(1)].locator)
    assert start == 0 and end > 0, "first stream must know where it ends"
    _, start3, end3 = split_locator(by_id[nid(3)].locator)
    assert start3 == end and end3 == 0, "last stream runs to EOF"
    assert by_id[nid(1)].extra["lang"] == "en"


def test_pages_of_one_stream_share_one_decompression(corpus, monkeypatch):
    """A stream holds ~100 pages and every stage walks them in order: the
    second page of a stream must not decompress and parse it again."""
    import bz2 as _bz2
    from app.plugins.wikipedia import source as wsrc
    calls = []
    real = _bz2.decompress
    monkeypatch.setattr(wsrc.bz2, "decompress", lambda blob: (calls.append(1), real(blob))[1])
    wsrc._stream_cache.clear()
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=corpus))}
    a = SOURCE.fetch(refs[nid(1)])
    b = SOURCE.fetch(refs[nid(2)])          # same first stream
    assert len(calls) == 1 and a.raw_hash != b.raw_hash
    SOURCE.fetch(refs[nid(3)])              # the second stream
    assert len(calls) == 2
    SOURCE.fetch(refs[nid(1)])              # back to the first: one slot, re-read
    assert len(calls) == 3


def test_fetch_decompresses_only_one_stream_and_parse_cleans_wikitext(corpus):
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=corpus))}
    doc = SOURCE.parse(SOURCE.fetch(refs[nid(3)]))
    assert doc.uid == f"wikipedia:{nid(3)}" and doc.title == "Cheese"
    assert doc.extra["page_id"] == "3", "the page id the native id carries"
    headings = [h for h, _ in doc.sections]
    assert headings == ["", "Production"]
    lead = doc.sections[0][1]
    assert "dairy product" in lead and "[[" not in lead, "markup must be stripped"

    moon = SOURCE.parse(SOURCE.fetch(refs[nid(1)]))
    assert all(h != "References" for h, _ in moon.sections), \
        "boilerplate sections must not become chunks"
    assert "27.3 days" in dict(moon.sections)["Orbit"]


def test_redirects_yield_no_sections(corpus):
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=corpus))}
    doc = SOURCE.parse(SOURCE.fetch(refs[nid(2)]))
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

    uid = f"wikipedia:{nid(1)}"
    row = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    worker.stage_rdf(con, row, collection)
    refreshed = con.execute("SELECT * FROM documents WHERE uid=?", (uid,)).fetchone()
    assert refreshed["title"] == "The Moon" and refreshed["rdf_done"] == 1

    chunks = worker._load_chunks(refreshed, collection)
    assert chunks and "natural satellite" in chunks[0]["text"]


def test_missing_index_is_an_honest_error_ref(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "trwiki-latest-pages-articles-multistream.xml.bz2").write_bytes(
        bz2.compress(b"<page></page>"))
    refs = list(SOURCE.discover(DiscoverState(corpus_root=d)))
    assert len(refs) == 1 and "index missing" in refs[0].error


def test_wikitext_edge_cases():
    assert to_sections("") == []
    # template-heavy text still yields clean prose
    sections = to_sections("{{Infobox|a=b}}\nSome '''bold''' prose about {{convert|5|km}}.")
    assert sections and "bold" in sections[0][1]
    assert "{{" not in sections[0][1]


def test_two_languages_live_in_one_collection_apart_by_id(tmp_path):
    """The collection holds wikivec's languages (operator, 2026-09-14). The
    same page id in two wikis is two documents: the native id carries the
    language, so neither overwrites the other — the reason a second
    language used to be refused at discovery."""
    root = tmp_path / "corpus"
    for wiki in ("enwiki", "itwiki"):
        d = root / wiki
        d.mkdir(parents=True)
        stream = bz2.compress(_page(7, f"Seven ({wiki})", "Seven.").encode())
        (d / f"{wiki}-latest-pages-articles-multistream.xml.bz2").write_bytes(stream)
        (d / f"{wiki}-latest-pages-articles-multistream-index.txt.bz2").write_bytes(
            bz2.compress(b"0:7:Seven\n"))
    refs = {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=root))}
    assert set(refs) == {nid(7, "en"), nid(7, "it")}
    docs = {SOURCE.parse(SOURCE.fetch(r)).language: SOURCE.parse(SOURCE.fetch(r))
            for r in refs.values()}
    assert docs["en"].title == "Seven (enwiki)" and docs["it"].title == "Seven (itwiki)"


def test_a_wiki_outside_the_collections_languages_is_reported(tmp_path):
    """simplewiki is not one of wikivec's languages, and it used to pass as
    "en" and share enwiki's page ids."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "simplewiki-20260101-pages-articles-multistream.xml.bz2").write_bytes(b"x")
    (root / "simplewiki-20260101-pages-articles-multistream-index.txt.bz2").write_bytes(b"y")
    refs = list(SOURCE.discover(DiscoverState(corpus_root=root)))
    assert len(refs) == 1 and refs[0].scope == "container"
    assert "'simple' is not one of the collection's languages" in refs[0].error
