"""The bake-off's metrics, pinned by hand.

Every expected value below is computed by hand in the test body, not taken
from the implementation. These definitions are the ONLY thing that makes our
numbers comparable to `jurifindit/DAR-legal-it`'s published table, which was
produced by sentence-transformers' `InformationRetrievalEvaluator`; a silent
change to a formula here turns the comparison into two unrelated numbers
that look like a result.

The case that matters most is several relevant documents per query, because
JuriFindIT averages 4.49 of them and goes up to 128 — the regime where
recall@k, MRR@k and MAP@k all come apart from each other and from "did we
find one".
"""

from __future__ import annotations

import json
import sys
import types
from math import log2

import numpy as np
import pytest

from tools.encoder_bakeoff import Corpus, Queries, metrics_for, score_run

K3 = (3,)
K5 = (5,)


def test_single_relevant_at_rank_one_is_perfect() -> None:
    m = metrics_for([5, 1, 2], {5}, K3)
    assert m["recall@3"] == pytest.approx(1.0)
    assert m["ndcg@3"] == pytest.approx(1.0)
    assert m["mrr@3"] == pytest.approx(1.0)
    assert m["map@3"] == pytest.approx(1.0)


def test_single_relevant_at_rank_three() -> None:
    m = metrics_for([1, 2, 5], {5}, K3)
    # found it, so recall is whole; the rest pay for the position
    assert m["recall@3"] == pytest.approx(1.0)
    assert m["ndcg@3"] == pytest.approx((1 / log2(4)) / (1 / log2(2)))
    assert m["ndcg@3"] == pytest.approx(0.5)
    assert m["mrr@3"] == pytest.approx(1 / 3)
    assert m["map@3"] == pytest.approx(1 / 3)


def test_two_relevant_only_one_retrieved() -> None:
    # This is the case a hit-rate metric would score 1.0 and call a success.
    m = metrics_for([9, 7, 8], {7, 42}, K3)
    assert m["recall@3"] == pytest.approx(0.5)
    ideal = 1 / log2(2) + 1 / log2(3)
    assert m["ndcg@3"] == pytest.approx((1 / log2(3)) / ideal)
    assert m["ndcg@3"] == pytest.approx(0.3868528, abs=1e-6)
    assert m["mrr@3"] == pytest.approx(0.5)
    assert m["map@3"] == pytest.approx(0.5 / 2)


def test_two_relevant_both_retrieved_at_ranks_one_and_three() -> None:
    m = metrics_for(["a", "x", "b", "y", "z"], {"a", "b"}, K5)
    assert m["recall@5"] == pytest.approx(1.0)
    ideal = 1 / log2(2) + 1 / log2(3)
    assert m["ndcg@5"] == pytest.approx((1 / log2(2) + 1 / log2(4)) / ideal)
    assert m["ndcg@5"] == pytest.approx(0.919721, abs=1e-6)
    assert m["mrr@5"] == pytest.approx(1.0)
    # average precision: hits at ranks 1 and 3 -> (1/1 + 2/3) / min(5, 2)
    assert m["map@5"] == pytest.approx((1.0 + 2 / 3) / 2)


def test_nothing_relevant_in_top_k_scores_zero() -> None:
    m = metrics_for([1, 2, 3, 4], {4}, K3)  # the hit sits at rank 4
    assert m["recall@3"] == 0.0
    assert m["ndcg@3"] == 0.0
    assert m["mrr@3"] == 0.0
    assert m["map@3"] == 0.0


def test_more_relevant_than_k_caps_the_ideal_at_k() -> None:
    # 5 relevant, k=3, all three top slots correct: recall can only be 3/5,
    # but NDCG and MAP are 1.0 because 3 is all the room there was.
    m = metrics_for([1, 2, 3, 9, 9], {1, 2, 3, 4, 5}, K3)
    assert m["recall@3"] == pytest.approx(0.6)
    assert m["ndcg@3"] == pytest.approx(1.0)
    assert m["map@3"] == pytest.approx(1.0)
    assert m["mrr@3"] == pytest.approx(1.0)


def test_mrr_takes_the_first_hit_not_the_best_run() -> None:
    early = metrics_for([7, 9, 9, 9], {7, 8}, (4,))
    late = metrics_for([9, 9, 9, 7], {7, 8}, (4,))
    assert early["mrr@4"] == pytest.approx(1.0)
    assert late["mrr@4"] == pytest.approx(0.25)
    assert early["recall@4"] == late["recall@4"] == pytest.approx(0.5)


