"""tests/ask/test_freshness.py — `freshness` (05 §5.3): the top five of a
list asked of their origins now — current, newer at the source (the
current text beside the lists as a fact; a red collection's hash only),
unreachable, unavailable — and the three plugins' `current()`: the form
the copy holds from Gutenberg's cache, the article E-utilities serves,
the page the export API writes, the archive layer refused."""
import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from app import collections as collections_mod, db, query_service
from app.ask import freshness, results
from app.hold import records as hold_records
from app.plugins.base import CanonicalDoc, DocRef, NoCurrentForm, RawDoc
from tests.deposit.test_deposit_identity import node  # noqa: F401


def a_doc(uid, title, body):
    return CanonicalDoc(uid=uid, title=title, creators=[], language="en", date=None, subjects=[], keys=[],
                        extra={}, sections=[("Body", body)], raw_hash="00" * 16)


class FakePlugin:
    """`serve`: native_id -> (title, body) now, or the exception the origin raises."""
    def __init__(self, serve):
        self.serve = serve

    def current(self, ref, *, client, workdir):
        got = self.serve[ref.native_id]
        if isinstance(got, Exception):
            raise got
        assert workdir.is_dir(), "a working directory for whatever the plugin writes"
        return RawDoc(ref=ref, raw_hash="11" * 16, data=repr(got).encode())

    def parse(self, raw):
        title, body = self.serve[raw.ref.native_id]
        return a_doc(f"g:{raw.ref.native_id}", title, body)


