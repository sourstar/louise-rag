"""Wikipedia's two sources in ONE collection (docs/notes/wikipedia-two-layers.md):
layer 1 reads the pre-embedded archive's own text and cut, so its vectors
attach one to one; layer 2 reads the dump; each page is read from one of
them, never both."""
import bz2
import os
from xml.sax.saxutils import escape

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import chunking, collections as collections_mod, db, discovery, plugin_io
from app.plugins.base import DiscoverState, DocRef
from app.plugins.embedded.source import EmbeddedSource
from app.plugins.wikipedia import SOURCE, archive
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.ids import native_id_of

DUMP = "enwiki-latest-pages-articles-multistream"


def _rows(*specs):
    """(article, seq, title, text) → an Upstash-shaped table."""
    return pa.table({
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://en.wikipedia.org/wiki/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [np.ones(4, dtype=np.float32).tolist() for _ in specs],
    })


def _page(pid: int, title: str, text: str) -> str:
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id><revision>"
            f"<id>{pid}00</id><timestamp>2026-07-31T12:00:00Z</timestamp>"
            f"<text>{escape(text)}</text></revision></page>\n")


@pytest.fixture
def corpus(tmp_path):
    wsrc._stream_cache.clear()
    archive._groups.clear()
    archive._layouts.clear()
    root = tmp_path / "wikipedia"
    (root / "data" / "en").mkdir(parents=True)
    pq.write_table(_rows(("10", 0, "Moon", "para one"), ("10", 1, "Moon", "para two"),
                         ("11", 0, "Sun", "straddle start")),
                   root / "data" / "en" / "000.parquet")
    pq.write_table(_rows(("11", 1, "Sun", "straddle end")),
                   root / "data" / "en" / "001.parquet")
    d = root / "enwiki" / "latest"
    d.mkdir(parents=True)
    stream = bz2.compress((_page(10, "Moon", "'''The Moon''' is newer text.")
                           + _page(11, "Sun", "The Sun.")
                           + _page(20, "Cheese", "'''Cheese''' is made from milk.")).encode())
    (d / f"{DUMP}.xml.bz2").write_bytes(stream)
    (d / f"{DUMP}-index.txt.bz2").write_bytes(
        bz2.compress(b"0:10:Moon\n0:11:Sun\n0:20:Cheese\n"))
    # the run's page_props, which both layers' records read (parser 5)
    from tests import wikipedia_props
    wikipedia_props.write(d / f"{DUMP}.xml.bz2", [(10, "wikibase_item", "Q405"),
                                                  (11, "wikibase_item", "Q525"),
                                                  (11, "page_image_free", "The_Sun.jpg")])
    return root


def _refs(root, state_dir=None):
    return {r.native_id: r for r in SOURCE.discover(DiscoverState(corpus_root=root,
                                                                   state_dir=state_dir))
            if r.scope == "document"}


def _doc(ref):
    return plugin_io.canonical_doc(SOURCE, {"native_id": ref.native_id, "path": ref.locator})


def test_each_page_is_read_from_one_source_only(corpus, tmp_path):
    refs = _refs(corpus, tmp_path / "state")
    assert set(refs) == {str(native_id_of("en", 10, archive=True)),
                         str(native_id_of("en", 11, archive=True)),
                         str(native_id_of("en", 20))}, \
        "10 and 11 from the archive, 20 from the dump — and no dump twin of 10 or 11"


def test_an_archive_article_is_wikivecs_own_text_and_cut(corpus):
    """The same sections the legacy plugin makes from the same rows, so
    `verbatim` cuts exactly the chunks the archive's vectors belong to."""
    nid = str(native_id_of("en", 10, archive=True))
    ref = _refs(corpus)[nid]
    doc = _doc(ref)
    legacy = EmbeddedSource()
    old = legacy.parse(legacy.fetch(DocRef(source_id="embedded", native_id=nid,
                                           locator=ref.locator)))
    assert doc.sections == old.sections == [("", "Moon\npara one"), ("", "Moon\npara two")]
    assert doc.uid == f"wikipedia:{nid}" and doc.language == "en"
    assert (doc.extra["source"], doc.extra["page_id"]) == ("archive", "10")
    assert doc.extra["url"] == "https://en.wikipedia.org/wiki/Moon"
    chunks = list(chunking.chunk_verbatim(doc.uid, int(nid), doc.sections, 1))
    assert [c["text"] for c in chunks] == ["Moon\npara one", "Moon\npara two"]