def _one_hot(dim: int, i: int) -> list[float]:
    v = [0.0] * dim
    v[i] = 1.0
    return v


def test_score_run_ranks_by_cosine_and_averages_over_queries() -> None:
    # Orthogonal unit vectors: query i is nearest document i, exactly.
    dim = 4
    doc = np.array([_one_hot(dim, i) for i in range(dim)], dtype=np.float32)
    corpus = Corpus(ids=[10, 20, 30, 40], texts=["", "", "", ""])
    qry = np.array([_one_hot(dim, 0), _one_hot(dim, 2)], dtype=np.float32)
    queries = Queries(texts=["q0", "q2"], relevant=[[10], [30]])
    scores = score_run(doc, qry, corpus, queries, ks=(1, 3))
    assert scores["recall@1"] == pytest.approx(1.0)
    assert scores["mrr@1"] == pytest.approx(1.0)
    assert scores["ndcg@3"] == pytest.approx(1.0)


def test_score_run_macro_averages_a_hit_and_a_miss() -> None:
    dim = 4
    doc = np.array([_one_hot(dim, i) for i in range(dim)], dtype=np.float32)
    corpus = Corpus(ids=[10, 20, 30, 40], texts=["", "", "", ""])
    qry = np.array([_one_hot(dim, 0), _one_hot(dim, 1)], dtype=np.float32)
    # the second query's only relevant document is the one it ranks last
    queries = Queries(texts=["hit", "miss"], relevant=[[10], [40]])
    scores = score_run(doc, qry, corpus, queries, ks=(1,))
    assert scores["recall@1"] == pytest.approx(0.5)
    assert scores["mrr@1"] == pytest.approx(0.5)


def test_slug_names_the_bare_query_side_explicitly() -> None:
    from tools.encoder_bakeoff import slug_of
    assert slug_of("") == "bare"
    a, b = slug_of("Instruct: x\nQuery: "), slug_of("query: ")
    assert a != b and a.startswith("q") and len(a) == 9
    assert slug_of("query: ") == b  # stable across calls, so runs collide on purpose


def test_query_prefix_reaches_queries_and_never_documents(tmp_path, monkeypatch) -> None:
    """The prefix is the whole difference between two rows of the report. If
    it ever leaked onto the corpus side, both rows would move together and
    the comparison would look fine while measuring nothing."""
    import numpy as _np

    from tools import encoder_bakeoff as bake

    corpus = bake.Corpus(ids=[1, 2], texts=["doc one", "doc two"])
    queries = bake.Queries(texts=["how?"], relevant=[[1]])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))

    seen: list[list[str]] = []

    def fake_embed_texts(texts, **kw):
        seen.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("app.embeddings._embed_texts", fake_embed_texts)

    bake.embed(tmp_path, "m", "http://x/upstream/m", "m", None, 8192, 1,
               20_000, query_prefix="Instruct: legal\nQuery: ")

    sent = [t for batch in seen for t in batch]
    assert "doc one" in sent and "doc two" in sent
    assert "Instruct: legal\nQuery: how?" in sent
    assert not any(t.startswith("Instruct:") and "doc" in t for t in sent)

    slug = bake.slug_of("Instruct: legal\nQuery: ")
    meta = json.loads((tmp_path / "m" / f"run-{slug}.json").read_text())
    assert meta["query_prefix"] == "Instruct: legal\nQuery: "
    assert _np.load(tmp_path / "m" / "corpus.npy").shape[0] == 2


def test_variants_of_one_model_share_the_corpus_vectors(tmp_path, monkeypatch) -> None:
    """A second query-side variant must not re-embed 23,617 documents — that
    is the whole reason the prefix keys only the query shards."""
    from tools import encoder_bakeoff as bake

    corpus = bake.Corpus(ids=[1, 2], texts=["a", "b"])
    queries = bake.Queries(texts=["q"], relevant=[[1]])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))
    calls: list[int] = []

    def fake_embed_texts(texts, **kw):
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("app.embeddings._embed_texts", fake_embed_texts)

    args = (tmp_path, "m", "http://x/upstream/m", "m", None, 8192, 1, 20_000)
    bake.embed(*args, query_prefix="")
    first = sum(calls)
    calls.clear()
    bake.embed(*args, query_prefix="query: ")
    # only the one query was embedded the second time
    assert sum(calls) == 1 < first
    assert len(bake.runs_in(tmp_path)) == 2