def a_manifest(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE documents (uid TEXT PRIMARY KEY, content_hash TEXT, path TEXT)")
    con.executemany("INSERT INTO documents VALUES (?,?,?)", rows)
    return con


def h(title, body):
    return hold_records.content_hash(title, [["Body", body]])


def test_each_state_and_the_fact_beside_the_lists():
    plugin = FakePlugin({"1": ("A", "same"), "2": ("B", "changed"), "3": ConnectionError("down"),
                         "4": NoCurrentForm("a static archive"), "6": ("F", "x" * 30_000)})
    c = SimpleNamespace(name="g", plugin="g", plugin_instance=lambda: plugin)
    con = a_manifest([("g:1", h("A", "same"), "/p/1"), ("g:2", h("B", "old"), "/p/2"), ("g:3", h("C", "c"), "/p/3"),
                      ("g:4", h("D", "d"), "/p/4"), ("g:5", None, "/p/5"), ("g:6", h("F", "old"), "/p/6"),
                      ("g:7", h("G", "g"), "/p/7")])
    hits = [{"uid": f"g:{i}", "origin_url": f"https://o/{i}"} for i in (1, 2, 3, 4, 5, 6, 7)]
    out = freshness.check(c, hits, con=con, red=False, client=object())
    assert out["checked"] == 5, "the top five, no more"
    assert [x.get("freshness") for x in hits] == ["current", "newer-at-source", "unreachable", "unavailable",
                                                  "unavailable", None, None]
    assert "down" in hits[2]["freshness_why"] and "archive" in hits[3]["freshness_why"]
    assert "built before records" in hits[4]["freshness_why"]
    (fact,) = out["facts"]
    assert (fact["uid"], fact["source"], fact["kind"], fact["title"]) == ("g:2", "origin", "current-version", "B")
    assert fact["text"] == "Body\n\nchanged" and fact["truncated"] is False
    assert fact["content_hash"] == h("B", "changed") and fact["held_content_hash"] == h("B", "old")
    assert "origin_url" not in fact
    assert out["states"] == {"current": 1, "newer-at-source": 1, "unreachable": 1, "unavailable": 2}
    # red: the hash is compared and the origin's page linked; the text never leaves
    out = freshness.check(c, [{"uid": "g:2", "origin_url": "https://o/2"}], con=con, red=True, client=object())
    (fact,) = out["facts"]
    assert fact["origin_url"] == "https://o/2" and "text" not in fact and fact["content_hash"] == h("B", "changed")
    # a long current text is cut and says so
    out = freshness.check(c, [{"uid": "g:6"}], con=con, red=False, client=object())
    (fact,) = out["facts"]
    assert fact["truncated"] is True and len(fact["text"]) <= freshness.TEXT_CHARS + 8
    # a plugin without the method
    plain = SimpleNamespace(name="p", plugin="p", plugin_instance=lambda: SimpleNamespace())
    hits = [{"uid": "g:1"}]
    freshness.check(plain, hits, con=con, red=False, client=object())
    assert hits[0]["freshness"] == "unavailable" and "no per-record current form" in hits[0]["freshness_why"]


def test_the_search_asks_the_top_five_of_each_list_and_puts_what_changed_beside(node, monkeypatch):  # noqa: F811
    legacy = SimpleNamespace(name="gutenberg", plugin="gutenberg", codex=None)
    monkeypatch.setattr(query_service, "resolve_collections", lambda names, **kw: [legacy])
    monkeypatch.setattr(db, "connect", lambda c: sqlite3.connect(":memory:"))
    seen = []

    def fake_check(collection, hits, *, con, red):
        seen.append(([x["uid"] for x in hits], red))
        for x in hits:
            x["freshness"] = "current"
        hits[0]["freshness"] = "newer-at-source"
        return {"facts": [{"source": "origin", "kind": "current-version", "uid": hits[0]["uid"]}],
                "checked": len(hits), "states": {"current": len(hits) - 1, "newer-at-source": 1}}
    monkeypatch.setattr(freshness, "check", fake_check)

    def run(names, query, k, mode, translate, **kw):
        return {"hits": [{"chunk_id": f"gutenberg:{i}:0:1", "book_id": i, "source_collection": "gutenberg",
                          "text": "t", "leg_ranks": {"text": i}} for i in range(1, 9)],
                "reranked": False, "timings": {}}
    out = results.search("q", limit=8, freshness=True, root=node, run=run)
    assert seen == [([f"gutenberg:{i}" for i in range(1, 6)], False)]
    hits = out["lists"][0]["hits"]
    assert [x.get("freshness") for x in hits] == ["newer-at-source"] + ["current"] * 4 + [None] * 3
    assert out["facts"][-1]["uid"] == "gutenberg:1"
    assert out["freshness"] == {"checked": 5, "states": {"current": 4, "newer-at-source": 1}}
    assert results.search("q", root=node, run=run)["freshness"] is None, "off by default"


# ---- the plugins' current() ----------------------------------------------------------

class _Client:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        body = self.routes(url, params) if callable(self.routes) else self.routes[url]
        return SimpleNamespace(content=body, raise_for_status=lambda: None)


def test_gutenberg_fetches_the_form_the_copy_holds_from_the_cache(tmp_path):
    plugin = collections_mod.PLUGIN_REGISTRY["gutenberg"]
    book_dir = tmp_path / "corpus" / "1"
    book_dir.mkdir(parents=True)
    text = b"The Project Gutenberg eBook of Moby Dick\n\nCall me Ishmael. Some years ago.\n"
    (book_dir / "pg1.txt").write_bytes(text)
    served = {"https://www.gutenberg.org/cache/epub/1/pg1.txt": text,
              "https://www.gutenberg.org/cache/epub/1/pg1.rdf": b"<rdf/>"}
    client = _Client(served)
    raw = plugin.current(DocRef("gutenberg", "1", str(book_dir)), client=client, workdir=tmp_path / "w")
    assert raw.path == tmp_path / "w" / "1" / "pg1.txt" and raw.ref.locator == str(tmp_path / "w" / "1")
    assert raw.raw_hash == hashlib.blake2b(text, digest_size=16).hexdigest()
    assert sorted(u for u, _ in client.calls) == sorted(served), "the text in the copy's form, and the catalogue entry"
    doc = plugin.parse(raw)
    assert "Ishmael" in " ".join(b for _, b in doc.sections)
    # the illustrated epub is asked for by the origin's name and kept under the local one
    (book_dir / "pg1.txt").unlink()
    (book_dir / "1-images.epub").write_bytes(b"not an epub")
    (book_dir / "pg1.txt").write_bytes(text)                      # the unreadable epub is skipped: txt it is
    client = _Client(served)
    raw = plugin.current(DocRef("gutenberg", "1", str(book_dir)), client=client, workdir=tmp_path / "w2")
    assert raw.path.name == "pg1.txt"


def test_pubmed_reads_the_article_efetch_serves():
    plugin = collections_mod.PLUGIN_REGISTRY["pubmed"]
    root = ET.fromstring(Path("tests/fixtures/pubmed-mini/pubmed25n0001.xml").read_bytes())
    article = root.find("PubmedArticle")
    pmid = article.findtext("MedlineCitation/PMID")
    served = lambda url, params: ET.tostring(root) if params["id"] == pmid else b"<PubmedArticleSet/>"  # noqa: E731
    client = _Client(served)
    raw = plugin.current(DocRef("pubmed", pmid, "pubmed25n0001.xml#0"), client=client, workdir=None)
    assert client.calls[0][0].startswith("https://eutils.ncbi.nlm.nih.gov/") and client.calls[0][1]["db"] == "pubmed"
    assert raw.data == ET.tostring(article) and raw.raw_hash == hashlib.blake2b(raw.data, digest_size=16).hexdigest()
    assert plugin.parse(raw).title
    with pytest.raises(RuntimeError, match="no article"):
        plugin.current(DocRef("pubmed", "999999999", "x#0"), client=_Client(served), workdir=None)


def test_wikipedia_reads_the_export_and_refuses_the_archive_layer():
    from app.plugins.wikipedia import ids
    plugin = collections_mod.PLUGIN_REGISTRY["wikipedia"]
    nid = ids.native_id_of("en", 5)
    export = (b'<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/" version="0.11"><page>'
              b"<title>Thing</title><ns>0</ns><id>5</id><revision><id>1</id>"
              b"<timestamp>2026-01-01T00:00:00Z</timestamp><contributor><username>u</username><id>7</id>"
              b"</contributor><text>'''Thing''' is a thing.</text></revision></page></mediawiki>")
    client = _Client(lambda url, params: export)
    raw = plugin.current(DocRef("wikipedia", str(nid), "enwiki-20260101-pages-articles-multistream.xml.bz2::0::0"),
                         client=client, workdir=None)
    assert client.calls[0][0] == "https://en.wikipedia.org/w/api.php" and client.calls[0][1]["pageids"] == "5"
    assert raw.data.startswith(b"<page>") and b"xmlns" not in raw.data, "the dump's own shape"
    doc = plugin.parse(raw)
    assert doc.title == "Thing" and "is a thing" in " ".join(b for _, b in doc.sections)
    with pytest.raises(NoCurrentForm):
        plugin.current(DocRef("wikipedia", str(ids.native_id_of("en", 5, archive=True)), "x"), client=client,
                       workdir=None)
    with pytest.raises(RuntimeError, match="no page"):
        plugin.current(DocRef("wikipedia", str(nid), "x::0::0"),
                       client=_Client(lambda u, p: b'<mediawiki xmlns="http://x/"/>'), workdir=None)
