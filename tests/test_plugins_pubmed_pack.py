"""The PubMed per-container pack (STATUS 5k): a derived Parquet file that
serves one record in milliseconds instead of a whole-container DOM parse.

The one property everything rests on: the bytes a record yields through
the pack are IDENTICAL to what the DOM path yielded — raw_hash is how
discovery tells a changed record from an unchanged one, so a single
differing byte would re-process all of PubMed on the next scan.
"""
import gzip
import hashlib
import os
import shutil
import threading
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pyarrow.parquet as pq
import pytest

from app import config
from app.plugins.base import DiscoverState, DocRef
from app.plugins.pubmed import SOURCE, pack
from app.plugins.pubmed.source import DELETE_MARKER

FIXTURE = Path(__file__).parent / "fixtures" / "pubmed-mini"


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", None)
    pack.reset_caches()
    yield
    pack.reset_caches()


def _corpus(tmp_path, gz: bool = False) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for src in sorted(FIXTURE.glob("pubmed25n000*.xml")):
        if gz:
            with open(src, "rb") as f, gzip.open(corpus / (src.name + ".gz"), "wb") as g:
                shutil.copyfileobj(f, g)
        else:
            shutil.copy(src, corpus / src.name)
        shutil.copy(src.with_name(src.name + ".md5"), corpus / (src.name + (".gz" if gz else "") + ".md5")) \
            if not gz else None
    return corpus


def _dom_records(container: Path) -> list[bytes]:
    """What the plugin used to return: ET.tostring(root[i]) for every child."""
    with pack.open_container(container) as f:
        root = ET.parse(f).getroot()
    return [ET.tostring(child, encoding="unicode").encode("utf-8") for child in root]


@pytest.mark.parametrize("gz", [False, True])
def test_pack_records_are_byte_identical_to_the_dom_path(tmp_path, gz):
    corpus = _corpus(tmp_path, gz=gz)
    for container in sorted(p for p in corpus.iterdir() if ".xml" in p.name and not p.name.endswith(".md5")):
        expected = _dom_records(container)
        assert expected, container
        path = pack.ensure(container)
        assert path.parent.name == ".pack" and path.name == container.name + pack.SUFFIX
        for i, want in enumerate(expected):
            got = pack.record_xml(container, i)
            assert got == want, f"{container.name}[{i}] differs (tail/whitespace?)"
            assert hashlib.blake2b(got, digest_size=16).hexdigest() == \
                hashlib.blake2b(want, digest_size=16).hexdigest()
        with pytest.raises(IndexError):
            pack.record_xml(container, len(expected))


def test_discovery_and_fetch_go_through_the_pack_and_match_the_old_shape(tmp_path):
    """Same DocRefs (index, kind, tombstones) and the same RawDoc bytes and
    hashes as before — and no container is ever parsed whole again."""
    corpus = _corpus(tmp_path)
    state = DiscoverState(corpus_root=corpus, state_dir=None)
    refs = list(SOURCE.discover(state))
    articles = [r for r in refs if r.extra.get("kind") == "article"]
    deletes = [r for r in refs if r.error == DELETE_MARKER]
    assert len(articles) == 17 and len(deletes) == 2, (len(articles), len(deletes))
    assert all("::" in r.locator for r in refs)

    # fetch: bytes and hash equal the DOM path; served from one row group
    for ref in articles:
        container, index = ref.locator.rsplit("::", 1)
        want = _dom_records(Path(container))[int(index)]
        raw = SOURCE.fetch(ref)
        assert raw.data == want
        assert raw.raw_hash == hashlib.blake2b(want, digest_size=16).hexdigest()
        doc = SOURCE.parse(raw)
        assert doc.uid == f"pubmed:{ref.native_id}"

    # the whole-container DOM parse is gone from the module
    from app.plugins.pubmed import source as source_mod
    assert not hasattr(source_mod, "_parse_container")
    assert not hasattr(source_mod, "_parsed_cache")