def test_a_limited_run_is_marked_and_refused_by_score(tmp_path, monkeypatch, capsys) -> None:
    """A smoke run must never reach the report. Every document left out is a
    distractor left out, so a partial corpus does not under-report — it
    over-reports, which is the direction nobody catches."""
    from tools import encoder_bakeoff as bake

    corpus = bake.Corpus(ids=[1, 2, 3, 4], texts=["a", "b", "c", "d"])
    queries = bake.Queries(texts=["q"], relevant=[[1]])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))
    monkeypatch.setattr("app.embeddings._embed_texts",
                        lambda texts, **kw: [[1.0, 0.0] for _ in texts])

    bake.embed(tmp_path, "m", "http://x/upstream/m", "m", None, 8192, 1,
               20_000, query_prefix="", limit=2)

    meta = json.loads((tmp_path / "m" / "run-bare.json").read_text())
    assert meta["limit"] == 2 and meta["documents"] == 2

    assert bake.score(tmp_path, 20_000) == 0
    assert "SKIPPED" in capsys.readouterr().out
    assert not (tmp_path / "m" / "scores-bare.json").exists()


def test_an_unlimited_run_is_scored_normally(tmp_path, monkeypatch) -> None:
    from tools import encoder_bakeoff as bake

    corpus = bake.Corpus(ids=[1, 2], texts=["a", "b"])
    queries = bake.Queries(texts=["q"], relevant=[[1]])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))
    monkeypatch.setattr("app.embeddings._embed_texts",
                        lambda texts, **kw: [[1.0, 0.0] for _ in texts])

    bake.embed(tmp_path, "m", "http://x/upstream/m", "m", None, 8192, 1, 20_000)
    assert bake.score(tmp_path, 20_000) == 0
    assert (tmp_path / "m" / "scores-bare.json").exists()


