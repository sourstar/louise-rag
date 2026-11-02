"""tools/measure/wikidata_encoder_ab.py — jina-v3 against bge-m3 for
wikidata's space, a known-item test (operator, 2026-10-31)."""
import json

import numpy as np
import pytest

from tools.measure import wikidata_encoder_ab as ab


def test_a_question_that_names_the_entity_is_left_out():
    assert ab.leaks("paintings by Caravaggio in Rome", "Caravaggio")
    assert ab.leaks("Michelangelo Merisi's last work", "Michelangelo Merisi da Caravaggio")
    assert not ab.leaks("Baroque painter who killed a man in Rome", "Caravaggio")
    assert not ab.leaks("a river in Italy", "Po"), "a word of three letters or fewer is not a name match"


def test_only_current_parts_of_the_family_are_read(tmp_path):
    d = tmp_path / "records" / "family=person" / "range=0"
    d.mkdir(parents=True)
    for n in ("00001.jsonl.zst", "00002.parquet", "00001.abcd1234.jsonl.zst", "notes.txt"):
        (d / n).write_text("x")
    assert [p.name for p in ab.part_files(tmp_path, "person")] == ["00001.jsonl.zst", "00002.parquet"]


def _shard(tmp_path, families=("person", "work"), per_part=30):
    for f in families:
        d = tmp_path / "records" / f"family={f}" / "range=0"
        d.mkdir(parents=True, exist_ok=True)
        (d / "00000.jsonl.zst").write_text(f)
    def iter_records(path):
        fam = path.read_text()
        return [{"uid": f"wikidata:{fam}{i}", "title": f"{fam} {i}"} for i in range(per_part)]
    def cut(recs):
        return {f"{r['uid']}:{k}:1": {"text": f"{r['title']} chunk {k}", "seq": k}
                for r in recs for k in range(2)}
    return iter_records, cut


def test_the_sample_takes_each_family_evenly_with_its_chunks_in_order(tmp_path):
    iter_records, cut = _shard(tmp_path)
    out = tmp_path / "ab"
    got = ab.sample(out, entities=20, families=("person", "work"), seed=0,
                    iter_records=iter_records, cut=cut, shard=tmp_path)
    assert got == {"entities": 20, "chunks": 40}
    rows = [json.loads(x) for x in (out / "corpus.jsonl").read_text().splitlines()]
    assert sum(r["family"] == "person" for r in rows) == 10
    assert rows[0]["chunks"][0].endswith("chunk 0") and rows[0]["chunks"][1].endswith("chunk 1")


def test_questions_alternate_languages_and_resume(tmp_path):
    iter_records, cut = _shard(tmp_path)
    out = tmp_path / "ab"
    ab.sample(out, entities=20, families=("person", "work"), seed=0,
              iter_records=iter_records, cut=cut, shard=tmp_path)
    asked = []
    def chat(prompt):
        asked.append(prompt)
        return "a thing someone wrote" if len(asked) % 5 else "person 3 and work 4"   # some leak
    got = ab.queries(out, n=6, seed=0, chat=chat)
    qs = [json.loads(x) for x in (out / "queries.jsonl").read_text().splitlines()]
    assert len(qs) == 6 and {q["lang"] for q in qs} == {"en", "it"}
    assert ab.queries(out, n=6, seed=0, chat=chat)["queries"] == 6, "resumed: nothing asked again"


def test_embedding_is_resumable_by_block(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "BLOCK", 4)
    out = tmp_path
    (out / "corpus.jsonl").write_text("".join(json.dumps({"uid": f"u{i}", "family": "person", "label": "",
                                                          "chunks": ["a", "b"]}) + "\n" for i in range(5)))
    (out / "queries.jsonl").write_text(json.dumps({"uid": "u0", "family": "person", "lang": "en",
                                                   "query": "q"}) + "\n")
    calls = []
    def fake(texts, query):
        calls.append((len(texts), query))
        return [[1.0, 0.0]] * len(texts)
    assert ab.embed(out, "bge", encode=fake)["blocks_written"] == 3
    calls.clear()
    assert ab.embed(out, "bge", encode=fake)["blocks_written"] == 0
    assert calls == [], "every block and the queries are there already"