def test_a_page_the_archive_lacks_is_read_from_the_dump(corpus):
    doc = _doc(_refs(corpus)[str(native_id_of("en", 20))])
    assert doc.title == "Cheese" and doc.extra["source"] == "dump"
    assert doc.date == "2026-07-31"


def test_a_straddlers_origin_names_both_shards_and_reads_back(corpus):
    """06 §6.4 (main 8068727): an archive record is located IN the archive,
    by URL at the registry's pinned revision — never `main`, never a path
    under this node's copy; a dump page stays relative to the dumps' base."""
    from app.bundles.genesis import UPSTASH_WIKIPEDIA as up
    arch = f"{up.base_url}/resolve/{up.revision}/"
    assert up.revision and "main" not in arch
    ref = _refs(corpus)[str(native_id_of("en", 11, archive=True))]
    origin = plugin_io.origin_locator(SOURCE, ref.locator, corpus)
    # and, since parser 5, the page's rows of the dump run's page_props,
    # relative to the dumps' base like any dump file
    rows, props = origin.rsplit("|", 1)
    assert rows == f"{arch}data/en/000.parquet#2::3|{arch}data/en/001.parquet#0::1"
    assert props.startswith("enwiki/latest/enwiki-latest-page_props.sql.gz#")
    local, locator = plugin_io.local_locator(SOURCE, origin, corpus)
    assert local.is_file() and locator == ref.locator
    assert _doc(ref).sections == [("", "Sun\nstraddle start"), ("", "Sun\nstraddle end")]
    assert _doc(ref).extra["wikidata"] == "Q525"
    assert _doc(ref).extra["page_props"] == {"page_image_free": "The_Sun.jpg"}

    dump_ref = _refs(corpus)[str(native_id_of("en", 20))]
    assert plugin_io.origin_locator(SOURCE, dump_ref.locator, corpus) == \
        f"enwiki/latest/{DUMP}.xml.bz2#0::0"


def test_coverage_holds_when_the_cursor_skips_the_archive(corpus, tmp_path, isolated_config):
    """A re-scan in which only the DUMP changed skips the archive's shards
    (the cursor) — and must still not mint dump twins of its articles."""
    collection = collections_mod.Collection(
        name="wikipedia", plugin="wikipedia", profile=None, embed_mode_default=None,
        corpus_dir=corpus, manifest_path=tmp_path / "m" / "manifest.db",
        fts_dir=tmp_path / "m" / "fts", lance_dir=tmp_path / "m" / "lancedb")
    con = db.connect(collection)
    discovery.sync_collection(con, collection)
    dump = corpus / "enwiki" / "latest" / f"{DUMP}.xml.bz2"
    st = dump.stat()
    os.utime(dump, ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000))
    discovery.sync_collection(con, collection)
    ids = {r[0] for r in con.execute("SELECT native_id FROM documents")}
    assert ids == {str(native_id_of("en", 10, archive=True)),
                   str(native_id_of("en", 11, archive=True)),
                   str(native_id_of("en", 20))}


def test_coverage_is_cached_per_shard(corpus, tmp_path, monkeypatch):
    state = tmp_path / "state"
    first = archive.covered_articles(corpus, state)
    calls = []
    real = pq.read_table
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    again = archive.covered_articles(corpus, state)
    assert calls == [], "an unchanged shard is not read again"
    assert list(first["en"]) == list(again["en"]) == [10, 11]


def test_an_archive_reached_through_a_link_is_walked_and_no_page_is_minted_twice(corpus, tmp_path):
    """wikipedia-v8 (2026-10-30): its origin reaches the archive through a
    link (`origin/data` -> /corpora/...), and rglob enters no symlinked
    directory (Python 3.11-3.13). The archive was never walked, its coverage
    read empty, and the dump pass minted 21.9 M pages a second time. Walked
    through the link, each page is read from one source, and the archive's
    locators stay under the origin they name."""
    real = tmp_path / "corpora" / "data"
    real.parent.mkdir()
    (corpus / "data").rename(real)
    (corpus / "data").symlink_to(real, target_is_directory=True)
    archive._groups.clear()
    archive._layouts.clear()
    (tmp_path / "state-linked").mkdir()
    refs = _refs(corpus, tmp_path / "state-linked")
    assert set(refs) == {str(native_id_of("en", 10, archive=True)),
                         str(native_id_of("en", 11, archive=True)),
                         str(native_id_of("en", 20))}, "no dump twin of 10 or 11"
    moon = refs[str(native_id_of("en", 10, archive=True))]
    assert moon.locator.startswith(str(corpus / "data")), "the path as reached, not resolved"