class _FakeST:
    """Records the shape of every forward pass it is asked for."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def encode(self, texts, batch_size=None, convert_to_numpy=True, **kw):
        self.batches.append(list(texts))
        return np.array([[float(len(t)), 1.0] for t in texts], dtype=np.float32)


def test_score_run_is_unaffected_by_vector_magnitude() -> None:
    # Cosine, not dot product: scaling a document must not promote it.
    dim = 3
    doc = np.array([_one_hot(dim, 0), [0.0, 9.0, 0.0]], dtype=np.float32)
    corpus = Corpus(ids=[1, 2], texts=["", ""])
    qry = np.array([[0.9, 0.1, 0.0]], dtype=np.float32)
    queries = Queries(texts=["q"], relevant=[[1]])
    assert score_run(doc, qry, corpus, queries, ks=(1,))["mrr@1"] == pytest.approx(1.0)


# ---- the BEIR layout (2026-10-28: the published sets of D4) --------------

def _beir(tmp_path, suite: str, corpus, queries, qrels, *, parquet: bool):
    """A directory as `fetch --suite` leaves it, without the network."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from tools import encoder_bakeoff as b
    out = tmp_path / suite
    out.mkdir()
    for kind, rows in (("corpus", corpus), ("queries", queries), ("qrels", qrels)):
        name = b._local(kind, b.SUITES[suite]["files"][kind])
        if parquet:
            pq.write_table(pa.Table.from_pylist(rows), out / name)
        else:
            (out / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (out / "SOURCE.json").write_text(json.dumps({"suite": suite}), encoding="utf-8")
    return out


def test_a_beir_set_maps_its_ids_to_positions_and_keeps_judged_queries(tmp_path) -> None:
    from tools.encoder_bakeoff import load
    out = _beir(tmp_path, "legalquad",
                corpus=[{"_id": "a", "title": "", "text": "erster"},
                        {"_id": "b", "title": "§ 1", "text": "zweiter"},
                        {"_id": "c", "title": "", "text": "dritter"}],
                queries=[{"_id": "q1", "text": "frage eins"},
                         {"_id": "q2", "text": "ohne urteil"},
                         {"_id": "q3", "text": "frage drei"}],
                qrels=[{"query-id": "q1", "corpus-id": "b", "score": 1},
                       {"query-id": "q3", "corpus-id": "c", "score": 1},
                       {"query-id": "q3", "corpus-id": "a", "score": 2},
                       {"query-id": "q2", "corpus-id": "a", "score": 0}],
                parquet=False)
    corpus, queries = load(out, max_chars=1000)
    assert corpus.ids == [0, 1, 2]
    assert corpus.texts == ["erster", "§ 1 zweiter", "dritter"], "title + ' ' + text, as MTEB"
    assert queries.texts == ["frage eins", "frage drei"], "a query judged only at 0 does not count"
    assert queries.relevant == [[1], [0, 2]]


def test_mteb_parquet_says_id_where_beir_says__id(tmp_path) -> None:
    from tools.encoder_bakeoff import load
    out = _beir(tmp_path, "bsard",
                corpus=[{"id": "1", "title": "Art. 1", "text": "texte"}],
                queries=[{"id": "9", "text": "question"}],
                qrels=[{"query-id": "9", "corpus-id": "1", "score": 1}], parquet=True)
    corpus, queries = load(out, max_chars=1000)
    assert corpus.texts == ["Art. 1 texte"] and queries.relevant == [[0]]


def test_a_judged_document_missing_from_the_corpus_is_refused(tmp_path) -> None:
    from tools.encoder_bakeoff import load
    out = _beir(tmp_path, "legalquad",
                corpus=[{"_id": "a", "title": "", "text": "x"}],
                queries=[{"_id": "q", "text": "y"}],
                qrels=[{"query-id": "q", "corpus-id": "zz", "score": 1}], parquet=False)
    with pytest.raises(SystemExit, match="not in the corpus"):
        load(out, max_chars=1000)


def test_one_directory_holds_one_set(tmp_path) -> None:
    from tools.encoder_bakeoff import fetch
    out = tmp_path / "set"
    out.mkdir()
    (out / "SOURCE.json").write_text(json.dumps({"suite": "bsard"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="one directory, one set"):
        fetch(out, "legalquad")
    with pytest.raises(SystemExit, match="unknown suite"):
        fetch(out, "nope")


def test_a_directory_from_before_suites_is_jurifindit(tmp_path) -> None:
    from tools.encoder_bakeoff import suite_of
    (tmp_path / "SOURCE.json").write_text(json.dumps({"dataset": "jurifindit/JuriFindIT"}),
                                          encoding="utf-8")
    assert suite_of(tmp_path) == "jurifindit"


def test_the_published_row_is_jurifindits_alone(tmp_path, capsys) -> None:
    from tools.encoder_bakeoff import report
    out = _beir(tmp_path, "legalquad", corpus=[{"_id": "a", "title": "", "text": "x"}],
                queries=[{"_id": "q", "text": "y"}],
                qrels=[{"query-id": "q", "corpus-id": "a", "score": 1}], parquet=False)
    run = out / "m"
    run.mkdir()
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}), encoding="utf-8")
    (run / "scores-bare.json").write_text(json.dumps({"ndcg@10": 0.5, "mrr@10": 0.25}),
                                          encoding="utf-8")
    report(out)
    shown = capsys.readouterr().out
    assert "DAR-legal-it" not in shown and "0.5000" in shown and "0.2500" in shown
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "SOURCE.json").write_text(json.dumps({"suite": "bsard"}), encoding="utf-8")
    assert report(empty) == 0


def test_a_cut_download_leaves_nothing_a_rerun_would_trust(tmp_path, monkeypatch) -> None:
    import httpx
    from tools import encoder_bakeoff as b

    class Cut:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b'{"_id": "a"'
            raise httpx.ReadError("cut")
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: Cut())
    target = tmp_path / "corpus.jsonl"
    with pytest.raises(httpx.ReadError):
        b._download("https://example.invalid/x", target)
    assert not target.exists() and not list(tmp_path.iterdir())


# ---- the hybrid: BM25 as Louise serves it, fused as a search fuses ------

def test_fuse_is_reciprocal_rank_with_k_60() -> None:
    from tools.encoder_bakeoff import fuse
    # 1: 1/61 + 1/62 = 0.032522; 3: 1/63 + 1/61 = 0.032266;
    # 2: 1/62 = 0.016129; 4: 1/63 = 0.015873
    assert fuse([1, 2, 3], [3, 1, 4]) == [1, 3, 2, 4]


