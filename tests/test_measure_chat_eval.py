"""tools/measure/chat_eval.py: Ask's own loop per chat model, scored
deterministically (tool use, the answer's language, time)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.measure import chat_eval as ev


def _queries(tmp_path: Path) -> Path:
    p = tmp_path / "q.tsv"
    p.write_text("W1\ten\tDumas novel about the necklace\n"
                 "W1\tit\tromanzo sulla collana\n"
                 "W2\tit\tsecondo romanzo\n"
                 "W3\tel\tμυθιστόρημα\n"
                 "bad line\n", encoding="utf-8")
    return p


def test_queries_are_read_with_a_deterministic_per_language_cut(tmp_path):
    rows = ev.read_queries(_queries(tmp_path))
    assert [r["key"] for r in rows] == ["W1:en", "W1:it", "W2:it", "W3:el"]
    assert [r["key"] for r in ev.read_queries(_queries(tmp_path), per_lang=1)] == ["W1:en", "W1:it", "W3:el"]


def _fake_ask(answers):
    def ask(query, collections=None):
        a = answers[query]
        if isinstance(a, Exception):
            raise a
        return a
    return ask


def test_a_run_records_tool_use_language_and_time_and_resumes(tmp_path):
    qs = ev.read_queries(_queries(tmp_path))
    answers = {
        "Dumas novel about the necklace": {"answer": "It is The Queen's Necklace.", "rounds": 2,
                                           "exhausted": False,
                                           "turns": [{"tool": "louise_search", "error": None}]},
        "romanzo sulla collana": {"answer": "È La collana della regina.", "rounds": 3, "exhausted": True,
                                  "turns": [{"tool": "louise_search", "error": None},
                                            {"tool": "nope", "error": "no tool named 'nope'"}]},
        "secondo romanzo": RuntimeError("endpoint down"),
        "μυθιστόρημα": {"answer": "Some English answer.", "rounds": 1, "exhausted": False, "turns": []},
    }
    langs = {"It is The Queen's Necklace.": "en", "È La collana della regina.": "it",
             "": None, "Some English answer.": "en"}
    ev.run(tmp_path, "m", model="m", queries=qs[:2], ask=_fake_ask(answers), guess=langs.get)
    ev.run(tmp_path, "m", model="m", queries=qs, ask=_fake_ask(answers), guess=langs.get)
    rows = ev._rows(tmp_path, "m")
    assert [r["key"] for r in rows] == ["W1:en", "W1:it", "W2:it", "W3:el"], "each once"
    s = ev.summary(rows)
    assert (s["n"], s["failed"], s["exhausted"], s["tool_errors"]) == (4, 1, 1, 1)
    assert (s["lang_right"], s["lang_guessed"]) == (2, 3), "the Greek query answered in English"
    assert rows[2]["error"].startswith("RuntimeError")


def test_an_answer_that_paid_a_load_is_cold_and_not_timed(tmp_path, capsys):
    """Warm answers only are timed (operator, 2026-11-03): no backend before
    the question, or another pid after it, is a load the answer paid."""
    qs = ev.read_queries(_queries(tmp_path))
    pids = iter([None, 7, 7, 7, 7, 9, 9, 9])          # before/after for each of the four
    ask = lambda q, collections=None: {"answer": q, "rounds": 1, "exhausted": False, "turns": []}
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "it",
           pid=lambda: next(pids))
    assert [r["warm"] for r in ev._rows(tmp_path, "m")] == [False, True, False, True]
    s = ev.summary(ev._rows(tmp_path, "m"))
    assert s["cold"] == 2 and s["n"] == 4
    ev.score(tmp_path)
    row = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("| m | all |"))
    assert row.split("|")[9].strip() == "2", "the cold column"


def test_other_settings_under_the_same_name_are_refused(tmp_path):
    qs = ev.read_queries(_queries(tmp_path))[:1]
    ask = _fake_ask({"Dumas novel about the necklace": {"answer": "x", "turns": []}})
    ev.run(tmp_path, "m", model="a", queries=qs, ask=ask, guess=lambda t: "en")
    with pytest.raises(SystemExit, match="settings differ"):
        ev.run(tmp_path, "m", model="b", queries=qs, ask=ask, guess=lambda t: "en")


def test_score_splits_the_operators_five_from_the_others(tmp_path, capsys):
    """Operator, 2026-11-03: correct in en it de fr es is the bar."""
    qs = ev.read_queries(_queries(tmp_path))
    ask = lambda q, collections=None: {"answer": q, "rounds": 1, "exhausted": False, "turns": []}
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "it")
    ev.score(tmp_path)
    table = capsys.readouterr().out
    assert "| m | all | 4 |" in table and "| m | the operator's five | 3 |" in table
    assert "| m | the others | 1 |" in table


def test_the_language_filter_keeps_only_those_named(tmp_path):
    rows = ev.read_queries(_queries(tmp_path), langs=frozenset({"it"}))
    assert [r["key"] for r in rows] == ["W1:it", "W2:it"]


def test_the_sample_is_blind_and_its_key_kept_apart(tmp_path):
    qs = ev.read_queries(_queries(tmp_path))
    for name in ("a", "b"):
        ev.run(tmp_path, name, model=name, queries=qs,
               ask=lambda q, collections=None, n=name: {"answer": f"{n}:{q}", "turns": []},
               guess=lambda t: None)
    ev.sample(tmp_path, ["a", "b"], n=3)
    text = (tmp_path / "sample.md").read_text()
    key = (tmp_path / "sample-key.txt").read_text()
    assert "**A**" in text and "**B**" in text and "=a" not in text
    assert len(key.splitlines()) == 3 and all("A=" in l and "B=" in l for l in key.splitlines())


def test_the_language_guess_is_the_nodes_own():
    assert ev.answer_language("Il romanzo è stato scritto nel secolo scorso e parla della guerra.") == "it"


def test_a_run_resumed_under_another_tag_is_scored_apart_with_where_the_time_went(tmp_path, capsys):
    """The benchmark of the optimizations (operator, 2026-11-03): stopped,
    the improvement deployed, resumed under another tag on the same
    questions; each tag its own rows, and each answer's time split into the
    model's rounds, the tool calls and the search's own timings."""
    qs = ev.read_queries(_queries(tmp_path))[:2]

    def ask(q, collections=None, model_s=(2.0, 1.0), rerank=900.0):
        return {"answer": q, "rounds": 2, "exhausted": False, "model_s": list(model_s),
                "turns": [{"tool": "louise_search", "error": None, "seconds": 1.5,
                           "result": {"timings": {"rerank": rerank, "languages": 200.0,
                                                  "dense": 50.0, "fts": 10.0, "total": 1400.0}}},
                          {"tool": "louise_lookup", "error": None, "seconds": 0.1, "result": {}}]}
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "en", tag="opt0")
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "en", tag="opt0")
    assert len(ev._rows(tmp_path, "m")) == 2, "resumed: nothing asked twice under one tag"
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en", tag="opt1",
           ask=lambda q, collections=None: ask(q, model_s=(1.0, 0.5), rerank=300.0))
    assert len(ev._rows(tmp_path, "m")) == 2, "without --redo another tag does not ask again"
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en", tag="opt1", redo=True,
           ask=lambda q, collections=None: ask(q, model_s=(1.0, 0.5), rerank=300.0))
    rows = ev._rows(tmp_path, "m")
    assert [r["tag"] for r in rows] == ["opt0", "opt0", "opt1", "opt1"]
    assert rows[0]["model_s"] == 3.0 and rows[0]["tools_s"] == 1.6
    assert rows[0]["search_s"]["rerank"] == 0.9 and rows[0]["search_s"]["total"] == 1.4
    capsys.readouterr()
    ev.score(tmp_path)
    table = capsys.readouterr().out
    assert "| m [opt0] | all | 2 |" in table and "| m [opt1] | all | 2 |" in table
    assert "| m [opt0] | 2 |" in table and "| 3.00 | 1.60 | 1.40 | 0.90 | 0.20 | 0.05 | 0.01 |" in table
    assert "| 1.50 | 1.60 | 1.40 | 0.30 |" in table


def test_citations_are_counted_and_checked_against_what_the_tools_returned(tmp_path, capsys):
    """Operator, 2026-11-03: "precise in articulating the answer and able to
    use tools". A cited uid no tool returned is invented; an answer with no
    tool call is counted."""
    turns = [{"tool": "louise_search", "error": None, "seconds": 1.0,
              "result": {"lists": [{"hits": [{"uid": "gutenberg:2701"}, {"uid": "gutenberg:15"}]}]}}]
    assert ev.citations("Ahab [gutenberg:2701], again [gutenberg:2701], and [gutenberg:9999].",
                        turns) == (2, 1)
    assert ev.citations("No brackets here.", turns) == (0, 0)
    qs = ev.read_queries(_queries(tmp_path))[:2]
    answers = {qs[0]["query"]: {"answer": "It is [gutenberg:2701] and [gutenberg:9999].",
                                "turns": turns},
               qs[1]["query"]: {"answer": "Non lo so.", "turns": []}}
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en",
           ask=lambda q, collections=None: answers[q])
    rows = ev._rows(tmp_path, "m")
    assert [(r["cited"], r["grounded"], r["no_tool"], r["returned"]) for r in rows] == \
        [(2, 1, False, 2), (0, 0, True, 0)]
    s = ev.summary(rows)
    assert (s["no_tool"], s["cited"], s["grounded"], s["uncited"]) == (1, 2, 1, 1)
    assert s["uncited_with_results"] == 0, "the uncited answer had nothing to cite"
    capsys.readouterr()
    ev.score(tmp_path)
    row = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("| m | all |"))
    assert row.split("|")[11:16] == [" 1 ", " 1.0 ", " 0.50 ", " 1 ", " 0 "]


def test_an_answer_that_cites_nothing_though_results_came_back_is_counted():
    turns = [{"tool": "louise_search", "result": {"lists": [{"hits": [{"uid": "gutenberg:1"}]}],
                                                  "facts": [{"uid": "hgnc:1101"}]}}]
    assert ev.returned_uids([t["result"] for t in turns]) == {"gutenberg:1", "hgnc:1101"}
    rows = [{"cited": 0, "returned": 2, "lang": "en", "tools": ["louise_search"], "error": None,
             "exhausted": False, "tool_errors": 0, "answer_lang": "en", "seconds": 1.0}]
    assert ev.summary(rows)["uncited_with_results"] == 1


def test_answers_whose_results_were_slimmed_are_counted(tmp_path, capsys):
    """#282: the loop slims older results to fit the context; how often a
    model searches past the budget is a tool-use signal of its own."""
    qs = ev.read_queries(_queries(tmp_path))[:2]
    got = {qs[0]["query"]: {"answer": "a", "turns": [], "slimmed": 3},
           qs[1]["query"]: {"answer": "b", "turns": []}}
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en",
           ask=lambda q, collections=None: got[q])
    assert [r["slimmed"] for r in ev._rows(tmp_path, "m")] == [3, 0]
    assert ev.summary(ev._rows(tmp_path, "m"))["slimmed"] == 1
    capsys.readouterr()
    ev.score(tmp_path)
    row = next(l for l in capsys.readouterr().out.splitlines() if l.startswith("| m | all |"))
    assert row.split("|")[16] == " 1 "


def test_the_operators_three_conveys_summarises_cites(tmp_path, capsys):
    """Operator, 2026-11-03: "how effectively the model conveys the query to
    Louise, how effectively it summarizes the results, and the amount of
    citations from the results". The work is the question's label."""
    works = tmp_path / "works.json"
    works.write_text(json.dumps([{"work": "W1", "books": ["gutenberg:10", "gutenberg:11"], "original": "fr"},
                                 {"work": "W2", "books": ["gutenberg:20"], "original": "it"}]))
    qs = ev.read_queries(_queries(tmp_path))[:3]       # W1:en, W1:it, W2:it

    def search(*uids):
        return {"tool": "louise_search", "error": None, "seconds": 1.0,
                "result": {"lists": [{"hits": [{"uid": u} for u in uids]}]}}
    got = {
        # found at rank 2 by its second search, and cited
        qs[0]["query"]: {"answer": "The Queen's Necklace [gutenberg:11].",
                         "turns": [search("gutenberg:99"), search("gutenberg:98", "gutenberg:11")]},
        # found at rank 1, but the answer cites another record
        qs[1]["query"]: {"answer": "Forse [gutenberg:99].", "turns": [search("gutenberg:10", "gutenberg:99")]},
        # never found
        qs[2]["query"]: {"answer": "Non so.", "turns": [search("gutenberg:5")]},
    }
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en",
           ask=lambda q, collections=None: got[q])
    rows = ev._rows(tmp_path, "m")
    assert rows[0]["hits"] == [["gutenberg:99"], ["gutenberg:98", "gutenberg:11"]]
    assert rows[0]["cited_uids"] == ["gutenberg:11"]
    c = ev.conveyed(rows, ev.load_works(works))
    assert (c["n"], c["found"], c["cited_right"], c["left_out"]) == (3, 2, 1, 1)
    assert c["mrr"] == pytest.approx((1 / 2 + 1) / 3)
    capsys.readouterr()
    ev.score(tmp_path, works)
    table = capsys.readouterr().out
    assert "| m | all | 3 | 0.00 | **0.67** | 0.500 | **0.33** | 1 |" in table, \
        "rows from before Louise searched first: every search was the model's"


