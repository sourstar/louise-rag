"""Incremental, resumable vector writes (embeddings.upsert_book).

Measured live on 2026-08-27: gutenberg 4656 is a 21 MB book that chunks
into 5,335 pieces, and the endpoint takes ~3.6 s per chunk — 5.3 hours
inside ONE call. The old shape embedded everything into memory and wrote
once at the end, so each of four restarts that afternoon discarded the
whole thing: the GPU ran continuously and produced nothing that survived,
while `embed_done` never moved and every rate read zero.

These tests pin the two properties that make that impossible now: work
already done is reused, and work not yet done is never marked done.
"""
import threading

import numpy as np
import pytest

from app import collections as collections_mod, config, embeddings


@pytest.fixture
def collection(tmp_path, isolated_config):
    return collections_mod.Collection(
        name="books", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance", embed=None)


def chunks(n, *, text=lambda i: f"chunk {i}"):
    return [{"id": f"books:1:{i}:1", "seq": i, "chapter": "", "text": text(i)}
            for i in range(n)]


@pytest.fixture
def calls(monkeypatch):
    """Record every text sent to the encoder, and return a deterministic
    vector so the assertions are about WHAT was embedded, not values."""
    seen = []

    def fake(texts, timeout=300, *, recipe=None, query=False):
        seen.extend(texts)
        return [[float(len(t)), 1.0, 0.0, 0.0] for t in texts]

    monkeypatch.setattr(embeddings, "_embed_texts", fake)
    return seen


def stored(collection):
    db = embeddings._conn(collection)
    tbl = db.open_table(embeddings.TABLE)
    return {r["id"]: r["text"] for r in
            tbl.search().where("book_id = 1").select(["id", "text"]).limit(10_000).to_list()}


# --------------------------------------------------------------- the basics

def test_a_small_document_is_written_exactly_as_before(collection, calls):
    n = embeddings.upsert_book(1, "en", chunks(5), collection)
    assert n == 5
    assert len(calls) == 5
    assert len(stored(collection)) == 5


def test_a_long_document_is_written_in_slices(collection, calls, monkeypatch):
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    n = embeddings.upsert_book(1, "en", chunks(10), collection)
    assert n == 10
    assert len(stored(collection)) == 10


# ------------------------------------------------------------- the resume

def test_a_rerun_embeds_nothing_when_the_document_is_unchanged(collection, calls,
                                                                monkeypatch):
    """The point of the whole change: what the GPU already produced is not
    produced again."""
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    embeddings.upsert_book(1, "en", chunks(10), collection)
    calls.clear()
    n = embeddings.upsert_book(1, "en", chunks(10), collection)
    assert calls == [], "re-embedded work that was already on disk"
    assert n == 10
    assert len(stored(collection)) == 10


def test_an_interrupted_document_resumes_where_it_stopped(collection, calls,
                                                           monkeypatch):
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    stop = threading.Event()

    real = embeddings._embed_texts
    written = {"slices": 0}

    def counting(texts, timeout=300, *, recipe=None, query=False):
        written["slices"] += 1
        if written["slices"] == 2:
            stop.set()                      # SIGTERM arrives mid-document
        return real(texts, timeout, recipe=recipe, query=query)

    monkeypatch.setattr(embeddings, "_embed_texts", counting)
    with pytest.raises(embeddings.EmbedInterrupted):
        embeddings.upsert_book(1, "en", chunks(12), collection, stop=stop)
    part = stored(collection)
    assert len(part) == 8, "the finished slices must be durable"

    # the next run embeds only what is missing
    calls.clear()
    monkeypatch.setattr(embeddings, "_embed_texts", real)
    n = embeddings.upsert_book(1, "en", chunks(12), collection)
    assert len(calls) == 4, f"re-embedded {len(calls)} chunks, expected the 4 missing"
    assert n == 12
    assert len(stored(collection)) == 12


def test_the_last_slice_does_not_raise_on_stop(collection, calls, monkeypatch):
    """Stopping exactly when the document finished is not an interruption —
    raising there would defer a document that is actually complete."""
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    stop = threading.Event()
    stop.set()
    with pytest.raises(embeddings.EmbedInterrupted):
        embeddings.upsert_book(1, "en", chunks(12), collection, stop=stop)
    # a document that fits in one slice completes even with stop set
    assert embeddings.upsert_book(2, "en", [
        {"id": "books:2:0:1", "seq": 0, "chapter": "", "text": "only"}],
        collection, stop=stop) == 1


# ------------------------------------------------------- a changed document

def test_a_changed_document_is_rebuilt_whole_not_patched(collection, calls,
                                                          monkeypatch):
    """Chunk ids are POSITIONAL (`{uid}:{seq}:{version}`), so an id that is
    already present says nothing about whether its vector still belongs to
    the current text. Re-chunked content reuses the same ids with different
    text, and reusing those vectors would leave the document silently
    describing what it used to say."""
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    embeddings.upsert_book(1, "en", chunks(8), collection)
    calls.clear()
    revised = chunks(8, text=lambda i: f"REVISED {i}")
    n = embeddings.upsert_book(1, "en", revised, collection)
    assert len(calls) == 8, "a changed document must be re-embedded whole"
    assert n == 8
    assert set(stored(collection).values()) == {c["text"] for c in revised}


def test_a_shrunken_document_leaves_no_orphan_chunks(collection, calls,
                                                      monkeypatch):
    """The old delete-then-insert removed rows the new chunking no longer
    produces. Appending slices must not lose that."""
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    embeddings.upsert_book(1, "en", chunks(12), collection)
    n = embeddings.upsert_book(1, "en", chunks(5), collection)
    assert n == 5
    assert len(stored(collection)) == 5, "orphaned vectors from the longer version"


def test_a_grown_document_only_embeds_the_new_chunks(collection, calls,
                                                      monkeypatch):
    monkeypatch.setattr(config, "EMBED_SLICE_CHUNKS", 4)
    embeddings.upsert_book(1, "en", chunks(8), collection)
    calls.clear()
    n = embeddings.upsert_book(1, "en", chunks(12), collection)
    assert len(calls) == 4, "the first 8 were unchanged and already embedded"
    assert n == 12
    assert len(stored(collection)) == 12
