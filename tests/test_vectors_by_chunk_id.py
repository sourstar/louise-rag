"""`embeddings.vectors_by_chunk_id` reads whole books (the filter is on the
indexed `book_id`) and keeps only the ids asked for — in Arrow, before any
row becomes Python. Turned into lists first, 500 of gutenberg's books (~65,000
rows of 2,560 floats) took 8 GB to give back a few vectors (the bakeoff's
pool, 2026-11-02, killed by its memory cap)."""
import pytest

from app import collections as collections_mod, embeddings


@pytest.fixture
def collection(tmp_path, isolated_config):
    return collections_mod.Collection(
        name="wv", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "wv" / "m.db",
        fts_dir=tmp_path / "wv" / "fts", lance_dir=tmp_path / "wv" / "lance")


@pytest.fixture(autouse=True)
def encoder(monkeypatch):
    def fake(texts, recipe=None, **kw):
        return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]
    monkeypatch.setattr(embeddings, "_embed_texts", fake)


def _chunks(book, n):
    return [{"id": f"gutenberg:{book}:{i}:1", "seq": i, "chapter": "", "text": "x" * (i + 1)}
            for i in range(n)]


def test_only_the_ids_asked_for_come_back_from_whole_books(collection):
    embeddings.upsert_book(1, "en", _chunks(1, 200), collection=collection)
    embeddings.upsert_book(2, "en", _chunks(2, 50), collection=collection)
    ask = ["gutenberg:1:0:1", "gutenberg:1:199:1", "gutenberg:2:7:1", "gutenberg:3:0:1"]
    got = embeddings.vectors_by_chunk_id(ask, collection=collection)
    assert set(got) == {"gutenberg:1:0:1", "gutenberg:1:199:1", "gutenberg:2:7:1"}
    assert [round(x, 4) for x in got["gutenberg:1:199:1"]] == [200.0, 1.0, 2.0, 3.0]
    assert [round(x, 4) for x in got["gutenberg:2:7:1"]] == [8.0, 1.0, 2.0, 3.0]
    assert all(isinstance(v, list) for v in got.values())


def test_with_text_gives_the_text_beside_the_vector(collection):
    embeddings.upsert_book(1, "en", _chunks(1, 30), collection=collection)
    got = embeddings._by_chunk_id(["gutenberg:1:4:1"], collection, with_text=True)
    text, vector = got["gutenberg:1:4:1"]
    assert text == "xxxxx" and [round(x, 4) for x in vector] == [5.0, 1.0, 2.0, 3.0]