def test_the_score_finds_the_better_encoder_and_says_the_verdict(tmp_path):
    rng = np.random.default_rng(0)
    n, dim = 200, 16
    corpus = [{"uid": f"u{i}", "family": "person" if i % 2 else "work", "label": "", "chunks": ["c"]}
              for i in range(n)]
    (tmp_path / "corpus.jsonl").write_text("".join(json.dumps(e) + "\n" for e in corpus))
    qs = [{"uid": f"u{i}", "family": corpus[i]["family"], "lang": "en" if i % 2 else "it", "query": "q"}
          for i in range(n // 2)]
    (tmp_path / "queries.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))
    docs = rng.normal(size=(n, dim)).astype("float32")
    targets = docs[[int(q["uid"][1:]) for q in qs]]
    np.save(tmp_path / "vec-bge-00000.npy", docs)
    np.save(tmp_path / "qvec-bge.npy", targets + 0.05 * rng.normal(size=targets.shape))
    np.save(tmp_path / "vec-jina-00000.npy", docs)
    np.save(tmp_path / "qvec-jina.npy", targets + 1.5 * rng.normal(size=targets.shape))
    got = ab.score(tmp_path)
    assert got["mrr10"]["bge"] > 0.95 > got["mrr10"]["jina"]
    assert got["bge_minus_jina"]["p"] < 0.05
    assert got["verdict"].startswith("switch to bge-m3")
    assert set(got["by_language"]) == {"en", "it"} and set(got["by_family"]) == {"person", "work"}
    assert json.loads(json.dumps(got)) == got, "what main() prints: Python numbers only"
    # and the other way round, the verdict keeps jina-v3
    np.save(tmp_path / "qvec-bge.npy", targets + 1.5 * rng.normal(size=targets.shape))
    np.save(tmp_path / "qvec-jina.npy", targets + 0.05 * rng.normal(size=targets.shape))
    assert ab.score(tmp_path)["verdict"].startswith("keep jina-v3")


def test_a_mismatched_vector_count_is_refused(tmp_path):
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"uid": "u0", "family": "person", "label": "",
                                                       "chunks": ["a", "b"]}) + "\n")
    (tmp_path / "queries.jsonl").write_text(json.dumps({"uid": "u0", "family": "person", "lang": "en",
                                                        "query": "q"}) + "\n")
    np.save(tmp_path / "vec-bge-00000.npy", np.ones((1, 4), dtype="float32"))
    np.save(tmp_path / "qvec-bge.npy", np.ones((1, 4), dtype="float32"))
    with pytest.raises(SystemExit, match="embed again"):
        ab.score(tmp_path)


# ---- jina-v3 on llama.cpp (operator, 2026-10-31) ----------------------------

def test_the_gguf_side_puts_the_task_instruction_first_and_cuts_to_512(tmp_path):
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"uid": "u0", "family": "person", "label": "",
                                                       "chunks": ["Rome"]}) + "\n")
    (tmp_path / "queries.jsonl").write_text(json.dumps({"uid": "u0", "family": "person", "lang": "en",
                                                        "query": "a city"}) + "\n")
    seen = []

    def fake(texts, query):
        seen.append((query, texts))
        return [[1.0] * 1024 for _ in texts]
    got = ab.embed(tmp_path, "jina-gguf", encode=fake)
    assert seen[0] == (False, ["Represent the document for retrieval: Rome"])
    assert seen[1] == (True, ["Represent the query for retrieving evidence documents: a city"])
    v = np.load(tmp_path / "vec-jina-gguf-00000.npy")
    assert v.shape == (1, 512) and abs(float(np.linalg.norm(v[0])) - 1.0) < 1e-6
    assert got["chunks_per_s"] is not None and (tmp_path / "time-jina-gguf.json").exists()


