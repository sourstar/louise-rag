"""A match in a chunk's heading against one in its body (fts.CHAPTER_BOOST,
BM25F's field weight). München, 2026-10-23: a few-token heading made one
heading term outweigh a whole body — Moby-Dick's quarter-deck chapter (white
x12, whale x10, captain x5) below a chunk headed "The White Whale Shoal."."""
from app import collections as collections_mod, fts


def _collection(tmp_path):
    return collections_mod.Collection(
        name="b", plugin="gutenberg", profile=None, embed_mode_default=None,
        corpus_dir=tmp_path / "corpus", manifest_path=tmp_path / "m.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lance")


def _index(c):
    # the heading carries the words; the body barely
    fts.index_book("en", 1, [{"id": "g:1:0:1", "chapter": "The White Whale Shoal.",
                              "text": "the boat drifted over the shoal at dawn " * 40}], c)
    # the body carries them, the heading none
    fts.index_book("en", 2, [{"id": "g:2:0:1", "chapter": "CHAPTER 36. The Quarter-Deck.",
                              "text": ("the captain spoke of the white whale, the white whale "
                                       "that took his leg; whale and captain and white ") * 6}], c)


def _scores(c):
    return {h["book_id"]: h["score"] for h in fts.search("en", "white whale captain", limit=2, collection=c)}


def test_the_heading_weight_scales_only_the_heading_s_share(tmp_path, monkeypatch):
    c = _collection(tmp_path)
    _index(c)
    at_one = _scores(c)
    monkeypatch.setattr(fts, "CHAPTER_BOOST", 0.1)
    at_tenth = _scores(c)
    # book 1 matches by its heading alone, book 2 by its body alone
    assert abs(at_tenth[1] - 0.1 * at_one[1]) < 1e-4, "a heading-only match weighs a tenth"
    assert abs(at_tenth[2] - at_one[2]) < 1e-4, "a body match is untouched"
    fts.close_all()


def test_the_weight_applies_where_the_index_stems_the_body_itself(tmp_path, monkeypatch):
    """English is stemmed by tantivy (one parse over body and chapter);
    the PyStemmer groups build two clauses — both carry the weight."""
    monkeypatch.setattr(fts, "CHAPTER_BOOST", 0.0)
    c = _collection(tmp_path)
    _index(c)
    assert set(_scores(c)) == {2} or _scores(c).get(1, 0.0) == 0.0
    fts.close_all()


def test_the_pystemmer_groups_carry_the_weight_too(tmp_path, monkeypatch):
    """Greek is stemmed by PyStemmer, and its heading is a separate clause."""
    c = _collection(tmp_path)
    fts.index_book("el", 1, [{"id": "g:1:0:1", "chapter": "Η λευκή φάλαινα",
                              "text": "η βάρκα έπλεε στη θάλασσα " * 30}], c)
    fts.index_book("el", 2, [{"id": "g:2:0:1", "chapter": "Κεφάλαιο",
                              "text": "η λευκή φάλαινα και ο καπετάνιος " * 6}], c)

    def scores():
        return {h["book_id"]: h["score"] for h in fts.search("el", "λευκή φάλαινα", limit=2, collection=c)}
    at_one = scores()
    monkeypatch.setattr(fts, "CHAPTER_BOOST", 0.1)
    at_tenth = scores()
    assert abs(at_tenth[1] - 0.1 * at_one[1]) < 1e-4 and abs(at_tenth[2] - at_one[2]) < 1e-4
    fts.close_all()