def test_fuse_breaks_ties_by_record_bytes_as_the_search_does() -> None:
    from tools.encoder_bakeoff import fuse
    assert fuse([5], [6]) == [5, 6]
    # "bakeoff:10" < "bakeoff:9": the uid's bytes, not the number
    assert fuse([9], [10]) == [10, 9]


def test_fuse_takes_each_leg_to_depth() -> None:
    """A record past a leg's depth gets nothing from that leg, as in the
    search: 3 is the vector leg's first and the text leg's third."""
    from tools.encoder_bakeoff import fuse
    # depth 3: 3 = 1/63 + 1/61 first; then 1 (1/61); 2 and 5 tie at 1/62
    assert fuse([1, 2, 3], [3, 5, 6], depth=3) == [3, 1, 2]
    # depth 2: the text leg stops at 2, so 3 has 1/61 alone and ties with 1
    assert fuse([1, 2, 3], [3, 5, 6], depth=2) == [1, 3]


def test_lexical_ranks_with_the_served_word_index_and_score_fuses(tmp_path) -> None:
    """End to end on a small German set: the index app/fts.py builds, its
    stemmer (Kaution ~ Kautionen), the hybrid row beside the encoder's."""
    from tools import encoder_bakeoff as bake
    out = _beir(tmp_path, "legalquad",
                corpus=[{"_id": "a", "title": "", "text": "Die Rückzahlung der Kautionen nach dem Mietvertrag"},
                        {"_id": "b", "title": "", "text": "Die Kündigung des Arbeitsvertrags durch den Arbeitgeber"},
                        {"_id": "c", "title": "", "text": "Der Schadensersatz nach einem Verkehrsunfall"}],
                queries=[{"_id": "q1", "text": "Wann bekomme ich meine Kaution zurück?"},
                         {"_id": "q2", "text": "fristlose Kündigung Arbeitgeber"}],
                qrels=[{"query-id": "q1", "corpus-id": "a", "score": 1},
                       {"query-id": "q2", "corpus-id": "b", "score": 1}], parquet=False)
    assert bake.lexical(out, "de", 20_000) == 0
    rankings = json.loads((out / "bm25" / "ranking-bare.json").read_text())
    assert rankings[0][0] == 0 and rankings[1][0] == 1, "positions of a and b"
    meta = json.loads((out / "bm25" / "run-bare.json").read_text())
    assert meta["group"] == "de" and meta["empty"] == 0
    # an encoder that ranks every query's relevant document last
    run = out / "m"
    run.mkdir()
    np.save(run / "corpus.npy", np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32))
    np.save(run / "queries-bare.npy", np.array([[0, 0.2, 1], [0.2, 0, 1]], dtype=np.float32))
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}))
    assert bake.score(out, 20_000) == 0
    bm25 = json.loads((out / "bm25" / "scores-bare.json").read_text())
    dense = json.loads((run / "scores-bare.json").read_text())
    fused = json.loads((run / "fused-bare.json").read_text())
    assert bm25["mrr@10"] == pytest.approx(1.0)
    assert dense["mrr@10"] < fused["mrr@10"] <= bm25["mrr@10"]


def test_a_bm25_run_of_other_settings_is_refused(tmp_path) -> None:
    from tools import encoder_bakeoff as bake
    out = _beir(tmp_path, "legalquad", corpus=[{"_id": "a", "title": "", "text": "Kaution"}],
                queries=[{"_id": "q", "text": "Kaution"}],
                qrels=[{"query-id": "q", "corpus-id": "a", "score": 1}], parquet=False)
    bake.lexical(out, "de", 20_000)
    with pytest.raises(SystemExit, match="run `lexical` again"):
        bake.score(out, 10_000)


def test_the_report_shows_bm25_the_encoder_and_the_two_fused(tmp_path, capsys) -> None:
    from tools.encoder_bakeoff import report
    out = _beir(tmp_path, "legalquad", corpus=[{"_id": "a", "title": "", "text": "x"}],
                queries=[{"_id": "q", "text": "y"}],
                qrels=[{"query-id": "q", "corpus-id": "a", "score": 1}], parquet=False)
    for name, files in (("bm25", {"scores-bare.json": {"mrr@10": 0.6}}),
                        ("m", {"scores-bare.json": {"mrr@10": 0.4}, "fused-bare.json": {"mrr@10": 0.7}})):
        d = out / name
        d.mkdir()
        (d / "run-bare.json").write_text(json.dumps({"name": "BM25 (Louise)" if name == "bm25" else "m",
                                                     "slug": "bare"}))
        for f, v in files.items():
            (d / f).write_text(json.dumps(v))
    report(out)
    shown = capsys.readouterr().out
    assert "BM25 (Louise)" in shown and "m + BM25 (RRF)" in shown and "0.7000" in shown