def test_ask_s_own_citations_and_whose_search_found_the_work(tmp_path):
    """Since 2026-11-03 Ask searches the question itself and the searches the
    model names, and the answer cites [n] and says what each n is."""
    works = tmp_path / "works.json"
    works.write_text(json.dumps([{"work": "W1", "books": ["gutenberg:11"], "original": "fr"}]))
    q = ev.read_queries(_queries(tmp_path))[:1]
    turns = [{"tool": "louise_search", "by": "louise", "error": None, "arguments": {"query": "q"},
              "result": {"lists": [{"hits": [{"uid": "gutenberg:5"}]}]}},
             {"tool": "louise_search", "by": "louise:rewrite", "error": None, "arguments": {"query": "Le Collier"},
              "result": {"lists": [{"hits": [{"uid": "gutenberg:11"}]}]}}]
    got = {"answer": "It is [2]; and [7].", "turns": turns, "rewrites": ["Le Collier"], "rewrite_s": 0.4,
           "cited": [{"n": 2, "uid": "gutenberg:11", "title": "Le Collier"}], "cited_unknown": ["[7]"]}
    ev.run(tmp_path, "m", model="m", queries=q, guess=lambda t: "en", ask=lambda qq, collections=None: got)
    (row,) = ev._rows(tmp_path, "m")
    assert (row["cited"], row["grounded"], row["cited_uids"]) == (2, 1, ["gutenberg:11"]), \
        "a number no search showed is cited and not grounded"
    assert row["hits_by"] == ["louise", "louise:rewrite"] and row["rewrites"] == ["Le Collier"]
    assert row["no_tool"], "the model itself called nothing"
    c = ev.conveyed([row], ev.load_works(works))
    assert (c["found_typed"], c["found"], c["cited_right"]) == (0, 1, 1), "the rewrite found it, not the question"


