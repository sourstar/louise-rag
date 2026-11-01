"""A wikipedia document is an ARTICLE, by its own wiki's namespace names.

`pages-articles` dumps carry categories, templates, files, portals and
modules beside the articles, and only a title's prefix says which — in the
wiki's language. Discovery skipped them by an English list, so on ten of
the eleven wikis `Kategorie:`, `Catégorie:`, `Категория:`, `رده:`… entered
as articles: 108 of 440 sampled dump-layer pages on the node (2026-09-15),
about 9 M documents. Each dump's <siteinfo> header names its namespaces.
"""
import bz2
from xml.sax.saxutils import escape

import pytest


from app.plugins.base import DiscoverState
from app.plugins.wikipedia import SOURCE, archive
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.ids import native_id_of

SITEINFO = ('<mediawiki xml:lang="de"><siteinfo><dbname>dewiki</dbname><namespaces>'
            '<namespace key="-2" case="first-letter">Medium</namespace>'
            '<namespace key="0" case="first-letter" />'
            '<namespace key="6" case="first-letter">Datei</namespace>'
            '<namespace key="10" case="first-letter">Vorlage</namespace>'
            '<namespace key="14" case="first-letter">Kategorie</namespace>'
            '</namespaces></siteinfo>\n')
PAGES = [(1, 0, "Nordstadt"), (2, 14, "Kategorie:Städte in Hessen"),
         (3, 10, "Vorlage:Infobox Stadt"), (4, 6, "Datei:Nordstadt.jpg"),
         (5, 0, "Star Trek: Raumschiff Enterprise")]


def _page(pid, ns, title):
    return (f"<page><title>{escape(title)}</title><ns>{ns}</ns><id>{pid}</id><revision>"
            f"<id>{pid}00</id><timestamp>2026-07-31T12:00:00Z</timestamp>"
            f"<text>text of {pid}</text></revision></page>\n")


def _dump(root, header: str | None):
    d = root / "dewiki" / "20260801"
    d.mkdir(parents=True)
    base = "dewiki-20260801-pages-articles-multistream"
    head = bz2.compress(header.encode()) if header else b""
    body = bz2.compress("".join(_page(*p) for p in PAGES).encode())
    (d / f"{base}.xml.bz2").write_bytes(head + body)
    (d / f"{base}-index.txt.bz2").write_bytes(bz2.compress(
        "".join(f"{len(head)}:{pid}:{title}\n" for pid, _ns, title in PAGES).encode()))
    from tests import wikipedia_props
    wikipedia_props.write(d / f"{base}.xml.bz2")
    return d / f"{base}.xml.bz2", d / f"{base}-index.txt.bz2"


@pytest.fixture(autouse=True)
def _fresh():
    wsrc._stream_cache.clear()
    archive._groups.clear()
    archive._layouts.clear()


def _articles(root):
    return sorted(int(r.native_id) for r in SOURCE.discover(
        DiscoverState(corpus_root=root, snapshot="20260801")) if r.scope == "document")


def test_the_wikis_own_namespace_names_decide_what_is_an_article(tmp_path):
    root = tmp_path / "wikipedia"
    dump, index = _dump(root, SITEINFO)
    assert wsrc.namespace_prefixes(dump, index) == ("Medium:", "Datei:", "Vorlage:", "Kategorie:")
    assert _articles(root) == [native_id_of("de", 1), native_id_of("de", 5)], \
        "a category, a template and a file are not articles; a colon in a title is"


def test_eswikis_anexo_is_articles(tmp_path):
    """Operator, 2026-09-15: eswiki's namespace 104 holds encyclopedic
    lists — enwiki's namespace-0 "List of…" articles — and stays."""
    d = tmp_path / "wikipedia" / "eswiki" / "20260801"
    d.mkdir(parents=True)
    base = "eswiki-20260801-pages-articles-multistream"
    head = bz2.compress(('<mediawiki xml:lang="es"><siteinfo><namespaces>'
                         '<namespace key="0" case="first-letter" />'
                         '<namespace key="10" case="first-letter">Plantilla</namespace>'
                         '<namespace key="104" case="first-letter">Anexo</namespace>'
                         '</namespaces></siteinfo>\n').encode())
    pages = [(1, 0, "Madrid"), (2, 104, "Anexo:Países de Europa"), (3, 10, "Plantilla:Ficha")]
    (d / f"{base}.xml.bz2").write_bytes(head + bz2.compress("".join(_page(*p) for p in pages).encode()))
    index = d / f"{base}-index.txt.bz2"
    index.write_bytes(bz2.compress("".join(f"{len(head)}:{p}:{t}\n" for p, _n, t in pages).encode()))
    from tests import wikipedia_props
    wikipedia_props.write(d / f"{base}.xml.bz2")
    assert wsrc.namespace_prefixes(d / f"{base}.xml.bz2", index) == ("Plantilla:",)
    assert _articles(tmp_path / "wikipedia") == [native_id_of("es", 1), native_id_of("es", 2)]


def test_a_dump_without_a_namespace_table_keeps_the_english_list(tmp_path):
    root = tmp_path / "wikipedia"
    dump, index = _dump(root, None)
    assert wsrc.namespace_prefixes(dump, index) is None
    # the fallback cannot know dewiki's names — which is why it is only that
    assert native_id_of("de", 2) in _articles(root)


def _held(con):
    return sorted(int(r[0]) for r in con.execute("SELECT native_id FROM documents"))
