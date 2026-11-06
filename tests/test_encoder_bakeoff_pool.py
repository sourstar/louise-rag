"""The bake-off's pool from a held collection (tools/encoder_bakeoff.py `pool`,
`embed --stored-vectors`, `--query-like`; the operator, 2026-11-02: "Measure
only, I am wondering if the quantization to 4 bits of qwen3 is putting it at
the same level of bge at 16 bits")."""
from __future__ import annotations

import json
from types import SimpleNamespace

import lancedb
import numpy as np
import pyarrow as pa
import pytest

from tools import encoder_bakeoff as bake

DIM = 4


def _table(tmp_path, books: dict[int, int]):
    """A vector table like a collection's: `books` book -> its chunk count."""
    rows = []
    for b, n in books.items():
        for seq in range(n):
            v = np.zeros(DIM, dtype=np.float32)
            v[b % DIM] = 1.0
            v[(seq + 1) % DIM] += 0.1 * seq
            rows.append({"id": f"gutenberg:{b}:{seq}:1", "book_id": b, "lang": "en", "seq": seq,
                         "chapter": "", "text": f"book {b} chunk {seq}", "vector": v.tolist()})
    schema = pa.schema([pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                        pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                        pa.field("chapter", pa.string()), pa.field("text", pa.string()),
                        pa.field("vector", pa.list_(pa.float32(), DIM))])
    db = lancedb.connect(str(tmp_path / "lance"))
    return db.create_table("chunks", data=pa.Table.from_pylist(rows, schema=schema))


@pytest.fixture
def held(tmp_path, monkeypatch):
    """A held collection `g` whose table holds books 1, 2 (targets) and 10–29."""
    from app import collections as collections_mod, embeddings, licensing, vector_parts
    books = {1: 12, 2: 3, **{b: 5 for b in range(10, 30)}}
    table = _table(tmp_path, books)
    c = SimpleNamespace(name="g")
    monkeypatch.setattr(collections_mod, "load_all", lambda: [c])
    monkeypatch.setattr(licensing, "state_for", lambda coll, spec=None: licensing.NOT_REQUIRED)
    monkeypatch.setattr(vector_parts, "is_split", lambda coll: False)
    monkeypatch.setattr(embeddings, "table_of_book", lambda coll, b: table)
    vectors = {r["id"]: r["vector"] for r in table.to_arrow().to_pylist()}
    monkeypatch.setattr(embeddings, "vectors_by_chunk_id",
                        lambda ids, coll=None: {i: vectors[i] for i in ids if i in vectors})
    q = tmp_path / "q.tsv"
    q.write_text("WQ1\ten\ta novel about one\nWQ1\tfr\tun roman sur un\nWQ2#T\ten\tTwo\n"
                 "WQ9\ten\ta work nobody holds\n", encoding="utf-8")
    w = tmp_path / "works.json"
    w.write_text(json.dumps([{"work": "WQ1", "books": ["gutenberg:1"]},
                             {"work": "WQ2", "books": ["gutenberg:2"]}]), encoding="utf-8")
    return SimpleNamespace(tmp=tmp_path, table=table, queries=q, works=w, vectors=vectors)


def test_spread_keeps_the_head_and_an_even_spread():
    assert bake.spread(10, 5, 2) == [0, 1, 2, 4, 7]
    assert bake.spread(3, 5, 2) == [0, 1, 2], "a short book enters whole"
    assert bake.spread(100, 4, 4) == [0, 1, 2, 3]
    picks = bake.spread(1000, 40, 4)
    assert len(picks) == 40 and picks == sorted(set(picks)) and picks[-1] >= 900


def test_the_query_files_and_works_read_as_one_language_wrote_them(held):
    assert bake.read_works(held.works) == {"WQ1": [1], "WQ2": [2]}
    (held.tmp / "plain.json").write_text(json.dumps({"WQ1": ["gutenberg:1", 7]}))
    assert bake.read_works(held.tmp / "plain.json") == {"WQ1": [1, 7]}
    assert bake.read_queries([held.queries])[2] == ("WQ2#T-en", "WQ2", "Two")


