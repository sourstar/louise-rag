"""tools/legal_pilot.py — the pilot's measurement over a built legal
collection: gold articles resolved BY KEY, metrics over the gold node ids,
the reach of the misses along declared edges, and the temporal arm — all
deterministic, no model in the loop (gazzetta-plugin.md §7.2; review §10)."""
from __future__ import annotations

import base64
import json

import pytest

from app import collections as collections_mod, db, discovery
from tests.test_normattiva import BUILT, L306, M306, SARD_MEMBERS, _nid, _zip
from tests.test_validity import _run
from tools import legal_pilot as lp

LAW_M = "LEGGE_20031031_306/2003-11-15_003G0333_"
SARD_O = _nid("1948-03-09", "048C0003", "1948-03-10", "ORIGINAL")
SARD_V12 = _nid("1948-03-09", "048C0003", "2001-02-16", "CONSOLIDATED/20010216")
L306_ID = _nid("2003-11-15", "003G0333", "2025-10-02", "CONSOLIDATED/20251002")
#: two consecutive versions of a synthetic decree whose art. 8 loses a number
DEC_A = "DECRETO_20200102_3/2020-01-10_20G00003_VIGENZA_2021-01-01_V1.xml"
DEC_B = "DECRETO_20200102_3/2020-01-10_20G00003_VIGENZA_2022-01-01_V2.xml"
DEC_A_ID = _nid("2020-01-10", "20G00003", "2021-01-01", "CONSOLIDATED/20210101")
DEC_B_ID = _nid("2020-01-10", "20G00003", "2022-01-01", "CONSOLIDATED/20220101")
VERSION_A = BUILT.replace(b"<p>Fuori capo((3)).</p>", b"<p>Il termine e' di 30 giorni.</p>")
VERSION_B = BUILT.replace(b"<p>Fuori capo((3)).</p>", b"<p>Il termine e' di 60 giorni.</p>")

CORPUS = [
    {"id": 1, "file_name": "§akn§it§act§l§STATO_ITALIANO§2003-10-31§0306§ITA", "path": "chapter_1__article_5"},
    {"id": 2, "file_name": "§akn§eu§act§reg§CONSIL-EP§2016-04-27§0679§ITA", "path": "chapter_2__article_11"},
    {"id": 3, "file_name": "§akn§it§act§lcost§STATO_ITALIANO§1948-02-26§0003§ITA", "path": "title_1__article_1"},
    {"id": 4, "file_name": "§akn§it§act§l§STATO_ITALIANO§2003-10-31§0306§ITA", "path": "chapter_9__article_999"},
    {"id": 5, "file_name": "§akn§it§act§l§STATO_ITALIANO§2003-10-31§0306§ITA", "path": "/"},
]
QUESTIONS = [
    {"question": "Cosa prevede l'art. 5 della legge 306/2003?", "relevant_doc_ids": [1]},
    {"question": "Trattamento senza identificazione", "relevant_doc_ids": [2]},
    {"question": "Statuto e deleghe", "relevant_doc_ids": [1, 3]},
    {"question": "Un articolo che non c'e'", "relevant_doc_ids": [4, 5]},
]


@pytest.fixture
def built(tmp_path, isolated_config, monkeypatch):
    """A normattiva collection through rdf and fts: the Statuto Sardegna's
    versions, legge 306/2003, and the synthetic decree in two versions."""
    from app.plugins.normattiva import source as nsrc
    nsrc._zip_cache.clear()
    root = tmp_path / "normattiva"
    _zip(root / "akn/M/leggi-costituzionali.zip", dict(SARD_MEMBERS))
    _zip(root / "akn/M/codici.zip", {M306: base64.b64encode(L306)})
    _zip(root / "akn/M/decreti.zip", {DEC_A: VERSION_A, DEC_B: VERSION_B})
    c = collections_mod.Collection(
        name="normattiva", plugin="normattiva", profile=None, embed_mode_default=None,
        corpus_dir=root, manifest_path=tmp_path / "manifest.db",
        fts_dir=tmp_path / "fts", lance_dir=tmp_path / "lancedb")
    con = db.connect(c)
    discovery.sync_collection(con, c)
    _run(con, c, "rdf")
    return lp.Held(c, con)