def test_bge_as_good_keeps_jina_and_the_two_jinas_are_compared(tmp_path):
    """The operator's rule: bge-m3 only if BETTER; jina-v3's vectors are half
    the size. And jina-v3 on llama.cpp against its torch reference."""
    rng = np.random.default_rng(1)
    n, dim = 120, 16
    corpus = [{"uid": f"u{i}", "family": "person", "label": "", "chunks": ["c"]} for i in range(n)]
    (tmp_path / "corpus.jsonl").write_text("".join(json.dumps(e) + "\n" for e in corpus))
    qs = [{"uid": f"u{i}", "family": "person", "lang": "en", "query": "q"} for i in range(n // 2)]
    (tmp_path / "queries.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))
    docs = rng.normal(size=(n, dim)).astype("float32")
    targets = docs[: n // 2]
    for enc in ("jina", "jina-gguf", "bge"):
        np.save(tmp_path / f"vec-{enc}-00000.npy", docs)
        np.save(tmp_path / f"qvec-{enc}.npy", targets + 0.3 * rng.normal(size=targets.shape))
        (tmp_path / f"time-{enc}.json").write_text(json.dumps({"seconds": 10.0, "chunks": 100}))
    got = ab.score(tmp_path)
    assert set(got["mrr10"]) == {"jina", "jina-gguf", "bge"}
    assert got["jina_gguf_vs_torch"]["worst"] > 0.9999
    assert got["chunks_per_s"]["jina-gguf"] == 10.0
    assert got["bge_minus_jina"]["against"] == "jina-gguf"
    assert got["verdict"].startswith("keep jina-v3")
    json.dumps(got)


# ---- harrier and sample-table (operator, 2026-11-02) ---------------------------

def test_harrier_is_the_base_recipe_with_its_model_and_retrieval_prompt(monkeypatch):
    from app import collections as collections_mod
    base = collections_mod.EmbedRecipe(url="http://localai:8080", api="vega", model="vega-bge-m3",
                                       dtype="f16", ctx=8191, dim=1024, cut="tokens")
    monkeypatch.setattr(collections_mod, "by_name", lambda name: object())
    monkeypatch.setattr(collections_mod, "recipe_for", lambda c: base)
    r = ab._recipe("harrier")
    assert r.model == ab.HARRIER_MODEL and r.query_instruction == ab.HARRIER_QUERY
    assert r.query_instruction.startswith("Instruct: ") and r.query_instruction.endswith("\nQuery: ")
    assert (r.url, r.api, r.cut, r.ctx, r.dim, r.query_model) == (base.url, "vega", "tokens", 8191, None, None)


def test_embedding_leases_the_encoder_for_the_whole_run(tmp_path, monkeypatch):
    from app import embeddings, encoder_use
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"uid": "u0", "family": "en", "label": "",
                                                       "chunks": ["a", "b"]}) + "\n")
    (tmp_path / "queries.jsonl").write_text(json.dumps({"uid": "u0", "family": "en", "lang": "en",
                                                        "query": "q"}) + "\n")
    events = []

    class Lease:
        def __enter__(self):
            events.append("leased")

        def __exit__(self, *exc):
            events.append("released")

    monkeypatch.setattr(ab, "_recipe", lambda enc: object())
    monkeypatch.setattr(encoder_use, "held", lambda recipe, holder: Lease())
    monkeypatch.setattr(encoder_use, "holder", lambda cmd: cmd)
    monkeypatch.setattr(embeddings, "embed_calibration",
                        lambda recipe, texts, **kw: events.append(len(texts)) or [[1.0, 0.0]] * len(texts))
    ab.embed(tmp_path, "harrier")
    assert events == ["leased", 2, 1, "released"], "every request inside the lease"


def test_every_pair_of_encoders_is_compared(tmp_path):
    rng = np.random.default_rng(1)
    n, dim = 120, 16
    corpus = [{"uid": f"u{i}", "family": "en", "label": "", "chunks": ["c"]} for i in range(n)]
    (tmp_path / "corpus.jsonl").write_text("".join(json.dumps(e) + "\n" for e in corpus))
    qs = [{"uid": f"u{i}", "family": "en", "lang": "en", "query": "q"} for i in range(60)]
    (tmp_path / "queries.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))
    docs = rng.normal(size=(n, dim)).astype("float32")
    targets = docs[:60]
    for enc, noise in (("bge", 0.3), ("harrier", 0.05), ("jina", 1.5)):
        np.save(tmp_path / f"vec-{enc}-00000.npy", docs)
        np.save(tmp_path / f"qvec-{enc}.npy", targets + noise * rng.normal(size=targets.shape))
    got = ab.score(tmp_path)
    assert set(got["pairs"]) == {"bge - harrier", "bge - jina", "harrier - jina"}
    hb = got["pairs"]["bge - harrier"]
    assert hb["mrr10"] <= 0 and hb["worse"] >= hb["better"]
    assert got["pairs"]["harrier - jina"]["p"] < 0.05
    assert json.loads(json.dumps(got)) == got


def test_wikipedia_questions_use_the_encyclopedia_prompt(tmp_path):
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"uid": "wikipedia:7", "family": "en",
                                                       "label": "Colosseum",
                                                       "chunks": ["The Colosseum is an amphitheatre"]}) + "\n")
    asked = []
    ab.queries(tmp_path, n=1, seed=0, prompt="wikipedia",
               chat=lambda p: asked.append(p) or "ancient amphitheatre in Rome")
    assert "encyclopedia article" in asked[0] and "The Colosseum is" in asked[0]
    assert json.loads((tmp_path / "queries.jsonl").read_text())["query"] == "ancient amphitheatre in Rome"


