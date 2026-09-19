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


def test_score_run_is_unaffected_by_vector_magnitude() -> None:
    # Cosine, not dot product: scaling a document must not promote it.
    dim = 3
    doc = np.array([_one_hot(dim, 0), [0.0, 9.0, 0.0]], dtype=np.float32)
    corpus = Corpus(ids=[1, 2], texts=["", ""])
    qry = np.array([[0.9, 0.1, 0.0]], dtype=np.float32)
    queries = Queries(texts=["q"], relevant=[[1]])
    assert score_run(doc, qry, corpus, queries, ks=(1,))["mrr@1"] == pytest.approx(1.0)
