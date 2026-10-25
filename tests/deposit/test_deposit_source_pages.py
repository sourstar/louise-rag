"""tests/deposit/test_deposit_source_pages.py — a URL source's page KEPT AS
FETCHED (06 §6.7, amended 2026-10-25, operator: "Keep all"; CLAUDE.md
invariant 11).

The page's bytes are the deposit, byte-exact; `origin_hash` is their hash
and `origin` locates them; the record is parsed from them, so a re-parse of
the same bytes reproduces it; an identical refetch writes nothing and a
changed page replaces the whole; what the page says about itself lands in
the record; and the text — so the chunks — is what it was when only the
text was kept. Local HTTP server, no network (test_deposit_sources.py).
"""
import json
import time
from contextlib import closing
from pathlib import Path

import pytest

from app import collections as collections_mod
from app import fts, layers, plugin_io
from app.hold import deposit, sources
from app.plugins.base import DocRef, RawDoc
from app.plugins.deposit import html, source
from tests.deposit.test_deposit_identity import make, node  # noqa: F401
from tests.deposit.test_deposit_sources import (  # noqa: F401
    accepted,
    charged,
    running,
    site,
)

RICH = (
    b'<!DOCTYPE html>\n<html lang="it-IT"><head><meta charset="utf-8">'
    b"<title>  La carbonara,\n  davvero </title>"
    b'<meta name="description" content="Come si fa la carbonara.">'
    b'<meta name="keywords" content="pasta, guanciale; uova">'
    b'<meta name="author" content="Ada Cuoca">'
    b'<meta property="article:published_time" content="2024-03-05T10:00:00+01:00">'
    b'<meta property="article:modified_time" content="2025-01-02T08:00:00Z">'
    b'<meta property="og:title" content="Carbonara (OG)">'
    b'<meta property="og:image" content="https://site.example/a.jpg">'
    b'<meta property="og:image" content="https://site.example/b.jpg">'
    b'<link rel="canonical" href="https://site.example/carbonara">'
    b'<link rel="alternate" hreflang="en" href="https://site.example/en/carbonara">'
    b'<link rel="stylesheet" href="/s.css">'
    b'<script type="application/ld+json">{"@type": "Recipe", "name": "Carbonara"}</script>'
    b"<style>.x{}</style></head><body><svg><title>icona</title></svg>"
    b"<h1>Carbonara</h1><p>Guanciale, pecorino, uova.</p>"
    b"<script>track()</script></body></html>")


def _row(name: str, key: str):
    c = collections_mod.by_name(name)
    with closing(deposit._connect(c, write=False)) as con:
        return c, dict(deposit._row(con, key))


def _record(c, row):
    return plugin_io.canonical_doc(c.plugin_instance(), row)


def _subscribe(node, site, page="page.html", interval=60):  # noqa: F811
    make(node, "alice")
    url = f"{site.base}/{page}"
    sources.add("alice", url, interval)
    return url, sources.source_key("alice", url)


# ---- kept byte-exact, hashed and located -------------------------------------

def test_the_page_is_kept_byte_exact_and_is_the_records_origin(node, site, accepted,  # noqa: F811
                                                               charged, running):  # noqa: F811
    url, key = _subscribe(node, site)
    assert sources.tick() == 1
    page = (site.root / "page.html").read_bytes()
    c, row = _row("alice", key)
    stored = Path(row["path"])
    assert stored.read_bytes() == page                      # the page, not its text
    assert source.read_sidecar(stored.parent)["form"] == "html"
    assert row["txt_hash"] == source.origin_hash(page)
    raw = c.plugin_instance().fetch(DocRef("deposit", row["native_id"], row["path"]))
    assert raw.raw_hash == source.origin_hash(page)         # 09 §9.5's origin_hash
    origin = plugin_io.origin_locator(c.plugin_instance(), row["path"], c.corpus_dir)
    assert origin == f"{row['native_id']}/{key}"            # 09 §9.5's origin
    assert sum(charged) == len(page)                        # invariant 5, as before


