"""Wikipedia parser 5: each record holds its page's rows of the dump run's
`page_props` — the Wikidata item in `extra.wikidata`, every other property
in `extra.page_props` — taken from the source, beside the page, and covered
by `origin_hash` (docs/notes/wikipedia-qids.md)."""
import bz2
import gzip
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import plugin_io
from app.hold import records as hold_records
from app.plugins.base import DiscoverState
from app.plugins.wikipedia import SOURCE, archive, page_props
from app.plugins.wikipedia import source as wsrc
from app.plugins.wikipedia.ids import native_id_of
from tests import wikipedia_props

DUMP = "enwiki-20260801-pages-articles-multistream"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    wsrc._stream_cache.clear()
    archive._groups.clear()
    archive._layouts.clear()
    page_props._fresh.clear()
    page_props._bounds.clear()
    page_props._groups.clear()
    # never the machine's own temporary directory
    monkeypatch.setattr(page_props.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))


def _page(pid: int, title: str, text: str) -> str:
    return (f"<page><title>{title}</title><ns>0</ns><id>{pid}</id><revision>"
            f"<id>{pid}00</id><timestamp>2026-07-31T12:00:00Z</timestamp>"
            f"<text>{escape(text)}</text></revision></page>\n")


ROWS = [(10, "wikibase_item", "Q405"), (10, "page_image_free", "FullMoon2010.jpg"),
        (10, "wikibase-badge-Q17437796", "1", "1"),
        (20, "disambiguation", ""), (20, "wikibase_item", "Q10943"),
        (21, "displaytitle", "<i>Bread</i> it's \\ \"so\"\nnew")]


@pytest.fixture
def corpus(tmp_path):
    """One pinned run: the archive covers page 10 (layer 1), the dump holds
    20, 21 and 22 (layer 2; 22 has no page_props rows)."""
    root = tmp_path / "wikipedia"
    (root / "data" / "en").mkdir(parents=True)
    pq.write_table(pa.table({
        "id": ["10_0", "10_1"], "url": ["https://en.wikipedia.org/wiki/Moon"] * 2,
        "title": ["Moon"] * 2, "text": ["para one", "para two"],
        "embedding": [np.ones(4, dtype=np.float32).tolist()] * 2}),
        root / "data" / "en" / "000.parquet")
    d = root / "enwiki" / "20260801"
    d.mkdir(parents=True)
    pages = [(10, "Moon", "The Moon."), (20, "Cheese", "'''Cheese''' is food."),
             (21, "Bread", "'''Bread''' is food."), (22, "Wine", "'''Wine''' is a drink.")]
    (d / f"{DUMP}.xml.bz2").write_bytes(bz2.compress("".join(_page(*p) for p in pages).encode()))
    (d / f"{DUMP}-index.txt.bz2").write_bytes(
        bz2.compress("".join(f"0:{p}:{t}\n" for p, t, _ in pages).encode()))
    # two rows per statement: page 10's three rows straddle a statement break
    wikipedia_props.write(d / f"{DUMP}.xml.bz2", ROWS, per_statement=2)
    return root


def _refs(root, state_dir=None):
    return {int(r.native_id): r for r in SOURCE.discover(
        DiscoverState(corpus_root=root, state_dir=state_dir, snapshot="20260801"))
        if r.scope == "document"}


def _doc(ref):
    return plugin_io.canonical_doc(SOURCE, {"native_id": ref.native_id, "path": ref.locator})


MOON, CHEESE, BREAD, WINE = (native_id_of("en", 10, archive=True), native_id_of("en", 20),
                             native_id_of("en", 21), native_id_of("en", 22))


# ---- the table ----------------------------------------------------------------

def test_a_unit_is_the_pages_own_bytes_of_the_decompressed_table(corpus):
    props = corpus / "enwiki" / "20260801" / "enwiki-20260801-page_props.sql.gz"
    raw = gzip.decompress(props.read_bytes())
    units = list(page_props.iter_units(iter(raw.splitlines(keepends=True))))
    assert [u[0] for u in units] == [10, 20, 21]
    for _page_id, start, end, unit in units:
        assert raw[start:end] == unit
    assert b"INSERT INTO" in units[0][3], "page 10's rows straddle a statement break"
    assert page_props.fields_of(units[0][3]) == {
        "wikidata": "Q405", "page_props": {"page_image_free": "FullMoon2010.jpg",
                                           "wikibase-badge-Q17437796": "1"}}
    assert page_props.fields_of(units[2][3]) == {
        "page_props": {"displaytitle": "<i>Bread</i> it's \\ \"so\"\nnew"}}, "escapes read back"
    assert page_props.fields_of(units[1][3])["page_props"] == {"disambiguation": ""}, \
        "an empty value is a property the page has"


