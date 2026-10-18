"""A pinned wikipedia collection reads its own dated dump and nothing else.

Dated dumps coexist in one corpus by design (acquire/specs.py
`_wiki_files`), and the node keeps the legacy `latest/` beside the pinned
`20260801/`. Two dumps of one wiki mint the same ids, so a scan that read
both would flip every page between them. The pin reached acquisition only;
discovery now reads it too, and an unpinned corpus refuses the second dump.
"""
import bz2
from xml.sax.saxutils import escape

import pytest

from app import collections as collections_mod, db, discovery
from app.plugins.base import DiscoverState
from app.plugins.wikipedia import SOURCE, archive
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.ids import native_id_of


def _page(pid: int, title: str, text: str) -> str:
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id><revision>"
            f"<id>{pid}00</id><timestamp>2026-07-31T12:00:00Z</timestamp>"
            f"<text>{escape(text)}</text></revision></page>\n")


def _dump(root, wiki: str, snap: str, pages) -> None:
    d = root / wiki / snap
    d.mkdir(parents=True)
    base = f"{wiki}-{snap}-pages-articles-multistream"
    (d / f"{base}.xml.bz2").write_bytes(
        bz2.compress("".join(_page(*p) for p in pages).encode()))
    (d / f"{base}-index.txt.bz2").write_bytes(
        bz2.compress("".join(f"0:{p[0]}:{p[1]}\n" for p in pages).encode()))
    from tests import wikipedia_props
    wikipedia_props.write(d / f"{base}.xml.bz2")


@pytest.fixture
def corpus(tmp_path):
    wsrc._stream_cache.clear()
    archive._groups.clear()
    archive._layouts.clear()
    root = tmp_path / "wikipedia"
    _dump(root, "enwiki", "latest", [(20, "Cheese", "old text"), (21, "Bread", "old text")])
    _dump(root, "enwiki", "20260801", [(20, "Cheese", "new text"), (22, "Wine", "new text")])
    return root


def _refs(root, snapshot=None):
    return list(SOURCE.discover(DiscoverState(corpus_root=root, snapshot=snapshot)))


def test_a_pinned_collection_reads_only_its_dated_dump(corpus):
    refs = _refs(corpus, "20260801")
    assert not [r for r in refs if r.error]
    docs = [r for r in refs if r.scope == "document"]
    assert sorted(int(r.native_id) for r in docs) == [native_id_of("en", 20),
                                                      native_id_of("en", 22)]
    assert all("20260801" in r.locator for r in docs)


def test_an_unpinned_corpus_refuses_a_second_dump_of_one_wiki(corpus):
    refs = _refs(corpus)
    docs = [r for r in refs if r.scope == "document"]
    assert len({r.native_id for r in docs}) == len(docs), "no id minted twice"
    assert [r.native_id for r in refs if r.error] == \
        ["enwiki-latest-pages-articles-multistream.xml.bz2"]


def test_discovery_hands_the_collections_pin_to_the_plugin(corpus, tmp_path, isolated_config):
    c = collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb",
        snapshot="20260801")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    paths = [r[0] for r in con.execute("SELECT path FROM documents")]
    assert len(paths) == 2 and all("20260801" in p for p in paths)