def test_hit_at_k_is_any_relevant_in_the_top_k():
    m = bake.metrics_for([9, 8, 5, 7], {5, 6, 1}, (2, 3))
    assert m["hit@2"] == 0.0 and m["hit@3"] == 1.0 and m["mrr@3"] == pytest.approx(1 / 3)


def test_the_pool_is_the_targets_spread_and_seeded_random_chunks_of_other_books(held):
    out = held.tmp / "set"
    assert bake.pool(out, "g", [held.queries], held.works, per_book=5, head=2, random_n=30,
                     seed=7) == 0
    source = json.loads((out / "SOURCE.json").read_text())
    assert source["suite"] == bake.POOL and source["counts"]["target_chunks"] == 5 + 3
    assert source["counts"]["random_chunks"] == 30 and source["counts"]["queries"] == 3
    corpus = [json.loads(l) for l in (out / "corpus.jsonl").read_text().splitlines()]
    ids = [r["_id"] for r in corpus]
    book1 = [i for i in ids if i.startswith("gutenberg:1:")]
    assert book1 == [f"gutenberg:1:{s}:1" for s in bake.spread(12, 5, 2)]
    # the random draw never takes a target book's chunk
    assert all(int(i.split(":")[1]) >= 10 for i in ids[8:])
    qrels = [json.loads(l) for l in (out / "qrels.jsonl").read_text().splitlines()]
    by_q = {}
    for r in qrels:
        by_q.setdefault(r["query-id"], set()).add(r["corpus-id"])
    assert by_q["WQ1-en"] == by_q["WQ1-fr"] == set(book1)
    assert by_q["WQ2#T-en"] == {f"gutenberg:2:{s}:1" for s in range(3)}
    assert "WQ9-en" not in by_q, "a work the works file lacks is left out"
    # the text is the stored text, as it was embedded
    assert corpus[0]["text"] == "book 1 chunk 0"
    # seeded: the same pool again
    again = held.tmp / "set2"
    bake.pool(again, "g", [held.queries], held.works, per_book=5, head=2, random_n=30, seed=7)
    assert (again / "corpus.jsonl").read_text() == (out / "corpus.jsonl").read_text()
    # and the set loads like any BEIR set
    corpus_l, queries_l = bake.load(out, bake.DEFAULT_MAX_CHARS)
    assert len(corpus_l.texts) == len(ids) and len(queries_l.texts) == 3
    with pytest.raises(SystemExit, match="already holds a set"):
        bake.pool(out, "g", [held.queries], held.works)


def test_an_unacknowledged_or_split_collection_makes_no_pool(held, monkeypatch):
    from app import licensing, vector_parts
    monkeypatch.setattr(licensing, "state_for", lambda coll, spec=None: licensing.MISSING)
    with pytest.raises(SystemExit, match="invariant 9"):
        bake.pool(held.tmp / "a", "g", [held.queries], held.works)
    monkeypatch.setattr(licensing, "state_for", lambda coll, spec=None: licensing.NOT_REQUIRED)
    monkeypatch.setattr(vector_parts, "is_split", lambda coll: True)
    with pytest.raises(SystemExit, match="split"):
        bake.pool(held.tmp / "b", "g", [held.queries], held.works)


def test_a_pool_is_not_fetched():
    with pytest.raises(SystemExit, match="made from a held collection"):
        bake.fetch(__import__("pathlib").Path("/nonexistent"), bake.POOL)


def _served(instruction="Instruct: find\nQuery: "):
    from app import collections as collections_mod
    return collections_mod.EmbedRecipe(url="http://x", api="openai", model="qwen-q4", dtype="f16",
                                       ctx=8192, dim=None, query_instruction=instruction)


