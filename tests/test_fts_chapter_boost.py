"""A match in a chunk's heading against one in its body: BM25F (Robertson,
Zaragoza & Taylor, CIKM 2004; fts.HEADING_IN_BODY is the heading's weight).
München, 2026-10-23: summing a few-token heading's own BM25 score made one
heading term outweigh a whole body — Moby-Dick's quarter-deck chapter (white
x12, whale x10, captain x5) below a chunk headed "The White Whale Shoal."."""
import math

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


def _bm25f(docs, term, w_heading, k1=1.2, b=0.75):
    """The paper's formula: per-document tf and length are the fields'
    weighted sums (body weight 1), saturated once; idf from the documents
    holding the term in any field (tantivy's idf and k1, b)."""
    tf = [w_heading * h.count(term) + t.count(term) for h, t in docs]
    dl = [w_heading * len(h) + len(t) for h, t in docs]
    avgdl = sum(dl) / len(docs)
    n = sum(1 for f in tf if f)
    idf = math.log(1 + (len(docs) - n + 0.5) / (n + 0.5))
    return [idf * f * (k1 + 1) / (f + k1 * (1 - b + b * d / avgdl)) if f else 0.0
            for f, d in zip(tf, dl)]


def test_the_score_is_the_papers_bm25f(tmp_path, monkeypatch):
    """Not an approximation of BM25F: with an integer weight the index IS it.
    Short texts, so tantivy's field norms are exact."""
    monkeypatch.setattr(fts, "HEADING_IN_BODY", 3)
    c = _collection(tmp_path)
    docs = [("whale", "boat sea dawn"),
            ("ship", "whale whale captain sea"),
            ("dawn", "captain boat"),
            ("whale shoal", "whale")]
    for i, (h, t) in enumerate(docs, 1):
        fts.index_book("en", i, [{"id": f"g:{i}:0:1", "chapter": h, "text": t}], c)
    got = {hit["book_id"]: hit["score"] for hit in fts.search("en", "whale", limit=10, collection=c)}
    want = _bm25f([(h.split(), t.split()) for h, t in docs], "whale", 3)
    for i, w in enumerate(want, 1):
        assert math.isclose(got.get(i, 0.0), w, rel_tol=1e-4), (i, got, want)
    fts.close_all()


def test_the_heading_folded_into_the_body_saturates_once(tmp_path, monkeypatch):
    """One field, one saturation, one document frequency; a heading match is
    k more occurrences, not a second document. The stored text the reader
    sees is untouched."""
    monkeypatch.setattr(fts, "HEADING_IN_BODY", 2)
    c = _collection(tmp_path)
    _index(c)
    hits = fts.search("en", "white whale captain", limit=2, collection=c)
    assert [h["book_id"] for h in hits] == [2, 1], "the body that carries the words wins"
    assert hits[1]["text"].startswith("the boat drifted"), "the heading is indexed, never shown in the body"
    assert hits[1]["chapter"] == "The White Whale Shoal."
    fts.close_all()


def test_the_heading_folded_in_where_pystemmer_stems(tmp_path, monkeypatch):
    monkeypatch.setattr(fts, "HEADING_IN_BODY", 2)
    c = _collection(tmp_path)
    fts.index_book("el", 1, [{"id": "g:1:0:1", "chapter": "Η λευκή φάλαινα",
                              "text": "η βάρκα έπλεε στη θάλασσα " * 30}], c)
    hits = fts.search("el", "λευκή φάλαινα", limit=1, collection=c)
    assert [h["book_id"] for h in hits] == [1], "the heading alone still finds its chunk"
    assert hits[0]["text"].startswith("η βάρκα")
    fts.close_all()


def test_zero_is_today_s_index():
    """0, the default: nothing new is written, so every index on every peer
    opens as before (a schema change would refuse to open them)."""
    assert fts.HEADING_IN_BODY == 0
    assert not hasattr(fts, "CHAPTER_BOOST"), "no knob re-weights the per-field sum"
    assert fts._indexed(None, "The White Whale", "body") == {}
    assert fts._indexed("greek", "Η φάλαινα", "κείμενο") == {
        "body_stem": fts.stemming.stem_text("greek", "κείμενο")}