def test_bytes_that_are_not_utf8_are_kept_as_base64():
    unit = b"(5,'templatedata','\\0\\Z\xff\xfe',NULL)"
    assert page_props.fields_of(unit) == {"page_props": {"templatedata": {"base64": "ABr//g=="}}}


@pytest.mark.parametrize("line", [
    b"INSERT INTO `page_props` VALUES (1,'a','b',NULL),(2,'a',b,NULL);\n",     # unquoted value
    b"INSERT INTO `page_props` VALUES (1,'a','b',NULL),(2,'a','b',0x1F);\n",  # an unknown sortkey
    b"INSERT INTO `page_props` VALUES (1,'a','b',NULL)\n",                      # no `;`
    b"INSERT INTO `page_props` VALUES (2,'a','b',NULL),(1,'a','b',NULL);\n",  # out of order
])
def test_a_row_the_reader_does_not_know_fails_the_file(line):
    """A skipped row would be a property the record silently lacks."""
    with pytest.raises(page_props.PagePropsError):
        list(page_props.iter_units(iter([line])))


def test_a_sortkey_in_any_float_form_reads():
    line = b"INSERT INTO `page_props` VALUES (1,'a','1',1),(1,'b','x',-2.5e-05),(2,'c','y',3.0E+20);\n"
    assert [u[0] for u in page_props.iter_units(iter([line]))] == [1, 2]


def test_the_pack_is_built_once_and_again_when_the_table_moves(corpus):
    props = corpus / "enwiki" / "20260801" / "enwiki-20260801-page_props.sql.gz"
    pack = page_props.ensure(props)
    assert pack.parent == props.parent / ".pack"
    first = pack.stat().st_mtime_ns
    page_props._fresh.clear()
    assert page_props.ensure(props) == pack and pack.stat().st_mtime_ns == first, "fresh: reused"
    wikipedia_props.write(corpus / "enwiki" / "20260801" / f"{DUMP}.xml.bz2",
                          [(10, "wikibase_item", "Q999")])
    st = props.stat()
    os.utime(props, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    assert page_props.unit_of(props, 10)[2] == b"(10,'wikibase_item','Q999',NULL)"
    assert page_props.unit_of(props, 20) is None


def test_an_origin_that_cannot_be_written_packs_in_the_temporary_directory(corpus, monkeypatch, tmp_path):
    """A check reads the origin from a read-only mount (03 §3.2)."""
    props = corpus / "enwiki" / "20260801" / "enwiki-20260801-page_props.sql.gz"
    blocked = tmp_path / "blocked"
    blocked.write_text("a file, so nothing can be made under it")
    monkeypatch.setattr(page_props, "_pack_dirs",
                        lambda p: [blocked / ".pack", tmp_path / "tmp" / "louise-page-props"])
    assert page_props.unit_of(props, 20) is not None
    assert page_props.ensure(props).parent == tmp_path / "tmp" / "louise-page-props"


# ---- the records ----------------------------------------------------------------

def test_both_layers_hold_the_page_props_of_their_page(corpus):
    refs = _refs(corpus)
    assert set(refs) == {MOON, CHEESE, BREAD, WINE}
    moon, cheese, wine = _doc(refs[MOON]), _doc(refs[CHEESE]), _doc(refs[WINE])
    assert (moon.extra["source"], moon.extra["wikidata"]) == ("archive", "Q405")
    assert moon.extra["page_props"]["page_image_free"] == "FullMoon2010.jpg"
    assert (cheese.extra["source"], cheese.extra["wikidata"]) == ("dump", "Q10943")
    assert cheese.extra["page_props"] == {"disambiguation": ""}
    assert "wikidata" not in wine.extra and "page_props" not in wine.extra, \
        "a page with no rows holds none"


def test_origin_hash_covers_the_page_and_its_rows(corpus):
    refs = _refs(corpus)
    wine, cheese = SOURCE.fetch(refs[WINE]), SOURCE.fetch(refs[CHEESE])
    own, unit = wsrc._split_raw(cheese.data)
    assert unit is not None and b"Q10943" in unit
    assert cheese.raw_hash == hashlib.blake2b(cheese.data, digest_size=16).hexdigest()
    assert cheese.raw_hash != hashlib.blake2b(own, digest_size=16).hexdigest()
    assert wsrc._split_raw(wine.data)[1] is None
    assert wine.raw_hash == hashlib.blake2b(wine.data, digest_size=16).hexdigest(), \
        "a page with no rows: its own bytes alone, parser 4's hash"


def test_title_and_sections_do_not_move_so_neither_does_a_chunk(corpus):
    """02 §2.4 A1d: the parser gained `extra` fields and nothing a chunker reads."""
    for nid in (MOON, CHEESE):
        raw = SOURCE.fetch(_refs(corpus)[nid])
        own, _unit = wsrc._split_raw(raw.data)
        with_rows = SOURCE.parse(raw)
        without = SOURCE.parse(SimpleNamespace(ref=raw.ref, data=own, raw_hash="x"))
        assert (with_rows.title, with_rows.sections) == (without.title, without.sections)
        assert hold_records.content_hash(with_rows.title, with_rows.sections) == \
            hold_records.content_hash(without.title, without.sections)
        assert with_rows.extra["wikidata"] and "wikidata" not in without.extra


def test_a_new_table_changes_every_signature_through_the_cursor(corpus, tmp_path):
    """tests/origin_coverage.py's rule, with the cursor that skips unchanged
    files in production: a record holds its rows, so a new page_props must
    rebuild it — the archive's articles and the dump's pages alike."""
    state = tmp_path / "state"
    state.mkdir()
    before = _refs_with_cursor(corpus, state)
    assert set(before) == {MOON, CHEESE, BREAD, WINE}
    assert _refs_with_cursor(corpus, state) == {}, "nothing changed: the cursor skips every file"
    props = corpus / "enwiki" / "20260801" / "enwiki-20260801-page_props.sql.gz"
    st = props.stat()
    os.utime(props, (st.st_atime, st.st_mtime + 1000))
    after = _refs_with_cursor(corpus, state)
    assert set(after) == set(before), "every record of the language is rediscovered"
    assert all(after[k] != before[k] for k in before)


def _refs_with_cursor(root, state):
    """One discovery with the cursor, committed as the orchestrator commits."""
    st = DiscoverState(corpus_root=root, state_dir=state, snapshot="20260801")
    out = {}
    for r in SOURCE.discover(st):
        st.received += 1
        if r.scope == "document":
            out[int(r.native_id)] = (r.mtime, r.size)
    st.committed = st.received
    for hook in st.on_commit or ():
        hook()
    return out


def test_a_run_whose_table_is_not_on_disk_waits(corpus):
    """Deferral, never an error on a document: the dump's pages and the
    archive's articles of that language wait for their run's page_props."""
    (corpus / "enwiki" / "20260801" / "enwiki-20260801-page_props.sql.gz").unlink()
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus, snapshot="20260801")))
    assert all(r.scope == "container" for r in refs)
    assert any("page_props" in r.error for r in refs)


