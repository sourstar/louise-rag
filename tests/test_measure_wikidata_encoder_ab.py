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
    assert ab.embed(out, "bge", embed_texts=fake)["blocks_written"] == 3
    calls.clear()
    assert ab.embed(out, "bge", embed_texts=fake)["blocks_written"] == 0
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