def test_article_tokens_and_acts_are_read_from_the_datasets_names():
    assert lp.article_token("attachment_(2)__book_(3)__title_8__chapter_2__section_3__article_1167") == "1167"
    assert lp.article_token("Art. 1167.") == "1167" and lp.article_token("CODICE CIVILE-art. 906") == "906"
    assert lp.article_token("Articolo 11") == "11" and lp.article_token("Art. 5-bis.") == "5-bis"
    assert lp.article_token("article_2043bis") == "2043-bis" and lp.article_token("Allegato 1") is None
    assert lp.act_of("§akn§it§act§rd§STATO_ITALIANO§1942-03-16§0262§ITA") == ("rd", "1942-03-16", 262)
    assert lp.act_of("§akn§eu§act§reg§CONSIL-EP§2016-04-27§0679§ITA") is None, "not an Italian act"
    assert lp.act_of("garbage") is None


def test_gold_articles_resolve_by_key_and_coverage_excludes_what_is_not_held(built):
    cov = lp.coverage(built, CORPUS, QUESTIONS)
    assert (cov["questions"], cov["covered"], cov["excluded"]) == (4, 2, 2)
    gold = {int(k): v for k, v in cov["gold"].items()}
    assert gold[1]["uid"] == f"normattiva:{L306_ID}" and gold[1]["seqs"], "legge 306's art. 5, by its URN key"
    assert gold[3]["uid"] == f"normattiva:{SARD_V12}", "the Statuto's SHOWN version holds the gold article"
    assert gold[2]["why"] == "not an Italian act"
    assert gold[4]["why"] == "article not among the shown version's chunks"
    assert gold[5]["why"] == "no article in the node path"
    assert [e["index"] for e in cov["excluded_questions"]] == [1, 3]
    assert cov["articles_by_outcome"]["held"] == 2


def _hit(uid, seq, text="", rank=0):
    return {"uid": uid, "chunk_id": f"{uid}:{seq}:1", "text": text, "rank": rank}


def test_the_run_scores_the_gold_by_key_and_measures_the_reach_of_the_misses(built):
    cov = lp.coverage(built, CORPUS, QUESTIONS)
    gold = {int(k): lp.gold_from(v) for k, v in cov["gold"].items()}
    law = f"normattiva:{L306_ID}"
    art5 = gold[1].seqs[0]
    other = next(s for s in built.articles(law)[1] if s != art5)
    answers = {
        QUESTIONS[0]["question"]: {"lists": [{"hits": [_hit(law, other, rank=0), _hit(law, art5, rank=1)]}]},
        QUESTIONS[2]["question"]: {"lists": [{"hits": [_hit(law, art5, rank=0)]}]},
    }
    edges = {law: {"edges": [{"from": law, "to": "akn:it/act/legge_costituzionale/stato/1948-02-26/3",
                              "type": "eli:cites"}]}}
    got = lp.run(built, QUESTIONS, gold, k=10,
                 search_fn=lambda q, **kw: answers[q],
                 traverse_fn=lambda uid, **kw: edges.get(uid, {"edges": []}))
    s = got["summary"]
    assert s["questions_run"] == 2
    assert s["recall@1"] == pytest.approx((0.0 + 0.5) / 2) and s["recall@10"] == pytest.approx((1.0 + 0.5) / 2)
    assert s["mrr@10"] == pytest.approx((0.5 + 1.0) / 2)
    assert s["reach_misses"] == 1 and s["reach_reachable_by_one_edge"] == 1 and s["reach_share"] == 1.0, \
        "the Statuto's article was missed, and legge 306 cites the Statuto: reachable by one edge"
    rows = [r for r in got["per_question"] if "index" in r]
    assert rows[0]["ranking"][1] == 1 and rows[0]["ranking"][0] < 0, "a non-gold chunk is a synthetic id"
    assert rows[1]["missed"] == [3] and rows[1]["reachable_by_edge"] == [3]