def test_the_direct_row_searches_the_question_as_typed(monkeypatch):
    from app.ask import mcp
    seen = []
    monkeypatch.setattr(mcp, "_call_tool", lambda name, args: seen.append((name, args)) or {"lists": []})
    out = ev.ask_direct(["gutenberg"])("romanzo sulla collana")
    assert seen == [("louise_search", {"query": "romanzo sulla collana", "translate": True,
                                       "collections": ["gutenberg"]})], "as the Ask tab searches"
    assert out["answer"] == "" and out["turns"][0]["tool"] == "louise_search"


def test_names_and_numbers_the_results_do_not_hold_are_what_the_model_added(tmp_path, capsys):
    """Operator, 2026-11-03: "is it adding some additional information or
    distorting an information from the collection, overlapping basically its
    own knowledge?" — entity-level factual consistency (Nan et al., 2021)."""
    turns = [{"tool": "louise_search", "result": {"lists": [{"hits": [
        {"uid": "gutenberg:2701", "title": "Moby Dick", "text": "Ahab hunts the whale, 1851, Herman Melville."}]}]}}]
    answer = ("Moby Dick was written by Herman Melville in 1851 [gutenberg:2701]. "
              "Ahab dies with Ishmael in 1852.")
    got = ev.unsupported(answer, "who hunts the whale?", turns)
    assert got["numbers"] == 2 and got["added_numbers"] == ["1852"], "1851 is in the text, 1852 is not"
    assert "Ishmael" in got["added_names"] and "Melville" not in got["added_names"]
    assert "Ahab" not in got["added_names"], "opening a sentence is not a name"
    assert ev.stated_facts("[gutenberg:2701]") == ([], []), "a citation is not a stated fact"
    qs = ev.read_queries(_queries(tmp_path))[:1]
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en",
           ask=lambda q, collections=None: {"answer": answer, "turns": turns})
    f = ev.faithfulness(ev._rows(tmp_path, "m"))
    assert (f["numbers"], f["numbers_adding"], f["numbers_found"]) == (2, 1, 0.5)
    capsys.readouterr()
    ev.score(tmp_path)
    assert "| m | 1 | 2 | **0.50** | **1** |" in capsys.readouterr().out


