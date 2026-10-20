"""tests/test_fts_ties.py — a text leg's list is the same on every peer with
the same members (05 §5.1, amended 2026-10-20; München's find): equal BM25
scores order by the record's uid bytes, then the chunk's place in its
record, and a tie group straddling the cut is read whole before the list is
cut — tantivy's own order among ties depends on how the index was built."""
from app import collections as collections_mod, fts


def _collection(tmp_path, name):
    return collections_mod.Collection(
        name=name, plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / name / "m.db",
        fts_dir=tmp_path / name / "fts", lance_dir=tmp_path / name / "lance")


def _book(book, seqs):
    # every chunk the same text: every hit of a query ties
    return [{"id": f"g:{book}:{s}:1", "chapter": "", "text": "the white whale"} for s in seqs]


def test_ties_order_by_uid_then_the_chunks_place_and_the_cut_is_the_same_whatever_the_build_order(tmp_path):
    a, b = _collection(tmp_path, "a"), _collection(tmp_path, "b")
    books = {1: [133, 1, 2], 10: [0, 5], 2: [7]}
    for book in (1, 10, 2):                          # one build order
        fts.index_book("en", book, _book(book, books[book]), a)
    for book in (2, 10, 1):                          # another, as another peer's
        fts.index_book("en", book, _book(book, list(reversed(books[book]))), b)
    full = [h["chunk_id"] for h in fts.search("en", "whale", limit=50, collection=a)]
    assert full == ["g:1:1:1", "g:1:2:1", "g:1:133:1", "g:10:0:1", "g:10:5:1", "g:2:7:1"], \
        "uid bytes, then the chunk's place: not the id's string order"
    for limit in (1, 2, 3, 4):
        got_a = [h["chunk_id"] for h in fts.search("en", "whale", limit=limit, collection=a)]
        got_b = [h["chunk_id"] for h in fts.search("en", "whale", limit=limit, collection=b)]
        assert got_a == got_b == full[:limit], f"the cut at {limit} is the same on both builds"
    one = fts.search("en", "whale", limit=1, collection=a, book_ids=[1])
    assert [h["chunk_id"] for h in one] == ["g:1:1:1"], "a record's best chunk among equals: its earliest"
    assert fts.tie_key("g:5097:1:1") < fts.tie_key("g:5097:133:1") and fts.tie_key("x") == (b"x", 0, "x")
    fts.close_all()
