"""EmbeddedSource — pre-embedded parquet corpora (amendment 11, format v1):
per-article grouping of consecutive chunk rows, shard-boundary straddling,
scan cursor, corrupt-shard tolerance, title-prefixed sections, and the
verbatim chunking profile that preserves the producer's boundaries 1:1.
"""
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app import chunking
from app.plugins.base import DiscoverState, DocRef
from app.plugins.embedded.source import (EmbeddedSource, decode_native_id,
                                          embed_input, encode_native_id,
                                          split_locator, split_row_id)

DIM = 8


def _rows(*specs):
    """specs: (article, seq, title, text)"""
    return {
        "id": [f"{a}_{s}" for a, s, _, _ in specs],
        "url": [f"https://tr.wikipedia.org/wiki/{t}" for _, _, t, _ in specs],
        "title": [t for _, _, t, _ in specs],
        "text": [x for _, _, _, x in specs],
        "embedding": [np.ones(DIM, dtype=np.float32).tolist() for _ in specs],
    }


def _write_shard(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(rows), path)


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    _write_shard(root / "data" / "tr" / "000.parquet", _rows(
        ("10", 0, "Cengiz Han", "para one"),
        ("10", 1, "Cengiz Han", "para two"),
        ("11", 0, "Ankara", "capital city"),
        ("12", 0, "Bursa", "straddle start"),
    ))
    _write_shard(root / "data" / "tr" / "001.parquet", _rows(
        ("12", 1, "Bursa", "straddle end"),
        ("13", 0, "Izmir", "coastal city"),
    ))
    return root


def test_native_id_roundtrip():
    n = encode_native_id("tr", 12345)
    assert decode_native_id(n) == ("tr", 12345)
    assert encode_native_id("en", 5) != encode_native_id("de", 5)
    assert split_row_id("12345_3") == ("12345", 3)


def test_discover_groups_articles_and_straddles_shards(corpus, tmp_path):
    src = EmbeddedSource()
    refs = list(src.discover(DiscoverState(corpus_root=corpus,
                                            state_dir=tmp_path)))
    assert all(r.error is None for r in refs)
    by_article = {r.extra["article"]: r for r in refs}
    assert set(by_article) == {"10", "11", "12", "13"}

    two = split_locator(by_article["10"].locator)
    assert len(two) == 1 and two[0][1:] == (0, 2)

    # article 12 straddles 000 -> 001: one ref, two segments
    segs = split_locator(by_article["12"].locator)
    assert len(segs) == 2
    assert segs[0][0].endswith("000.parquet") and segs[0][1:] == (3, 4)
    assert segs[1][0].endswith("001.parquet") and segs[1][1:] == (0, 1)

    assert by_article["11"].native_id == str(encode_native_id("tr", 11))
    assert by_article["11"].extra["lang"] == "tr"
    assert by_article["11"].extra["title"] == "Ankara"


def test_discover_cursor_skips_unchanged_shards(corpus, tmp_path):
    src = EmbeddedSource()
    state = DiscoverState(corpus_root=corpus, state_dir=tmp_path)
    first = list(src.discover(state))
    assert len(first) == 4
    assert list(src.discover(state)) == []   # nothing changed → stat pass


def test_corrupt_shard_costs_one_error_ref_not_the_scan(corpus, tmp_path):
    (corpus / "data" / "tr" / "000a.parquet").write_bytes(b"not parquet")
    src = EmbeddedSource()
    refs = list(src.discover(DiscoverState(corpus_root=corpus,
                                            state_dir=tmp_path)))
    errors = [r for r in refs if r.error]
    assert len(errors) == 1 and "unreadable shard" in errors[0].error
    # every healthy article still discovered
    assert {r.extra["article"] for r in refs if not r.error} == {"10", "11", "12", "13"}


def test_unrecognized_layout_is_an_error_ref(tmp_path):
    root = tmp_path / "corpus"
    _write_shard(root / "elsewhere" / "x.parquet", _rows(("1", 0, "T", "t")))
    src = EmbeddedSource()
    refs = list(src.discover(DiscoverState(corpus_root=root, state_dir=None)))
    assert len(refs) == 1 and "unrecognized shard path" in refs[0].error


def test_fetch_parse_title_prefixed_sections_in_seq_order(corpus, tmp_path):
    src = EmbeddedSource()
    refs = {r.extra["article"]: r
            for r in src.discover(DiscoverState(corpus_root=corpus, state_dir=None))}
    doc = src.parse(src.fetch(refs["12"]))
    assert doc.uid == f"embedded:{encode_native_id('tr', 12)}"
    assert doc.title == "Bursa" and doc.language == "tr"
    assert doc.keys == ["wikipedia:tr:12"]
    assert [b for _, b in doc.sections] == [
        embed_input("Bursa", "straddle start"),
        embed_input("Bursa", "straddle end"),
    ]
    assert doc.sections[0][1].startswith("Bursa\n")


def test_parse_recovers_lang_from_native_id_for_manifest_refs(corpus):
    """plugin_io rebuilds refs from manifest rows with EMPTY extra — the
    plugin must recover language and article from the encoded native_id."""
    src = EmbeddedSource()
    live = {r.extra["article"]: r
            for r in src.discover(DiscoverState(corpus_root=corpus, state_dir=None))}
    bare = DocRef(source_id=src.source_id, native_id=live["11"].native_id,
                  locator=live["11"].locator)
    doc = src.parse(src.fetch(bare))
    assert doc.language == "tr" and doc.keys == ["wikipedia:tr:11"]


def test_verbatim_chunking_preserves_producer_boundaries(corpus):
    src = EmbeddedSource()
    assert src.chunking_profile == "verbatim"
    chunk = chunking.chunker_for(src.chunking_profile)
    huge = "x" * 100_000   # far over CHUNK_TOKENS*4 — must NOT split
    sections = [("", "short one"), ("", huge)]
    chunks = list(chunk("embedded:1", 1, sections))
    assert len(chunks) == 2
    assert chunks[0]["seq"] == 0 and chunks[1]["seq"] == 1
    assert chunks[1]["text"] == huge


def test_fetch_uses_the_one_slot_shard_cache(corpus, monkeypatch):
    """Every per-article fetch re-parsing a ~437 MB shard would make rdf/fts
    unusable at ~95k articles per shard — consecutive fetches of the same
    shard must parse it once (pubmed's container-cache lesson)."""
    import app.plugins.embedded.source as mod
    mod._shard_cache.clear()
    real = pq.read_table
    calls = []

    def counting(path, *a, **k):
        calls.append(str(path))
        return real(path, *a, **k)
    # _shard_rows re-imports pyarrow.parquet at call time, so patching the
    # module attribute is exactly what it sees
    monkeypatch.setattr(pq, "read_table", counting)

    src = EmbeddedSource()
    refs = {r.extra["article"]: r
            for r in src.discover(DiscoverState(corpus_root=corpus, state_dir=None))}
    src.fetch(refs["10"])
    src.fetch(refs["11"])   # same shard — must hit the cache
    shard_reads = [c for c in calls if c.endswith("000.parquet")]
    assert len(shard_reads) == 1


def test_chunker_dispatch_default_is_unchanged():
    assert chunking.chunker_for(None) is chunking.chunk_book
    assert chunking.chunker_for("book") is chunking.chunk_book
    assert chunking.chunker_for("abstract") is chunking.chunk_book
    assert chunking.chunker_for("wiki") is chunking.chunk_book
    assert chunking.chunker_for("verbatim") is chunking.chunk_verbatim