def test_a_page_that_is_not_utf8_is_kept_as_its_bytes(node, site, accepted, charged,  # noqa: F811
                                                      running):  # noqa: F811
    page = "<p>Perché è così caro?</p>".encode("latin-1")
    (site.root / "page.html").write_bytes(page)
    url, key = _subscribe(node, site)
    assert sources.tick() == 1
    _, row = _row("alice", key)
    assert Path(row["path"]).read_bytes() == page
    assert "caro" in deposit.show("alice", key)["content"]


# ---- refetching ----------------------------------------------------------------

def test_an_identical_refetch_changes_nothing(node, site, accepted, charged, running):  # noqa: F811
    url, key = _subscribe(node, site, interval=1)
    t0 = time.time()
    assert sources.tick(now=t0) == 1
    c, before = _row("alice", key)
    d = Path(before["path"]).parent
    files = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()}
    stamp = sources.last_update("alice", url)
    assert sources.tick(now=t0 + 120) == 1                  # fetched again...
    _, after = _row("alice", key)
    assert after == before                                  # ...and nothing written
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in d.iterdir()} == files
    assert deposit.keys("alice") == [key]
    assert sum(charged) == 2 * len((site.root / "page.html").read_bytes())
    assert sources.last_update("alice", url) > stamp        # the fetch is recorded


def test_a_changed_page_replaces_the_whole(node, site, accepted, charged, running):  # noqa: F811
    url, key = _subscribe(node, site, interval=1)
    t0 = time.time()
    sources.tick(now=t0)
    _, before = _row("alice", key)
    new = b"<html><body><p>Amatriciana, ora.</p></body></html>"
    (site.root / "page.html").write_bytes(new)
    assert sources.tick(now=t0 + 120) == 1
    _, after = _row("alice", key)
    assert after["native_id"] != before["native_id"]
    assert not Path(before["path"]).parent.exists()         # the old one leaves no trace
    assert Path(after["path"]).read_bytes() == new
    assert after["txt_hash"] == source.origin_hash(new)
    assert deposit.keys("alice") == [key]


def test_a_refresh_that_fails_or_is_too_large_keeps_the_page_it_had(node, site, accepted,  # noqa: F811
                                                                    charged, running,  # noqa: F811
                                                                    monkeypatch):
    url, key = _subscribe(node, site, interval=1)
    t0 = time.time()
    sources.tick(now=t0)
    _, before = _row("alice", key)
    (site.root / "page.html").write_bytes(b"<p>" + b"x" * 4096 + b"</p>")
    monkeypatch.setattr(sources, "MAX_PAGE_BYTES", 1024)    # 50 MB, scaled down
    assert sources.tick(now=t0 + 120) == 0
    _, after = _row("alice", key)
    assert after == before and Path(after["path"]).exists()


# ---- the record, parsed from the kept bytes -------------------------------------

def test_a_reparse_of_the_kept_bytes_or_a_refetch_reproduces_the_record(node, site, accepted,  # noqa: F811
                                                                        charged, running):  # noqa: F811
    """What records-vs-origin does for a published collection (03 §3.2),
    done here by hand: a deposit collection runs no such check (02 §2.9),
    but its record must be reproducible from its origin all the same."""
    (site.root / "rich.html").write_bytes(RICH)
    url, key = _subscribe(node, site, page="rich.html")
    sources.tick()
    c, row = _row("alice", key)
    record = _record(c, row)
    plugin = source.DepositSource()
    again = plugin.parse(plugin.fetch(DocRef("deposit", row["native_id"], row["path"])))
    assert again == record
    refetched = sources._get(url)                           # the origin, fetched again
    ref = DocRef("deposit", row["native_id"], row["path"])
    from_origin = plugin.parse(RawDoc(ref=ref, raw_hash=source.origin_hash(refetched),
                                      data=refetched))
    assert from_origin == record and from_origin.raw_hash == row["txt_hash"]