# ---- sets made BEIR at fetch (EUR-Lex-Sum, LEMUR) ------------------------

def _serve(monkeypatch, files: dict[str, list[dict]]):
    """`_download` answered from memory: the URL's tail names the rows."""
    from tools import encoder_bakeoff as b

    def download(url, target):
        rows = next(v for k, v in files.items() if url.endswith(k))
        target.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(b, "_download", download)


def test_eurlex_sum_queries_test_summaries_against_every_splits_acts(tmp_path, monkeypatch) -> None:
    from tools import encoder_bakeoff as b
    _serve(monkeypatch, {
        "data/german/train.json": [{"celex_id": "32001L0001", "reference": "Richtlinie eins", "summary": "s1"}],
        "data/german/validation.json": [{"celex_id": "32002R0002", "reference": "Verordnung zwei", "summary": "s2"}],
        "data/german/test.json": [{"celex_id": "32003L0003", "reference": "Richtlinie drei", "summary": "Worum geht es? Drei."},
                                  {"celex_id": "32004L0004", "reference": "Richtlinie vier", "summary": ""}]})
    out = tmp_path / "eurlex-sum-de"
    assert b.fetch(out, "eurlex-sum-de") == 0
    corpus, queries = b.load(out, 1000)
    assert corpus.texts == ["Richtlinie eins", "Verordnung zwei", "Richtlinie drei", "Richtlinie vier"]
    assert queries.texts == ["Worum geht es? Drei."], "a test act with no summary asks nothing"
    assert queries.relevant == [[2]]
    source = json.loads((out / "SOURCE.json").read_text())
    assert source["suite"] == "eurlex-sum-de" and "NC-SA" in source["licence"]
    assert {"raw/test.json", "corpus.jsonl", "qrels.jsonl"} <= set(source["sha256"])


def test_lemur_takes_each_acts_first_row_and_its_metadata(tmp_path, monkeypatch, capsys) -> None:
    from tools import encoder_bakeoff as b
    _serve(monkeypatch, {"languages/it/test.jsonl": [
        {"law_id": "32007D0364", "metadata": "DECISIONE DELLA COMMISSIONE del 23 maggio 2007", "text": "Il Consiglio..."},
        {"law_id": "32007D0364", "metadata": "DECISIONE DELLA COMMISSIONE del 23 maggio 2007", "text": "pagina due"},
        {"law_id": "31991L0271", "metadata": "DIRETTIVA DEL CONSIGLIO del 21 maggio 1991", "text": "acque reflue"}]})
    out = tmp_path / "lemur-it"
    b.fetch(out, "lemur-it")
    corpus, queries = b.load(out, 1000)
    assert corpus.texts == ["Il Consiglio...", "acque reflue"]
    assert queries.texts[1] == "DIRETTIVA DEL CONSIGLIO del 21 maggio 1991" and queries.relevant == [[0], [1]]
    assert "rows per act up to 2" in capsys.readouterr().out


def test_every_eu_language_has_its_suites() -> None:
    from tools.encoder_bakeoff import SUITES
    for code in ("en", "fr", "de", "it", "pl", "mt"):
        assert f"eurlex-sum-{code}" in SUITES and f"lemur-{code}" in SUITES


# ---- VeGa endpoints, and runs that may not mix settings (2026-10-29) ------

def test_a_vega_model_is_asked_through_the_tagging_client() -> None:
    """Every GPU encoder on the node is `vega-…`, and VeGa refuses an
    untagged input: the first LegalQuAD runs died at the first request."""
    from tools.encoder_bakeoff import api_for
    assert api_for("vega-bge-m3") == "vega"
    assert api_for("vega-qwen3-embedding-4b") == "vega"
    assert api_for("xcheck-bge-m3-cpu") == "openai"
    assert api_for("vega-bge-m3", "openai") == "openai", "--api overrides"


