"""06 §6.3: a changed record's vectors are carried over BY CHUNK HASH.

    "it is recut, and its vectors are carried over by chunk hash, never by
    chunk id: the peer maps the hashes of the chunks it already holds for
    that record to their vectors, and every recut chunk whose hash is in
    that map keeps its vector under whatever id it now has. Only a chunk
    whose text is new is embedded"

Nothing implemented it. The only reuse matched on (chunk_id AND byte-equal
text), and a chunk id is `{uid}:{seq}:{version}` — `seq` is a POSITION. So
inserting one paragraph at the top of an article shifted the id of every
chunk after it while changing the text of none of them, the all-or-nothing
`rebuild` fired, and the whole document went back to the encoder to learn
what it already knew.

Measured on a 71-chunk article before this: an inserted paragraph left 70
texts byte-identical and 0 (id, text) pairs intact — 73 encoder calls where
3 were needed. At 14M articles that is the difference the spec calls "the
very cost `cdc` exists to avoid".
"""
import numpy as np
import pytest

from app import collections as collections_mod, embeddings


@pytest.fixture
def collection(tmp_path, isolated_config, monkeypatch):
    c = collections_mod.Collection(
        name="wv", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "wv" / "m.db",
        fts_dir=tmp_path / "wv" / "fts", lance_dir=tmp_path / "wv" / "lance")
    return c


def _chunks(texts, uid="gutenberg:1", version=1):
    return [{"id": f"{uid}:{i}:{version}", "seq": i, "chapter": "",
             "text": t} for i, t in enumerate(texts)]


@pytest.fixture
def encoder(monkeypatch):
    """Counts what actually reaches the encoder, and gives each distinct
    text a distinct, recognisable vector."""
    calls = {"texts": []}

    def fake(texts, recipe=None, **kw):
        calls["texts"].extend(texts)
        return [[float(len(t)), 1.0, 2.0, 3.0] for t in texts]

    monkeypatch.setattr(embeddings, "embed_texts", fake)
    return calls


def test_an_inserted_chunk_embeds_only_itself(collection, encoder):
    """THE case the spec names: a paragraph inserted at the TOP."""
    before = ["alpha", "beta", "gamma"]
    embeddings.upsert_book(1, "en", _chunks(before), collection=collection)
    assert encoder["texts"] == before
    encoder["texts"].clear()

    after = ["NEW", "alpha", "beta", "gamma"]
    embeddings.upsert_book(1, "en", _chunks(after), collection=collection)
    assert encoder["texts"] == ["NEW"], \
        "every id shifted, but only one TEXT is new — the rest must be carried"


def test_a_one_word_edit_embeds_only_the_edited_chunk(collection, encoder):
    embeddings.upsert_book(1, "en", _chunks(["a", "b", "c"]), collection=collection)
    encoder["texts"].clear()
    embeddings.upsert_book(1, "en", _chunks(["a", "B!", "c"]), collection=collection)
    assert encoder["texts"] == ["B!"]


def test_a_carried_vector_is_the_same_vector(collection, encoder):
    """Carried, not recomputed: the stored bytes move to the new id."""
    embeddings.upsert_book(1, "en", _chunks(["alpha", "beta"]), collection=collection)
    first = embeddings.vectors_by_chunk_id(["gutenberg:1:0:1"],
                                            collection=collection)
    alpha = list(first["gutenberg:1:0:1"])

    embeddings.upsert_book(1, "en", _chunks(["NEW", "alpha", "beta"]),
                            collection=collection)
    moved = embeddings.vectors_by_chunk_id(["gutenberg:1:1:1"],
                                            collection=collection)
    assert [round(x, 4) for x in moved["gutenberg:1:1:1"]] == \
        [round(x, 4) for x in alpha], "alpha's vector must have moved, not been recomputed"


def test_a_dropped_chunk_leaves_no_row(collection, encoder):
    """"a vector whose hash no longer appears is dropped"."""
    embeddings.upsert_book(1, "en", _chunks(["a", "b", "c"]), collection=collection)
    embeddings.upsert_book(1, "en", _chunks(["a", "c"]), collection=collection)
    got = embeddings.vectors_by_chunk_id(
        ["gutenberg:1:0:1", "gutenberg:1:1:1", "gutenberg:1:2:1"],
        collection=collection)
    assert set(got) == {"gutenberg:1:0:1", "gutenberg:1:1:1"}, \
        "the document now has two chunks; a third row would be a stale vector"


def test_an_unchanged_document_still_embeds_nothing(collection, encoder):
    """The resume path must not regress: identical ids AND text = no work."""
    embeddings.upsert_book(1, "en", _chunks(["a", "b"]), collection=collection)
    encoder["texts"].clear()
    embeddings.upsert_book(1, "en", _chunks(["a", "b"]), collection=collection)
    assert encoder["texts"] == []


def test_a_wholly_new_document_embeds_everything(collection, encoder):
    embeddings.upsert_book(1, "en", _chunks(["a", "b"]), collection=collection)
    encoder["texts"].clear()
    embeddings.upsert_book(1, "en", _chunks(["x", "y"]), collection=collection)
    assert encoder["texts"] == ["x", "y"], "nothing to carry, so all of it"
