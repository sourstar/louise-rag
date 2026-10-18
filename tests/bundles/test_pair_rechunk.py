"""A pair layer's stored chunk is re-embedded as the pair its record gives back.

docs/notes/input-forms.md (operator, 2026-10-08): "paths that re-embed stored
text re-chunk from the records" — a stored row keeps a chunk's text and not
the pair layer's two segments (`fields`), so verify-on-use (T4) and a bundle
import's re-embed proof re-cut the record through `layers.chunks_for_document`
and feed the encoder the pair, using it only when the re-cut chunk has the
stored id and text. A chunk whose record cannot be read, or does not give it
back, is inconclusive — never a pass. Text layers are untouched
(tests/bundles/test_verify_on_use.py, test_import.py).
"""
import dataclasses

import numpy as np
import pytest

from app import db, embeddings, layers as layers_mod, plugin_io, spaces, worker
from app.bundles import export as exp, importer, ledger, verify_on_use
from tests.bundles.helpers import DIM, make_collection, unit_vectors

PAIR_LAYER = layers_mod.Layer("abstract", 1, "default", input=dict(layers_mod.INPUT_PAIR))


@pytest.fixture(autouse=True)
def no_collection_files(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "no-config"))


@pytest.fixture
def collection(tmp_path, monkeypatch):
    """A collection whose one layer feeds the encoder PAIRS (09 §9.3), on an
    OpenAI-style endpoint — the one body that can say a pair."""
    from app import collections as collections_mod, config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.delenv("LOUISE_ROOT", raising=False)
    monkeypatch.setattr(config, "CORPUS_DIR", tmp_path / "corpus")
    recipe = collections_mod.EmbedRecipe(url="http://models:30437/upstream/medcpt-article",
                                         api="openai", model="", dtype="f32", ctx=512,
                                         dim=DIM)
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c=None: recipe)
    return dataclasses.replace(make_collection(tmp_path), layers=(PAIR_LAYER,))


@pytest.fixture
def con(collection):
    connection = db.connect(collection)
    connection.execute(
        "INSERT OR REPLACE INTO space(id,space_id,dim,components,pinned_at) VALUES(1,?,?,?,0)",
        (spaces.compute_space_id(spaces.gather_components(collection)), DIM, "{}"))
    return connection


def seed(con, collection, native_id: int, vector) -> dict:
    """A document on disk and in the manifest, its one chunk in Lance, cut
    the way every stage cuts it."""
    book = collection.corpus_dir / str(native_id)
    book.mkdir(parents=True, exist_ok=True)
    (book / f"pg{native_id}.txt").write_text(
        f"CHAPTER I\n\nThe keeper of lamp {native_id} counted ships.\n\n"
        f"CHAPTER II\n\nThe ledger of lamp {native_id} showed a deficit.\n", encoding="utf-8")
    uid = f"gutenberg:{native_id}"
    con.execute(
        "INSERT OR REPLACE INTO documents(uid,source,native_id,path,lang,title,"
        "rdf_done,fts_done,embed_done,updated_at) VALUES(?,?,?,?,?,?,1,1,0,0)",
        (uid, "gutenberg", str(native_id), str(book), "en", f"Doc {native_id}"))
    row = {"uid": uid, "native_id": native_id, "path": str(book), "source": "gutenberg",
           "lang": "en"}
    chunk, = layers_mod.chunks_for_document(
        collection, row, plugin_io.sections(collection.plugin_instance(), row))
    assert chunk["fields"] is not None, "a pair layer's chunk carries its two segments"
    lance = {"id": chunk["id"], "book_id": native_id, "lang": "en", "seq": chunk["seq"],
             "chapter": "", "text": chunk["text"], "vector": np.asarray(vector, np.float32)}
    conn = embeddings._conn(collection)
    if embeddings.TABLE in conn.table_names():
        conn.open_table(embeddings.TABLE).add([lance])
    else:
        embeddings._create_table(conn, [lance])
    return chunk