def test_the_blind_sample_shows_what_each_answer_added(tmp_path):
    qs = ev.read_queries(_queries(tmp_path))[:1]
    turns = [{"tool": "louise_search", "result": {"lists": [{"hits": [{"uid": "g:1", "text": "1851"}]}]}}]
    for name, answer in (("a", "Written in 1851 [g:1]."), ("b", "Written in 1852 [g:1].")):
        ev.run(tmp_path, name, model=name, queries=qs, guess=lambda t: "en",
               ask=lambda q, collections=None, a=answer: {"answer": a, "turns": turns})
    ev.sample(tmp_path, ["a", "b"], n=1)
    text = (tmp_path / "sample.md").read_text()
    assert text.count("*not in the results:* 1852") == 1 and "1851" not in text.split("not in the results")[-1]


def test_what_the_model_sent_louise_is_kept(tmp_path):
    """How the model talks to Louise: each call's tool and arguments."""
    qs = ev.read_queries(_queries(tmp_path))[:1]
    turns = [{"tool": "louise_search", "arguments": {"query": "Dumas collier", "collections": ["gutenberg"]},
              "result": {"lists": []}, "error": None}]
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en",
           ask=lambda q, collections=None: {"answer": "x", "turns": turns})
    assert ev._rows(tmp_path, "m")[0]["calls"] == [
        {"tool": "louise_search", "arguments": {"query": "Dumas collier", "collections": ["gutenberg"]},
         "by": "model", "dropped": [], "error": None}]