def _pack_meta(path):
    return pq.read_metadata(path).metadata or {}


def test_pack_is_rebuilt_when_the_container_or_the_format_changes(tmp_path, monkeypatch):
    """A rebuild is proved by what the pack RECORDS, not by the pack file's
    own mtime. st_mtime_ns is nanoseconds by unit and milliseconds by
    granularity on most filesystems, so a rebuild that takes under a tick
    leaves the timestamp unchanged — this test failed for exactly that
    reason while the rebuild it doubted was demonstrably happening. The
    pack stores the container's size and mtime and its own format version
    (`is_fresh` reads all three), so those are the evidence.
    """
    corpus = _corpus(tmp_path)
    container = corpus / "pubmed25n0001.xml"
    path = pack.ensure(container)
    assert pack.is_fresh(container, path)
    before = _pack_meta(path)
    assert pack.ensure(container) == path, "fresh: reused"
    assert _pack_meta(path) == before, "fresh: not rewritten"

    # the container changes (an updatefile re-landed): stale → rebuilt
    os.utime(container, (container.stat().st_atime, container.stat().st_mtime + 5))
    assert not pack.is_fresh(container, path)
    pack.ensure(container)
    assert pack.is_fresh(container, path), "stale → rebuilt → fresh again"
    assert _pack_meta(path)[b"louise.source_mtime_ns"] == \
        str(container.stat().st_mtime_ns).encode(), \
        "the rebuilt pack records the container it was built from"
    assert _pack_meta(path)[b"louise.source_mtime_ns"] != \
        before[b"louise.source_mtime_ns"]

    # the pack format changes: stale → rebuilt
    monkeypatch.setattr(pack, "PACK_VERSION", pack.PACK_VERSION + 1)
    assert not pack.is_fresh(container, path)
    pack.ensure(container)
    assert pack.is_fresh(container, path)
    assert _pack_meta(path)[b"louise.pack_version"] == \
        str(pack.PACK_VERSION).encode()


def test_a_corrupt_container_leaves_no_pack_and_costs_one_error_ref(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "bad.xml.gz").write_bytes(b"not gzip")
    shutil.copy(FIXTURE / "pubmed25n0002.xml", corpus / "pubmed25n0002.xml")
    refs = list(SOURCE.discover(DiscoverState(corpus_root=corpus, state_dir=None)))
    errors = [r for r in refs if r.error and r.scope == "container"]
    assert len(errors) == 1 and "bad.xml.gz" in errors[0].native_id
    assert not pack.pack_path(corpus / "bad.xml.gz").exists()
    assert not pack.pack_path(corpus / "bad.xml.gz").with_name("bad.xml.gz" + pack.SUFFIX + ".tmp").exists()
    assert [r for r in refs if not r.error], "the good container still enumerates"


def test_pack_dir_can_be_redirected_and_is_never_corpus_material(tmp_path, monkeypatch):
    corpus = _corpus(tmp_path)
    elsewhere = tmp_path / "ssd-cache"
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", elsewhere)
    container = corpus / "pubmed25n0001.xml"
    path = pack.ensure(container)
    assert path.parent == elsewhere and path.is_file()
    # default placement is a HIDDEN directory beside the archive, which
    # discovery (files only) skips and torrent publishing never ships
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", None)
    pack.reset_caches()
    pack.ensure(container)
    assert (corpus / ".pack").is_dir()
    names = {r.native_id for r in SOURCE.discover(DiscoverState(corpus_root=corpus, state_dir=None))}
    assert not any(".pack" in n or n.endswith(".parquet") for n in names)
    from app.acquire import torrent_publish
    assert torrent_publish._hidden(".pack/pubmed25n0001.xml.pack.parquet")
    assert not torrent_publish._hidden("pubmed25n0001.xml")
    before = torrent_publish.corpus_digest(corpus)
    (corpus / ".pack" / "extra.tmp").write_bytes(b"x")
    assert torrent_publish.corpus_digest(corpus) == before, "a cache never changes the snapshot identity"