def test_a_locator_naming_rows_the_table_no_longer_holds_is_refused(corpus):
    ref = _refs(corpus)[CHEESE]
    body, (props, start, end) = wsrc.props_segment(ref.locator)
    stale = SimpleNamespace(**{**ref.__dict__, "locator": f"{body}|{props}::{start + 1}::{end}"})
    with pytest.raises(RuntimeError, match="discovery has not seen this table"):
        SOURCE.fetch(stale)


def test_the_origin_names_both_files_and_reads_back(corpus):
    """09 §9.5: `origin` locates every byte `origin_hash` covers — the page's
    stream and its rows, each relative to the dumps' base."""
    ref = _refs(corpus)[CHEESE]
    origin = plugin_io.origin_locator(SOURCE, ref.locator, corpus)
    page, props = origin.split("|")
    assert page == f"enwiki/20260801/{DUMP}.xml.bz2#0::0"
    assert props.startswith("enwiki/20260801/enwiki-20260801-page_props.sql.gz#")
    local, locator = plugin_io.local_locator(SOURCE, origin, corpus)
    assert locator == ref.locator
    unit, size = plugin_io.origin_unit(SOURCE, origin, corpus)
    assert unit == (f"enwiki/20260801/{DUMP}.xml.bz2+"
                    f"enwiki/20260801/enwiki-20260801-page_props.sql.gz")
    assert size == sum((corpus / p).stat().st_size for p in unit.split("+"))


def test_an_origin_pair_is_weighed_once_however_many_records_name_it(corpus, monkeypatch):
    """The cost test of tests/hold/test_check.py, for locators of two pieces:
    the members must not make every record a new key."""
    from app import louise
    weighed = []
    real = plugin_io.origin_unit
    monkeypatch.setattr(plugin_io, "origin_unit",
                        lambda p, o, b: (weighed.append(o), real(p, o, b))[1])
    unit_of = louise._origin_unit_from(SimpleNamespace(plugin="wikipedia"), "wikipedia", corpus)
    answers = {unit_of(f"enwiki/20260801/{DUMP}.xml.bz2#0::{i}|"
                       f"enwiki/20260801/enwiki-20260801-page_props.sql.gz#{i}::{i + 9}")
               for i in range(200)}
    assert len(weighed) == 1 and len(answers) == 1


def test_genesis_still_reads_an_archive_article_as_one_piece(corpus):
    """bundles/genesis.py's straddler test counts archive segments: the
    page_props segment is never one."""
    ref = _refs(corpus)[MOON]
    assert "|" in ref.locator
    assert archive.split_locator(ref.locator) == [
        (str(corpus / "data" / "en" / "000.parquet"), 0, 2)]