def test_dated_question_stubs_come_from_versions_whose_article_lost_a_number(built):
    stubs = lp.temporal_make(built, limit=10)
    dec = [s for s in stubs if s["gold_uid"] == f"normattiva:{DEC_A_ID}"]
    assert len(dec) == 1, stubs
    s = dec[0]
    assert s["article"] == "8" and s["nugget"] == "30" and s["control_uid"] == f"normattiva:{DEC_B_ID}"
    assert s["as_of"] == "2021-12-31", "the last day of the earlier version"
    assert "art. 8" in s["question"] and s["review"].startswith("stub")


def test_the_temporal_run_holds_the_version_to_its_uid_and_the_nugget_to_its_text(built):
    a, b = f"normattiva:{DEC_A_ID}", f"normattiva:{DEC_B_ID}"
    seq_a = built.articles(a)[0]["8"][0]
    seq_b = built.articles(b)[0]["8"][0]
    item = {"work": built.work_of(a), "article": "8", "as_of": "2021-12-31", "gold_uid": a, "control_uid": b,
            "nugget": "30", "question": "Che cosa stabiliva l'art. 8 al 2021-12-31?"}

    def search(q, *, collections, limit, as_of):
        if as_of:
            return {"lists": [{"hits": [_hit(a, seq_a, text="Il termine e' di 30 giorni.")]}]}
        return {"lists": [{"hits": [_hit(b, seq_b, text="Il termine e' di 60 giorni.")]}]}
    got = lp.temporal_run(built, [item], k=5, search_fn=search)
    assert got["summary"] == {"n": 1, "version_right": 1, "nugget_found": 1, "control_current": 1,
                              "control_nugget_gone": 1}
    # a current-only answer to the dated question: the version is wrong and the nugget gone
    got = lp.temporal_run(built, [item], k=5,
                          search_fn=lambda q, **kw: {"lists": [{"hits": [_hit(b, seq_b, text="60 giorni")]}]})
    assert got["summary"]["version_right"] == 0 and got["summary"]["nugget_found"] == 0


def test_the_command_runs_end_to_end_on_files(built, tmp_path, monkeypatch, capsys):
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = tmp_path / "pilot"
    out.mkdir()
    pq.write_table(pa.Table.from_pylist(CORPUS), out / "corpus.parquet")
    pq.write_table(pa.Table.from_pylist(QUESTIONS), out / "questions-validation.parquet")
    monkeypatch.setattr(lp, "_open", lambda name, root: (built.collection, built.con))
    assert lp.main(["coverage", "--out", str(out), "--collection", "normattiva"]) == 0
    assert "covered 2" in capsys.readouterr().out
    law = f"normattiva:{L306_ID}"
    art5 = built.articles(law)[0]["5"][0]
    monkeypatch.setattr(lp, "default_search",
                        lambda q, **kw: {"lists": [{"hits": [_hit(law, art5, rank=0)]}]})
    assert lp.main(["run", "--out", str(out), "--collection", "normattiva", "--k", "5", "--no-reach"]) == 0
    runs = sorted((out / "runs").glob("*-run.json"))
    assert runs, "a run is written with the collection named"
    payload = json.loads(runs[-1].read_text(encoding="utf-8"))
    assert payload["collection"] == "normattiva" and payload["summary"]["questions_run"] == 2
    stubs = tmp_path / "stubs.jsonl"
    assert lp.main(["temporal-make", "--collection", "normattiva", "--out", str(stubs), "--limit", "5"]) == 0
    assert stubs.read_text(encoding="utf-8").count("\n") >= 1
