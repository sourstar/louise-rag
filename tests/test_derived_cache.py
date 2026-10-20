"""tests/test_derived_cache.py — the fast drive for derived data (07 §7.8;
operator, 2026-10-20): LOUISE_CACHE_DIR names it; app/derived.py places
every plugin's cache under it by the origin's physical identity and carries
an existing cache there once; unset, nothing moves."""
import os
from pathlib import Path

from app import config, derived, parallel
from app.plugins.pubmed import pack as pubmed_pack
from app.plugins.wikipedia import page_props


def test_unset_means_beside_the_origin_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", None)
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", None)
    assert derived.place("pubmed-pack", tmp_path) is None
    container = tmp_path / "corpus" / "pubmed26n0001.xml.gz"
    assert pubmed_pack.pack_path(container) == tmp_path / "corpus" / ".pack" / ("pubmed26n0001.xml.gz" + pubmed_pack.SUFFIX)
    props = tmp_path / "enwiki" / "enwiki-20260801-page_props.sql.gz"
    assert page_props._candidates(props)[0] == props.parent / ".pack" / (props.name + page_props.PACK_SUFFIX)


def test_the_cache_is_keyed_on_the_origins_physical_identity(tmp_path, monkeypatch):
    cache = tmp_path / "nvme" / "cache"
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    a = tmp_path / "corpora" / "a"
    b = tmp_path / "corpora" / "b"
    a.mkdir(parents=True), b.mkdir(parents=True)
    link = tmp_path / "corpora" / "a-by-another-name"
    os.symlink(a, link, target_is_directory=True)
    pa, pb, pl = (derived.place("pubmed-pack", p) for p in (a, b, link))
    assert pa.parent == cache / "pubmed-pack" and pa != pb, "two origins: two places"
    assert pl == pa, "the same origin through a link: the same place"
    assert len(pa.name) == 16 and not pa.exists(), "named by the tag; not created here"
    assert derived.place("wikidata-pack", a) != pa, "per kind"


def test_an_existing_cache_is_carried_in_once_whole_and_left_in_place(tmp_path, monkeypatch):
    src = tmp_path / "beside" / "pack"
    (src / "sub").mkdir(parents=True)
    (src / "a.parquet").write_bytes(b"a" * 10)
    (src / "sub" / "b.parquet").write_bytes(b"b" * 20)
    (src / "00017.parquet.k3j2.tmp").write_bytes(b"half")          # a pass's uncommitted work
    dst = tmp_path / "cache" / "wikidata-pack" / "deadbeef" / "pack"
    assert derived.adopt(src, dst) is True
    assert (dst / "a.parquet").read_bytes() == b"a" * 10 and (dst / "sub" / "b.parquet").read_bytes() == b"b" * 20
    assert not (dst / "00017.parquet.k3j2.tmp").exists(), "*.tmp is not carried"
    assert src.is_dir() and (src / "a.parquet").is_file(), "the original is left for the operator"
    assert (dst / "a.parquet").stat().st_mtime_ns == (src / "a.parquet").stat().st_mtime_ns, "times kept"
    assert derived.adopt(src, dst) is False, "present already: a stat, nothing copied"
    assert derived.adopt(tmp_path / "nowhere", tmp_path / "cache" / "x") is False
    # a copy stopped half-way: only the sibling is left, and the next call redoes it whole
    half = dst.with_name(f".{dst.name}.adopting")
    dst2 = tmp_path / "cache" / "other" / "pack"
    half2 = dst2.with_name(f".{dst2.name}.adopting")
    half2.mkdir(parents=True)
    (half2 / "stale").write_bytes(b"from the stopped copy")
    assert derived.adopt(src, dst2) is True
    assert not half2.exists() and not (dst2 / "stale").exists() and (dst2 / "a.parquet").is_file()
    assert not half.exists()
    # a single file, too
    f = tmp_path / "beside" / "pubmed26n0001.xml.gz.pack"
    f.write_bytes(b"pack")
    assert derived.adopt(f, tmp_path / "cache" / "pubmed-pack" / "t" / f.name) is True
    assert (tmp_path / "cache" / "pubmed-pack" / "t" / f.name).read_bytes() == b"pack"


def test_pubmeds_pack_and_wikipedias_page_props_follow_the_fast_drive(tmp_path, monkeypatch):
    cache = tmp_path / "nvme" / "cache"
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", None)
    corpus = tmp_path / "corpora" / "pubmed"
    (corpus / ".pack").mkdir(parents=True)
    container = corpus / "pubmed26n0001.xml.gz"
    beside = corpus / ".pack" / (container.name + pubmed_pack.SUFFIX)
    beside.write_bytes(b"built beside the containers")
    got = pubmed_pack.pack_path(container)
    assert got == derived.place("pubmed-pack", corpus) / (container.name + pubmed_pack.SUFFIX)
    assert got.read_bytes() == b"built beside the containers", "carried in on the first ask"
    assert beside.is_file()
    # PUBMED_PACK_DIR, when set, still wins
    monkeypatch.setattr(config, "PUBMED_PACK_DIR", tmp_path / "explicit")
    assert pubmed_pack.pack_path(container) == tmp_path / "explicit" / (container.name + pubmed_pack.SUFFIX)
    # wikipedia's page-props pack
    props = tmp_path / "corpora" / "enwiki" / "enwiki-20260801-page_props.sql.gz"
    (props.parent / ".pack").mkdir(parents=True)
    old = props.parent / ".pack" / (props.name + page_props.PACK_SUFFIX)
    old.write_bytes(b"old pack")
    first, second = page_props._candidates(props)
    assert first == derived.place("wikipedia-page-props", props.parent) / (props.name + page_props.PACK_SUFFIX)
    assert first.read_bytes() == b"old pack" and old.is_file()
    assert second.name.startswith(derived.tag(props) + "-")
    # the parse pool's children see the same drive
    assert "CACHE_DIR" in parallel.KNOBS
