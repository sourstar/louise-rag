"""A collection re-created at the same path is written through NEW handles.

normattiva, 2026-10-13: the collection was deleted and created again under
the same name, so at the same paths, while the worker ran. The text index's
handles are cached by path, and the new collection was written through the
old Index and writer, whose files had been deleted: "An index writer was
killed … io::Error" on 244 documents, each one an error that takes it out of
every selection. The worker now forgets a removed collection's handles
(fts.forget, catalog.forget, embeddings.forget).
"""
import shutil

import pytest

from app import collections as collections_mod, fts


def _collection(tmp_path):
    return collections_mod.Collection(
        name="c", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "c" / "m.db",
        fts_dir=tmp_path / "c" / "fts", lance_dir=tmp_path / "c" / "lance")


def _chunks(book):
    return [{"id": f"c:{book}:0", "chapter": "", "text": f"il testo dell'atto {book}"}]


def test_a_re_created_collection_indexes_through_new_handles(tmp_path):
    c = _collection(tmp_path)
    fts.index_book("it", 1, _chunks(1), c)
    assert fts.search("it", "atto", collection=c)
    shutil.rmtree(tmp_path / "c")                    # deleted while the process runs
    assert fts.forget(c.fts_dir) >= 1, "the removed collection's handles are dropped"
    fts.index_book("it", 2, _chunks(2), c)          # created again at the same path
    hits = fts.search("it", "atto", collection=c)
    assert hits and all(str(h.get("book_id", 2)) != "1" for h in hits if isinstance(h, dict)), hits
    fts.close_all()


def test_forget_leaves_other_collections_alone(tmp_path):
    a, b = _collection(tmp_path / "a"), _collection(tmp_path / "b")
    fts.index_book("it", 1, _chunks(1), a)
    fts.index_book("it", 1, _chunks(1), b)
    fts.forget(a.fts_dir)
    assert any(k[0] == str(b.fts_dir) for k in fts._indexes), "b's handles are kept"
    fts.close_all()