def test_a_vega_run_reaches_the_app_client_as_vega(tmp_path, monkeypatch) -> None:
    from tools import encoder_bakeoff as bake
    seen = []
    corpus = bake.Corpus(ids=[1, 2], texts=["a", "b"])
    queries = bake.Queries(texts=["q"], relevant=[[1]])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))
    monkeypatch.setattr("app.config.VEGA_URL", "http://vega")
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: seen.append(recipe.api) or [[1.0, 0.0] for _ in texts])
    assert bake.embed(tmp_path, "m", "http://x", "vega-bge-m3", None, 8192, 1, 20_000) == 0
    assert set(seen) == {"vega"}
    assert json.loads((tmp_path / "m" / "run-bare.json").read_text())["api"] == "vega"


def test_a_vega_model_without_receipts_is_refused_before_anything_is_sent(tmp_path, monkeypatch) -> None:
    from tools import encoder_bakeoff as bake
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (bake.Corpus([1], ["a"]), bake.Queries(["q"], [[1]])))
    monkeypatch.setattr("app.config.VEGA_URL", "")
    with pytest.raises(SystemExit, match="VEGA_URL is unset"):
        bake.embed(tmp_path, "m", "http://x", "vega-bge-m3", None, 8192, 1, 20_000)


def test_one_run_name_never_mixes_two_encodings(tmp_path, monkeypatch) -> None:
    """The corpus shards are shared by every query variant of a run: another
    model, URL or a smoke run's short shard under the same --name would be
    one row from two encodings."""
    from tools import encoder_bakeoff as bake
    corpus = bake.Corpus(ids=[1, 2], texts=["a", "b"])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, bake.Queries(["q"], [[1]])))
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: [[1.0, 0.0] for _ in texts])
    bake.embed(tmp_path, "m", "http://x", "model-a", None, 8192, 1, 20_000, limit=1)
    with pytest.raises(SystemExit, match="different settings.*limit"):
        bake.embed(tmp_path, "m", "http://x", "model-a", None, 8192, 1, 20_000)
    with pytest.raises(SystemExit, match="different settings.*model"):
        bake.embed(tmp_path, "m", "http://x", "model-b", None, 8192, 1, 20_000, limit=1)
    assert bake.embed(tmp_path, "m", "http://x", "model-a", None, 8192, 1, 20_000,
                      query_prefix="Q: ", limit=1) == 0, "a query variant is the same run"


def test_a_green_set_asks_no_acknowledgement(tmp_path, monkeypatch, capsys) -> None:
    from tools import encoder_bakeoff as b
    _serve(monkeypatch, {"corpus.jsonl": [{"_id": "a", "title": "", "text": "x"}],
                         "queries.jsonl": [{"_id": "q", "text": "y"}],
                         "qrels/test.jsonl": [{"query-id": "q", "corpus-id": "a", "score": 1}]})
    b.fetch(tmp_path / "lq", "legalquad")
    said = capsys.readouterr().out
    assert "green, no acknowledgement needed" in said and "record an acknowledgement" not in said
    assert b.SUITES["bsard"].get("green") is None and b.SUITES["lemur-en"]["green"]


# ---- the paired test (Smucker et al., CIKM 2007) --------------------------

def test_paired_on_equal_rows_finds_nothing() -> None:
    from tools.encoder_bakeoff import paired
    delta, lo, hi, p = paired([0.5, 0.2, 1.0], [0.5, 0.2, 1.0])
    assert (delta, lo, hi, p) == (0.0, 0.0, 0.0, 1.0)


def test_paired_on_two_queries_by_hand() -> None:
    """d = [1, 1]: of the four sign patterns, two give |mean| = 1 — p ≈ 1/2."""
    from tools.encoder_bakeoff import paired
    delta, lo, hi, p = paired([1.0, 1.0], [0.0, 0.0])
    assert delta == 1.0 and lo == hi == 1.0
    assert 0.47 < p < 0.53


def test_paired_on_a_clear_win_is_significant_and_seeded() -> None:
    from tools.encoder_bakeoff import paired
    a, b = [1.0] * 50, [0.0] * 50
    _, _, _, p = paired(a, b)
    assert p == pytest.approx(1 / 10_001)
    rng = np.random.default_rng(3)
    x, y = list(rng.random(200)), list(rng.random(200))
    assert paired(x, y) == paired(x, y), "the same scores give the same report"


