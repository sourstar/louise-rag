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


def test_budgeted_encoding_shrinks_the_batch_for_long_texts() -> None:
    """A fixed batch size is wrong in both directions: it wastes the card on
    short articles and asks for 48 GiB on long ones. Every pass should cost
    about the same in memory instead."""
    from tools.bakeoff_local import CHARS_PER_TOKEN, encode_budgeted

    short = ["x" * 320] * 8          # ~100 tokens each
    long_one = "y" * 32000           # ~10000 tokens, clamped to max_seq
    model = _FakeST()
    out = encode_budgeted(model, short + [long_one], max_seq=8192,
                          max_batch=64, budget=4.0e7)

    assert out.shape == (9, 2)
    # longest first, and it travels alone: 8192^2 already exceeds the budget
    assert model.batches[0] == [long_one]
    # the short ones fit together under the same budget
    seq = int(320 / CHARS_PER_TOKEN)
    assert len(model.batches[1]) == min(8, int(4.0e7 / (seq * seq)))
    for batch in model.batches:
        longest = max(int(len(t) / CHARS_PER_TOKEN) for t in batch)
        assert len(batch) * min(longest, 8192) ** 2 <= 4.0e7 or len(batch) == 1


def test_budgeted_encoding_preserves_input_order() -> None:
    """It encodes longest-first but must return vectors in the caller's
    order — a silent permutation here would misalign every vector with its
    document id and the scores would still look plausible."""
    from tools.bakeoff_local import encode_budgeted

    texts = ["a" * 100, "b" * 5000, "c" * 10, "d" * 900]
    out = encode_budgeted(_FakeST(), texts, max_seq=8192, max_batch=4,
                          budget=4.0e7)
    assert [int(v[0]) for v in out] == [len(t) for t in texts]


def test_resuming_with_changed_settings_is_refused(tmp_path, monkeypatch) -> None:
    """Shards are only reusable while the settings that decide a vector are
    unchanged. Re-embedding at a different max_seq would keep the old shards
    and write the rest differently — one row from two encodings, which is
    plausible, internally consistent and wrong."""
    from tools import bakeoff_local as local

    corpus = Corpus(ids=[1, 2], texts=["a", "b"])
    queries = Queries(texts=["q"], relevant=[[1]])
    monkeypatch.setattr(local, "load", lambda out, max_chars: (corpus, queries))

    class _Model:
        max_seq_length = 8192

        def encode(self, texts, **kw):
            return np.array([[1.0, 0.0] for _ in texts], dtype=np.float32)

    model = _Model()
    # sentence_transformers is deliberately absent from the dev image
    # (invariant 7 keeps torch out of it), so stand the module up rather
    # than patch an attribute on something that cannot be imported.
    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = lambda *a, **k: model
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)

    argv = ["--out", str(tmp_path), "--name", "m", "--model", "fake"]
    assert local.main(argv) == 0

    model.max_seq_length = 2048  # the same command, a different encoding
    with pytest.raises(SystemExit, match="different settings"):
        local.main(argv)


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