@pytest.fixture
def imported(con, collection, producer_key, monkeypatch):
    """Six documents whose vectors arrived in an accepted bundle — through the
    importer's re-embed proof, which must have fed the encoder pairs."""
    vectors = unit_vectors(6, seed=3)
    chunks = [seed(con, collection, n, v) for n, v in zip(range(1, 7), vectors)]
    by_pair = {tuple(c["fields"]): v for c, v in zip(chunks, vectors)}
    fed: list = []

    def encoder(batch, **kw):
        fed.extend(batch)
        return [by_pair[x] for x in batch]          # a text here is a KeyError

    monkeypatch.setattr(embeddings, "_embed_texts", encoder)
    inbox = importer.inbox_dir(collection)
    inbox.mkdir(parents=True, exist_ok=True)
    exp.export_bundle(con, collection, [c["id"].rsplit(":", 2)[0] for c in chunks],
                      inbox / "b1", signing_key=producer_key)
    assert worker._drain_bundle_inbox(con, collection) == 1
    assert ledger.verification_states(con, [c["id"] for c in chunks]), "imported"
    assert fed and all(isinstance(x, tuple) for x in fed), \
        "the import's re-embed proof fed the encoder the records' pairs"
    fed.clear()
    return chunks, by_pair, fed


def test_verify_on_use_re_embeds_the_pair_the_record_gives_back(imported, con, collection):
    chunks, by_pair, fed = imported
    ids = [c["id"] for c in chunks]
    out = verify_on_use.verify_chunks(con, collection, ids)
    assert out["checked"] == 6 and out["verified"] == 6 and out["inconclusive"] == 0
    assert sorted(fed) == sorted(by_pair), "each chunk fed as its record's pair"


def test_a_poisoned_pair_vector_is_still_caught(imported, con, collection):
    chunks, by_pair, fed = imported
    poisoned = dict(by_pair)
    poisoned[tuple(chunks[2]["fields"])] = unit_vectors(1, seed=999)[0]
    out = verify_on_use.verify_chunks(
        con, collection, [c["id"] for c in chunks],
        embed_fn=lambda batch: [poisoned[tuple(x["fields"])] for x in batch])
    assert out["failed"] == 1 and out["verified"] == 5


def test_a_record_that_does_not_give_the_chunk_back_is_inconclusive(imported, con, collection):
    """The stored text and the record disagree, or the record is gone: the
    chunk cannot be checked here. Never verified, never condemned."""
    chunks, by_pair, fed = imported
    # the record of the first now reads otherwise than the stored chunk
    first = collection.corpus_dir / "1" / "pg1.txt"
    first.write_text("CHAPTER I\n\nAnother text entirely.\n", encoding="utf-8")
    # the second's record is no longer in this node's manifest
    con.execute("DELETE FROM documents WHERE uid='gutenberg:2'")
    ids = [c["id"] for c in chunks]
    out = verify_on_use.verify_chunks(con, collection, ids)
    assert out["inconclusive"] == 2 and out["verified"] == 4 and out["failed"] == 0
    states = ledger.verification_states(con, ids[:2])
    assert {s["state"] for s in states.values()} == {"imported"}, "left as it was"
    assert tuple(chunks[0]["fields"]) not in fed and tuple(chunks[1]["fields"]) not in fed


def test_a_bundle_whose_vectors_another_function_made_is_refused(con, collection,
                                                                 producer_key, monkeypatch):
    """The re-embed proof, in the pair form: vectors that are not the pairs'
    function fail it, exactly as a wrong model's do for text."""
    vectors = unit_vectors(6, seed=4)
    chunks = [seed(con, collection, n, v) for n, v in zip(range(1, 7), vectors)]
    monkeypatch.setattr(embeddings, "_embed_texts",
                        lambda batch, **kw: unit_vectors(len(batch), seed=5))
    inbox = importer.inbox_dir(collection)
    inbox.mkdir(parents=True, exist_ok=True)
    exp.export_bundle(con, collection, [c["id"].rsplit(":", 2)[0] for c in chunks],
                      inbox / "b1", signing_key=producer_key)
    assert worker._drain_bundle_inbox(con, collection) == 1, "processed: refused, not retried"
    assert not ledger.verification_states(con, [c["id"] for c in chunks]), "nothing imported"
