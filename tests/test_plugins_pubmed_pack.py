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


def test_pack_is_rebuilt_when_the_container_or_the_format_changes(tmp_path, monkeypatch):
    corpus = _corpus(tmp_path)
    container = corpus / "pubmed25n0001.xml"
    path = pack.ensure(container)
    first = path.stat().st_mtime_ns
    assert pack.ensure(container) == path and path.stat().st_mtime_ns == first, "fresh: reused"

    # the container changes (an updatefile re-landed): stale → rebuilt
    os.utime(container, (container.stat().st_atime, container.stat().st_mtime + 5))
    assert not pack.is_fresh(container, path)
    pack.ensure(container)
    assert path.stat().st_mtime_ns != first

    # the pack format changes: stale → rebuilt
    monkeypatch.setattr(pack, "PACK_VERSION", pack.PACK_VERSION + 1)
    assert not pack.is_fresh(container, path)
    second = path.stat().st_mtime_ns
    pack.ensure(container)
    assert path.stat().st_mtime_ns != second
    meta = pq.read_metadata(path).metadata
    assert meta[b"louise.pack_version"] == str(pack.PACK_VERSION).encode()


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
    assert fields["journal"] == doc.extra["journal"] and fields["date"] == doc.extra["date"]
    assert set(fields) == {"pmid", "title", "abstract", "journal", "date", "authors"}