def test_sample_table_takes_documents_per_language_from_the_vector_table(tmp_path):
    import lancedb
    import pyarrow as pa
    schema = pa.schema([pa.field("id", pa.string()), pa.field("book_id", pa.int64()),
                        pa.field("lang", pa.string()), pa.field("seq", pa.int64()),
                        pa.field("text", pa.string()), pa.field("vector", pa.list_(pa.float32(), 2))])
    tables = {}
    for lang, base in (("en", 100), ("it", 200)):
        rows = [{"id": f"wikipedia:{b}:{s}:1", "book_id": b, "lang": lang, "seq": s,
                 "text": f"{lang} {b} part {s}", "vector": [1.0, 0.0]}
                for b in range(base, base + 30) for s in (3, 0, 2, 1)]
        db = lancedb.connect(str(tmp_path / f"lance-{lang}"))
        tables[lang] = db.create_table("chunks", data=pa.Table.from_pylist(rows, schema=schema))
    got = ab.sample_table(tmp_path / "s", collection="wikipedia", langs=("en", "it"), docs=10,
                          max_chunks=3, seed=0, tables=tables,
                          titles=lambda books: {b: f"Title {b}" for b in books})
    assert got["by_language"] == {"en": 5, "it": 5} and got["chunks"] == 30
    rows = [json.loads(l) for l in (tmp_path / "s" / "corpus.jsonl").read_text().splitlines()]
    r = rows[0]
    b = int(r["uid"].split(":")[1])
    assert r["family"] == "en" and r["label"] == f"Title {b}"
    assert r["chunks"] == [f"en {b} part 0", f"en {b} part 1", f"en {b} part 2"], "the first chunks, in seq order"
    assert len({x["uid"] for x in rows}) == 10


# ---- rerank: the whole search on the sample (tools/measure/pipeline_eval.py)