def test_stored_vectors_are_scored_and_only_the_queries_embedded(held, monkeypatch):
    out = held.tmp / "set"
    bake.pool(out, "g", [held.queries], held.works, per_book=5, head=2, random_n=30, seed=7)
    monkeypatch.setattr(bake, "served_recipe", lambda name: _served())
    calls = []

    def fake(recipe, texts, **kw):
        calls.append((list(texts), kw.get("query", False)))
        if kw.get("query"):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        # the check: re-embedded documents come back as stored
        by_text = {f"book {i.split(':')[1]} chunk {i.split(':')[2]}": v for i, v in held.vectors.items()}
        return [by_text[t] for t in texts]
    monkeypatch.setattr("app.embeddings.embed_calibration", fake)
    assert bake.embed(out, "q4", None, None, None, 8192, 1, 20_000, from_collection="g",
                      stored=True, check=6) == 0
    assert [len(t) for t, q in calls] == [6, 3] and [q for _, q in calls] == [False, True]
    meta = json.loads(next((out / "q4").glob("run-*.json")).read_text())
    assert meta["stored_vectors"] is True and meta["stored_check"]["worst"] >= 0.99
    doc = np.load(out / "q4" / "corpus.npy")
    ids = [json.loads(l)["_id"] for l in (out / "corpus.jsonl").read_text().splitlines()]
    assert np.allclose(doc[0], held.vectors[ids[0]])
    assert bake.score(out, bake.DEFAULT_MAX_CHARS) == 0


def test_stored_vectors_of_another_function_are_refused(held, monkeypatch):
    out = held.tmp / "set"
    bake.pool(out, "g", [held.queries], held.works, per_book=5, head=2, random_n=30, seed=7)
    monkeypatch.setattr(bake, "served_recipe", lambda name: _served())
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: [[0.0, 0.0, 0.0, 1.0] for _ in texts])
    with pytest.raises(SystemExit, match="not this encoder's"):
        bake.embed(out, "q4", None, None, None, 8192, 1, 20_000, from_collection="g",
                   stored=True, check=8)
    with pytest.raises(SystemExit, match="needs --from-collection"):
        bake.embed(out, "x", "http://x", "m", None, 8192, 1, 20_000, stored=True)


def test_query_like_asks_another_model_as_the_collection_asks(held, monkeypatch):
    out = held.tmp / "set"
    bake.pool(out, "g", [held.queries], held.works, per_book=5, head=2, random_n=30, seed=7)
    monkeypatch.setattr(bake, "served_recipe", lambda name: _served("Instruct: books\nQuery: "))
    sent = []
    monkeypatch.setattr("app.embeddings.embed_calibration",
                        lambda recipe, texts, **kw: sent.append((list(texts), kw.get("query")))
                        or [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    assert bake.embed(out, "q-f16", "http://x", "qwen-f16", None, 8192, 1, 20_000,
                      query_like="g") == 0
    queries = [t for t, q in sent if q][0]
    assert queries[0] == "Instruct: books\nQuery: a novel about one"
    meta = json.loads(next((out / "q-f16").glob("run-*.json")).read_text())
    assert meta["query_like"] == "g" and meta["query_prefix"] == "Instruct: books\nQuery: "
    with pytest.raises(SystemExit, match="ANOTHER model"):
        bake.embed(out, "x", "http://x", "m", None, 8192, 1, 20_000, query_like="g",
                   query_prefix="p")


def test_the_report_of_a_pool_shows_hits(held, capsys):
    out = held.tmp / "set"
    bake.pool(out, "g", [held.queries], held.works, per_book=5, head=2, random_n=30, seed=7)
    run = out / "m"
    run.mkdir()
    (run / "run-bare.json").write_text(json.dumps({"name": "m", "slug": "bare"}))
    (run / "scores-bare.json").write_text(json.dumps({"hit@40": 0.9, "mrr@10": 0.5}))
    capsys.readouterr()
    bake.report(out)
    shown = capsys.readouterr().out
    assert "hit@40" in shown and "recall@3" not in shown