def test_what_the_page_says_about_itself_lands_in_the_record(node, site, accepted,  # noqa: F811
                                                             charged, running):  # noqa: F811
    (site.root / "rich.html").write_bytes(RICH)
    url, key = _subscribe(node, site, page="rich.html")
    sources.tick()
    c, row = _row("alice", key)
    doc = _record(c, row)
    assert doc.title == "La carbonara, davvero"
    assert doc.language == "it" and row["lang"] == "it"
    assert doc.creators == ["Ada Cuoca"] and doc.date == "2024-03-05"
    assert doc.subjects == ["pasta", "guanciale", "uova"]
    e = doc.extra
    assert set(e) == set(source.EXTRA_KEYS)
    assert (e["url"], e["key"]) == (url, key)
    assert e["html_lang"] == "it-IT"
    assert e["html_description"] == "Come si fa la carbonara."
    assert e["html_keywords"] == "pasta, guanciale; uova"
    assert e["html_published"] == "2024-03-05T10:00:00+01:00"
    assert e["html_modified"] == "2025-01-02T08:00:00Z"
    assert e["html_canonical"] == "https://site.example/carbonara"
    og = [(m["property"], m["content"]) for m in e["html_meta"] if m.get("property", "").startswith("og:")]
    assert og == [("og:title", "Carbonara (OG)"), ("og:image", "https://site.example/a.jpg"),
                  ("og:image", "https://site.example/b.jpg")]       # every one, in order
    assert {"charset": "utf-8"} in e["html_meta"]
    assert {"rel": "alternate", "hreflang": "en",
            "href": "https://site.example/en/carbonara"} in e["html_links"]
    assert json.loads(e["html_jsonld"][0]) == {"@type": "Recipe", "name": "Carbonara"}
    json.dumps(e)                                            # a record's extra is JSON
    text = deposit.show("alice", key)["content"]
    assert "track()" not in text and ".x{}" not in text and "Recipe" not in text
    assert any(h for h in fts.search("it", "guanciale", collection=c))


def test_every_deposit_has_the_same_extra_keys_a_page_too(node, site, accepted,  # noqa: F811
                                                          charged, running):  # noqa: F811
    url, key = _subscribe(node, site)
    sources.tick()
    deposit.add("alice", "note.txt", b"una nota")
    c, page = _row("alice", key)
    _, note = _row("alice", "note.txt")
    a, b = _record(c, page).extra, _record(c, note).extra
    assert set(a) == set(b) == set(source.EXTRA_KEYS)
    assert all(b[k] is None for k in source.HTML_KEYS)


# ---- the text, and so the chunks, unchanged --------------------------------------

def _as_before(page: bytes, key: str):
    """What the sources thread deposited before 2026-10-25 — the page's text
    as a `.txt` deposit — and what that deposit parsed to."""
    text = sources.html_to_text(page.decode("utf-8", "replace"))
    return text.encode("utf-8"), source.sections_of_text(source.decode_text(key, text.encode()))


def test_the_text_and_the_chunks_are_what_they_were(node, site, accepted, charged,  # noqa: F811
                                                    running):  # noqa: F811
    url, key = _subscribe(node, site)
    sources.tick()
    page = (site.root / "page.html").read_bytes()
    old_bytes, old_sections = _as_before(page, key)
    # the fixture page's text, as LocalRecall's conversion has always given it
    assert old_bytes == b"Ricette\n\nCarbonara\n\nGuanciale, pecorino, uova."
    c, row = _row("alice", key)
    assert deposit.show("alice", key)["content"].encode("utf-8") == old_bytes
    doc = _record(c, row)
    assert doc.sections == old_sections
    assert layers.chunks_for_document(c, row, doc.sections) == \
        layers.chunks_for_document(c, row, old_sections)