def _rerank_set(tmp_path):
    """Four articles, en 0-1 and it 2-3; q0 (en) asks for article 1, q1 (it)
    for article 3. `bge` ranks every question 0, 2, 1, 3; `harrier` right."""
    out = tmp_path / "set"
    out.mkdir()
    words = ["ocean ships", "mountain snow", "fiume acqua", "castello torre"]
    rows = [{"uid": f"wikipedia:{i}", "family": "en" if i < 2 else "it", "label": f"Title {i}",
             "chunks": [f"{words[i]} uno", f"{words[i]} due"]} for i in range(4)]
    (out / "corpus.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    qs = [{"uid": "wikipedia:1", "family": "en", "lang": "en", "query": "mountain snow"},
          {"uid": "wikipedia:3", "family": "it", "lang": "it", "query": "castello torre"}]
    (out / "queries.jsonl").write_text("".join(json.dumps(q) + "\n" for q in qs))
    doc = np.eye(8, dtype="float32")
    np.save(out / "vec-bge-00000.npy", doc)
    np.save(out / "vec-harrier-00000.npy", doc)
    weights_a = [8, 7, 4, 3, 6, 5, 2, 1]
    np.save(out / "qvec-bge.npy", np.asarray([weights_a, weights_a], dtype="float32"))
    np.save(out / "qvec-harrier.npy", np.asarray([[1, 1, 9, 8, 1, 1, 1, 1],
                                                  [1, 1, 1, 1, 1, 1, 9, 8]], dtype="float32"))
    return out


def _plans(out, translations):
    from tools.measure import pipeline_eval as pe
    return pe.PlanStore(out / "plans.jsonl", lambda text: {
        "core": text, "translations": translations.get(text, {}), "query_lang": None,
        "only": [], "main": None, "status": "ok"})


def test_the_sample_runs_through_the_whole_search_stage_by_stage(tmp_path, monkeypatch):
    from app import collections as collections_mod
    from app import retrieval
    monkeypatch.setattr(retrieval, "_warm_reranker", lambda recipe: None)
    monkeypatch.setattr(retrieval, "fit_to_window", lambda texts, recipe: texts)
    out = _rerank_set(tmp_path)
    recipe = collections_mod.RerankRecipe(url="http://r/v1/rerank", api="jina", model="rr")
    seen = []

    def call(recipe, query, texts, timeout):
        seen.append((query, [t.split("\n")[0] for t in texts]))
        return [10.0 if query.split()[0] in t else 0.0 for t in texts]
    res = ab.rerank(out, "wikipedia", encoders=["bge", "harrier"], recipe=recipe,
                    plans=_plans(out, {"mountain snow": {"it": "neve montagna"},
                                       "castello torre": {"en": "castle tower"}}),
                    typed=_plans(out / "typed", {}), depths=[2, 4], call=call)
    rows = res["rows"]
    assert rows["bge [translated]"]["mrr@10"] == pytest.approx((1 / 3 + 1 / 4) / 2, abs=1e-4)
    assert rows["harrier [typed]"]["hit@1"] == 1.0
    assert rows["BM25 [translated]"]["hit@1"] == 1.0
    assert rows["bge + rerank@4 [translated]"]["hit@1"] == 1.0
    assert "bge + BM25 + rerank@4 [translated]  vs  BM25 + rerank@4 [translated]" in res["pairs"]
    assert "BM25 + rerank@4 [translated]  vs  BM25 + rerank@4 [typed]" in res["pairs"]
    variants = {q for q, _ in seen}
    assert {"neve montagna", "castle tower"} <= variants, "each record judged in its language"
    assert (out / "pipeline" / "pipeline.json").exists()


def test_wikidata_records_need_their_language_said(tmp_path):
    out = _rerank_set(tmp_path)
    rows = [json.loads(x) for x in (out / "corpus.jsonl").read_text().splitlines()]
    (out / "corpus.jsonl").write_text("".join(json.dumps(r | {"family": "person"}) + "\n" for r in rows))
    with pytest.raises(SystemExit, match="--doc-lang"):
        ab.rerank(out, "wikidata", encoders=["bge"], recipe=object(), plans=None, depths=[4])