def test_concurrent_first_access_builds_the_pack_once(tmp_path, monkeypatch):
    corpus = _corpus(tmp_path)
    container = corpus / "pubmed25n0001.xml"
    builds = []
    real_build = pack.build

    def counting_build(c, dest):
        builds.append(c.name)
        return real_build(c, dest)
    monkeypatch.setattr(pack, "build", counting_build)
    threads = [threading.Thread(target=lambda: pack.record_xml(container, 0)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert builds == [container.name], builds


def test_reader_fields_come_from_the_pack(tmp_path):
    corpus = _corpus(tmp_path)
    container = corpus / "pubmed25n0001.xml"
    refs = [r for r in SOURCE.discover(DiscoverState(corpus_root=corpus, state_dir=None))
            if r.extra.get("kind") == "article" and r.locator.startswith(str(container))]
    ref = refs[0]
    fields = pack.record_fields(container, ref.extra["record_index"])
    doc = SOURCE.parse(SOURCE.fetch(ref))
    assert fields["pmid"] == ref.native_id and fields["title"] == doc.title
    assert fields["journal"] == doc.extra["journal"]
    # THE PACK KEEPS ITS OWN DATE SHAPE. `date` here is the pack's planner
    # column — internal, never published — so it stays "2023-Jan" while the
    # RECORD carries §9.5's "2023-01". Normalising the column would bump
    # PACK_VERSION and rebuild all 1,334 containers' packs for no gain.
    assert fields["date"] == "2023-Jan" and doc.date == "2023-01"
    assert set(fields) == {"pmid", "title", "abstract", "journal", "date", "authors"}


def test_no_parquet_reader_is_shared_between_threads(tmp_path, monkeypatch):
    """A ParquetFile is not safe for concurrent use (pre_buffer, pyarrow
    25's default, swaps the reader's ReadRangeCache under a read in
    progress). One cached per pack was shared by pubmed's fts stage and its
    four embed workers until 2026-09-18: a ReadRangeCache error recorded on
    a healthy document, a double free, two segfaults in libarrow. Every read
    must go through a reader no other thread holds — and still return the
    right record."""
    monkeypatch.setattr(pack, "ROW_GROUP", 2)          # 11 records -> 6 groups
    corpus = _corpus(tmp_path)
    container = corpus / "pubmed25n0001.xml"
    expected = _dom_records(container)
    pack.record_fields(container, 0)                   # a cache, if any, is warm
    real = pq.ParquetFile
    guard = threading.Lock()
    active: dict[int, int] = {}
    shared = []

    class Watched(real):
        def read_row_group(self, *a, **k):
            with guard:
                active[id(self)] = active.get(id(self), 0) + 1
                if active[id(self)] > 1:
                    shared.append(id(self))
            try:
                time.sleep(0.01)                       # widen the window
                return super().read_row_group(*a, **k)
            finally:
                with guard:
                    active[id(self)] -= 1

    monkeypatch.setattr(pack.pq, "ParquetFile", Watched)
    articles = [i for i, tag, _pmid, _dels in pack.index_columns(container)
                if tag == "PubmedArticle"]
    start = threading.Barrier(6)
    wrong, failed = [], []

    def reader(k: int) -> None:
        start.wait()
        try:
            for i in range(len(expected)):
                j = (i + k) % len(expected)
                if pack.record_xml(container, j) != expected[j]:
                    wrong.append(j)
                if j in articles:
                    pack.record_fields(container, j)
        except Exception as e:                         # noqa: BLE001
            failed.append(repr(e))

    threads = [threading.Thread(target=reader, args=(k,)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not failed and not wrong, (failed, wrong)
    assert not shared, f"{len(shared)} read(s) entered a reader another thread was using"