def test_a_source_deposited_as_text_is_replaced_by_its_page_with_the_same_chunks(
        node, site, accepted, charged, running):  # noqa: F811
    """A source fetched before 2026-10-25 holds the page's text; its next
    refresh keeps the page instead — once, since its hash is the text's —
    and the chunks' text does not move."""
    url, key = _subscribe(node, site, interval=1)
    page = (site.root / "page.html").read_bytes()
    old_bytes, _ = _as_before(page, key)
    deposit.add("alice", key, old_bytes, url=url)           # the deposit as it was
    c, legacy = _row("alice", key)
    old_chunks = [ch["text"] for ch in layers.chunks_for_document(
        c, legacy, _record(c, legacy).sections)]
    assert sources.tick(now=time.time()) == 1
    _, now_row = _row("alice", key)
    assert Path(now_row["path"]).read_bytes() == page
    assert [ch["text"] for ch in layers.chunks_for_document(
        c, now_row, _record(c, now_row).sections)] == old_chunks


def test_a_collection_created_at_parser_2_keeps_parser_2s_record(node, site, accepted,  # noqa: F811
                                                                 charged, running,  # noqa: F811
                                                                 monkeypatch):
    """09 §9.5: `extra`'s keys are fixed for a collection's life. A deposit
    collection whose Codex names parser 2 keeps its page as fetched like any
    other, but its record keeps parser 2's shape — the page's text, titled
    by its key, `extra` without `html_*`."""
    monkeypatch.setattr(source.DepositSource, "PARSER_VERSION", 2)
    make(node, "alice")
    monkeypatch.setattr(source.DepositSource, "PARSER_VERSION", 3)
    (site.root / "rich.html").write_bytes(RICH)
    url = f"{site.base}/rich.html"
    sources.add("alice", url)
    sources.tick()
    key = sources.source_key("alice", url)
    c, row = _row("alice", key)
    assert c.codex.parser.version == 2
    assert Path(row["path"]).read_bytes() == RICH
    doc = _record(c, row)
    assert set(doc.extra) == set(source.EXTRA_KEYS_2)
    assert (doc.title, doc.language, doc.creators, doc.date, doc.subjects) == \
        (key, "und", [], None, [])
    assert doc.sections == _as_before(RICH, key)[1]


# ---- html.py --------------------------------------------------------------------

@pytest.mark.parametrize("tag,lang", [("it-IT", "it"), ("EN", "en"), ("fil", "fil"),
                                      ("x-default", "und"), ("", "und"), (None, "und")])
def test_the_language_is_the_tags_primary_subtag(tag, lang):
    assert html.language_of(tag) == lang


@pytest.mark.parametrize("value,date", [("2024-03-05T10:00:00Z", "2024-03-05"),
                                        ("2024-03", "2024-03"), ("2024", "2024"),
                                        ("5 marzo 2024", None), (None, None)])
def test_a_date_is_taken_as_far_as_it_goes(value, date):
    assert html.date_of(value) == date


def test_a_title_inside_an_svg_is_not_the_pages():
    h = html.head(b"<html><body><svg><title>icona</title></svg><title>Vera</title></body></html>")
    assert h.title == "Vera"


def test_a_page_with_nothing_to_say_about_itself_is_titled_by_its_key():
    ref = DocRef("deposit", "7", "/nowhere/source-a-x.txt")
    data = b"<p>solo testo</p>"
    orig = source.read_sidecar
    source.read_sidecar = lambda d: {"key": "source-a-x.txt", "form": "html"}
    try:
        doc = source.DepositSource().parse(RawDoc(ref=ref, raw_hash="", data=data))
    finally:
        source.read_sidecar = orig
    assert (doc.title, doc.language, doc.creators, doc.date) == ("source-a-x.txt", "und", [], None)
    assert all(doc.extra[k] in (None, []) for k in source.HTML_KEYS)