def test_per_query_is_the_metric_of_each_query() -> None:
    from tools.encoder_bakeoff import Queries, per_query
    got = per_query([[1, 2], [3, 1]], Queries(texts=["a", "b"], relevant=[[1], [1]]))
    assert got["mrr@10"] == [1.0, 0.5]


def test_the_report_tests_each_row_against_bm25(tmp_path, capsys) -> None:
    from tools.encoder_bakeoff import report
    out = _beir(tmp_path, "legalquad", corpus=[{"_id": "a", "title": "", "text": "x"}],
                queries=[{"_id": "q", "text": "y"}],
                qrels=[{"query-id": "q", "corpus-id": "a", "score": 1}], parquet=False)
    rows = {"bm25": ("BM25 (Louise)", [1.0] * 40), "m": ("m", [0.0] * 40)}
    for d, (name, per) in rows.items():
        run = out / d
        run.mkdir()
        (run / "run-bare.json").write_text(json.dumps({"name": name, "slug": "bare"}))
        (run / "scores-bare.json").write_text(json.dumps({"mrr@10": float(np.mean(per))}))
        (run / "perquery-bare.json").write_text(json.dumps({"mrr@10": per, "ndcg@10": per}))
    report(out)
    shown = capsys.readouterr().out
    assert "Paired, mrr@10, 40 queries" in shown
    assert "m  vs  BM25 (Louise): -1.0000" in shown and "p = 0.0001" in shown


def test_a_smoke_run_cuts_the_questions_as_well(tmp_path, monkeypatch) -> None:
    from tools import encoder_bakeoff as bake
    sent: list[int] = []
    corpus = bake.Corpus(ids=list(range(10)), texts=[f"d{i}" for i in range(10)])
    queries = bake.Queries(texts=[f"q{i}" for i in range(10)], relevant=[[i] for i in range(10)])
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (corpus, queries))
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: sent.append(len(texts)) or [[1.0, 0.0] for _ in texts])
    bake.embed(tmp_path, "smoke", "http://x", "m", None, 8192, 1, 20_000, limit=3)
    assert sent == [3, 3], "three documents and three questions"


# ---- the served query side (Normattiva's "query: ", München 2026-10-29) ---

def test_from_collection_embeds_queries_as_the_collection_serves_them(tmp_path, monkeypatch, capsys) -> None:
    """The collection's own recipe goes to the app client, which prefixes the
    space's instruction to every query as a search does; the tool adds
    nothing, and the slug names the instruction."""
    from app import collections as collections_mod
    from tools import encoder_bakeoff as bake
    served = collections_mod.EmbedRecipe(url="http://x", api="openai", model="dar", dtype="f16",
                                         ctx=8192, dim=None, query_instruction="query: ")
    monkeypatch.setattr(bake, "served_recipe", lambda name: served)
    monkeypatch.setattr(bake, "load", lambda out, max_chars: (bake.Corpus([1], ["doc"]),
                                                              bake.Queries(["frage"], [[1]])))
    calls = []
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: calls.append((recipe.query_instruction, list(texts),
                                                                  kw.get("query", False)))
                        or [[1.0, 0.0] for _ in texts])
    assert bake.embed(tmp_path, "dar", None, None, None, 8192, 1, 20_000,
                      from_collection="normattiva") == 0
    assert calls[1] == ("query: ", ["frage"], True), "the app prefixes; the text goes bare"
    meta = json.loads(next((tmp_path / "dar").glob("run-*.json")).read_text())
    assert meta["slug"] == bake.slug_of("query: ") and meta["from_collection"] == "normattiva"
    with pytest.raises(SystemExit, match="would add a second"):
        bake.embed(tmp_path, "dar2", None, None, None, 8192, 1, 20_000,
                   query_prefix="query: ", from_collection="normattiva")


def test_the_report_no_longer_says_bare_is_what_the_node_serves(tmp_path, capsys) -> None:
    from tools.encoder_bakeoff import report
    out = _beir(tmp_path, "legalquad", corpus=[{"_id": "a", "title": "", "text": "x"}],
                queries=[{"_id": "q", "text": "y"}],
                qrels=[{"query-id": "q", "corpus-id": "a", "score": 1}], parquet=False)
    run = out / "m"
    run.mkdir()
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}))
    (run / "scores-bare.json").write_text(json.dumps({"mrr@10": 0.5}))
    report(out)
    shown = capsys.readouterr().out
    assert "which is what this node serves today" not in shown
    assert "--from-collection" in shown
