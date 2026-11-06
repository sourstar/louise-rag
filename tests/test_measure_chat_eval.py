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
    assert row.split("|")[11].strip() == "2", "the cold column"


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
    assert row.split("|")[13:18] == [" 1 ", " 1.0 ", " 0.50 ", " 1 ", " 0 "]


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
    assert row.split("|")[18] == " 1 "


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


def test_a_search_runs_once_and_is_replayed_for_every_model(tmp_path):
    """Operator, 2026-11-03: "Do we need the reranker for this kind of
    test?" The question's own search is the same for every model: it runs
    once, reranker included, and is replayed (CLAUDE.md RULE 1)."""
    live = []

    def call(name, args):
        live.append((name, args))
        return {"lists": [{"hits": [{"uid": "gutenberg:1"}]}]} if name == "louise_search" else {"facts": []}
    path = tmp_path / "searches.jsonl"
    version = {"gutenberg": "snap-1"}
    cache = ev.SearchCache(path, version, live=call)
    args = {"query": "q", "translate": True, "collections": ["gutenberg"]}
    first = cache("louise_search", args)
    first["lists"].append("written by the caller")
    again = ev.SearchCache(path, version, live=call)("louise_search", dict(reversed(list(args.items()))))
    assert again == {"lists": [{"hits": [{"uid": "gutenberg:1"}]}]}, "the recording, not the caller's copy"
    assert len(live) == 1, "replayed from the file, in another process's order of keys"
    cache("louise_lookup", {"id": "hgnc:1"})
    cache("louise_lookup", {"id": "hgnc:1"})
    assert [n for n, _ in live].count("louise_lookup") == 2, "only searches are kept"
    with pytest.raises(SystemExit, match="record a new file"):
        ev.SearchCache(path, {"gutenberg": "snap-2"}, live=call)


def test_a_run_says_which_searches_it_replayed(tmp_path):
    qs = ev.read_queries(_queries(tmp_path))[:2]
    cache = ev.SearchCache(tmp_path / "s.jsonl", {"g": "1"},
                           live=lambda n, a: {"lists": [{"hits": [{"uid": "gutenberg:5"}]}]})
    for name in ("direct", "again"):
        ev.run(tmp_path, name, model="m", queries=qs, guess=lambda t: "en", pid=lambda: 0,
               ask=ev.ask_direct(None, call=cache), search_cache=cache)
    assert [r["replayed"] for r in ev._rows(tmp_path, "direct")] == [0, 0]
    assert [r["replayed"] for r in ev._rows(tmp_path, "again")] == [1, 1]


def test_a_sentence_says_only_what_the_source_it_cites_says():
    """The walk of 2026-11-03 (München, qwen3-8b), and the walk of this
    measure on it: the question pasted as a title in source [1]'s mouth and
    the French title in the Finnish edition's are flagged; the reader's
    "Luigi XVI" and the possessive "Dumas's" are not; and the title from
    memory, in a sentence that cites nothing, is caught as an uncited claim."""
    turns = [{"tool": "louise_search", "result": {"lists": [{"hits": [
        {"uid": "gutenberg:74803", "title": "Hirmuhallitus", "text": "Pariisin vallankumous"},
        {"uid": "gutenberg:18271", "title": "Georges", "authors": "Dumas, Alexandre",
         "text": "Du même auteur: Le Collier de la Reine, 11 vol. in-8, 1849. Louis XVI"},
        {"uid": "gutenberg:54229", "title": "Kuningattaren kaulanauha", "authors": "Dumas, Alexandre",
         "text": "Ludvig XVI:n hovista"}]}]}}]
    sources = [{"n": 1, "uid": "gutenberg:74803"}, {"n": 2, "uid": "gutenberg:54229"},
               {"n": 7, "uid": "gutenberg:18271"}]
    question = "Il romanzo di Dumas sullo scandalo della collana alla corte di Luigi XVI"
    it = ('Il testo [7] indica che Dumas ha scritto "Le Collier de la Reine" alla corte di Luigi XVI nel 1849 [7]. '
          'Il testo [1] menziona un romanzo intitolato "Il romanzo di Dumas sullo scandalo della collana" [1]. '
          'Il testo [2] è una traduzione del romanzo "Le Collier de la Reine" [2].')
    s = ev.citation_support({"answer": it, "sources": sources}, turns, question)
    assert (s["sentences"], s["supported"], s["quotes"], s["quotes_found"]) == (3, 1, 3, 1)
    assert [u["missing"] for u in s["unsupported"]] == [["Il romanzo di Dumas sullo scandalo della collana"],
                                                        ["Le Collier de la Reine"]]
    en = ('The novel is "The Diamond Necklace Affair" (French: "L\'Affaire du collier de diamants"). '
          'It is part of Dumas\'s fiction, known as "Kuningattaren kaulanauha" [2].')
    s = ev.citation_support({"answer": en, "sources": sources}, turns,
                            "Dumas novel about the diamond necklace scandal")
    assert (s["sentences"], s["supported"], s["uncited_claims"]) == (1, 1, 1), "Dumas's is Dumas"
    assert s["uncited"][0]["missing"][:2] == ["The Diamond Necklace Affair", "L'Affaire du collier de diamants"]
    assert ev.unsupported(it, question, turns)["added_numbers"] == [], \
        "[7], [1], [2] are citations, not numbers stated"
    assert ev.citation_support({"answer": it}, turns) is None, "no sources said: not scored"


def test_score_shows_what_the_sources_support(tmp_path, capsys):
    qs = ev.read_queries(_queries(tmp_path))[:1]
    turns = [{"tool": "louise_search", "by": "louise", "arguments": {"query": "q"}, "error": None,
              "result": {"lists": [{"hits": [{"uid": "gutenberg:1", "title": "Moby Dick", "text": "Ahab"}]}]}}]
    got = {"answer": 'Ahab hunts it [1]. It is "Moby Whale" [1].', "turns": turns,
           "sources": [{"n": 1, "uid": "gutenberg:1"}], "cited": [{"n": 1, "uid": "gutenberg:1"}],
           "cited_unknown": []}
    ev.run(tmp_path, "m", model="m", queries=qs, guess=lambda t: "en", ask=lambda q, collections=None: got)
    capsys.readouterr()
    ev.score(tmp_path)
    assert "| m | 1 | 2 | **0.50** | 1 | 0.00 | **1** | **0** |" in capsys.readouterr().out
    ev.sample(tmp_path, ["m"], n=1)
    assert '*not in its cited source:* Moby Whale ← "It is "Moby Whale"."' in (tmp_path / "sample.md").read_text()


def test_a_whole_run_waits_for_a_pilot_read_one_by_one(tmp_path, monkeypatch, capsys):
    """Operator, 2026-11-03: "Check the first 3 question/answer per test
    category one by one before launching a whole test that may produce
    garbage" (measuring.md §12)."""
    asked = []

    def fake_direct(cols, call=None):
        def ask(q, collections=None):
            asked.append(q)
            return {"answer": f"answer to {q} [1]", "turns": [],
                    "sources": [{"n": 1, "uid": "gutenberg:1"}],
                    "cited": [{"n": 1, "uid": "gutenberg:1", "title": "T"}], "cited_unknown": []}
        return ask
    monkeypatch.setattr(ev, "ask_direct", fake_direct)
    out, qs = tmp_path / "out", _queries(tmp_path)
    base = ["run", "--out", str(out), "--name", "m", "--model", "m", "--queries", str(qs),
            "--tag", "t", "--direct"]
    with pytest.raises(SystemExit, match="no pilot read yet"):
        ev.main(base)
    assert ev.main(base + ["--pilot", "1"]) == 0
    assert asked == ["Dumas novel about the necklace", "romanzo sulla collana", "μυθιστόρημα"], \
        "the first question of every language, and nothing else"
    report = (out / "runs" / "m" / "pilot-t.md").read_text()
    assert "## 1. [en] Dumas novel about the necklace" in report and "- cited: gutenberg:1" in report
    with pytest.raises(SystemExit, match="no pilot read yet"):
        ev.main(base)
    with pytest.raises(SystemExit):
        ev.main(["pilot-ok", "--out", str(out), "--name", "m", "--tag", "t", "--by", " ", "--note", "x"])
    assert ev.main(["pilot-ok", "--out", str(out), "--name", "m", "--tag", "t", "--by", "dev session",
                    "--note", "3 read, clean"]) == 0
    asked.clear()
    assert ev.main(base) == 0
    assert asked == ["secondo romanzo"], "the whole run resumes after the pilot's answers"
    assert len(ev._rows(out, "m")) == 4


def test_an_arm_s_sampling_is_parsed_and_a_penalty_of_zero_refused():
    """LocalAI reads a request's penalty of 0 as unset (2026-11-06): the model
    file's value would apply without a word, so 0 is refused, not sent."""
    assert ev.parse_sampling("temperature=0.1, top_p=0.8,top_k=20,min_p=0,presence_penalty=1.5") == \
        {"temperature": 0.1, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
    assert isinstance(ev.parse_sampling("top_k=20")["top_k"], int)
    assert ev.parse_sampling("") == {}
    for bad, why in (("temp=0.1", "not one of"), ("top_p=high", "not a number"),
                     ("presence_penalty=0", "unset"), ("repeat_penalty=0.0", "unset"),
                     ("frequency_penalty=0", "unset"), ("temperature", "not one of")):
        with pytest.raises(SystemExit, match=why):
            ev.parse_sampling(bad)


def test_an_arm_is_its_own_run_and_sends_its_sampling_every_round(tmp_path, monkeypatch, capsys):
    """Operator, 2026-11-06: "optimize the results by changing their
    parameters". The run is the arm: its settings hold the sampling, its rows
    carry it, another sampling under its name is refused, and Ask's model
    sends it with every request."""
    qs = ev.read_queries(_queries(tmp_path))[:1]
    ask = _fake_ask({"Dumas novel about the necklace": {"answer": "x", "turns": []}})
    arm = {"temperature": 0.1, "presence_penalty": 1.5}
    ev.run(tmp_path, "m.t01", model="m", queries=qs, ask=ask, guess=lambda t: "en", sampling=arm)
    stated = json.loads((tmp_path / "runs" / "m.t01" / "settings.json").read_text())
    assert stated["sampling"] == arm
    assert ev._rows(tmp_path, "m.t01")[0]["sampling"] == arm
    with pytest.raises(SystemExit, match="settings differ"):
        ev.run(tmp_path, "m.t01", model="m", queries=qs, ask=ask, guess=lambda t: "en",
               sampling={"temperature": 0.7})
    ev.score(tmp_path)
    assert "| m.t01 | presence_penalty=1.5, temperature=0.1 |" in capsys.readouterr().out
    from app import llm
    sent = []
    monkeypatch.setattr(llm, "chat_tools", lambda m, t, **kw: sent.append(kw) or ("ok", [], {}))
    assert ev.arm_model(None) is None, "no arm: the model file's own sampling"
    ev.arm_model(arm)([{"role": "user", "content": "q"}], None)
    assert sent == [{"sampling": arm}]


def test_a_loop_is_a_span_repeated_back_to_back_and_a_list_is_not(tmp_path):
    """The 35B pilot, 2026-11-03: "Rouge-Profond ? Non, …" to the token bound."""
    assert ev.looped("Le titre est " + "Rouge-Profond ? Non, Rouge-Sombre. " * 6)
    assert ev.looped("A [1] b c d e. " + "the book is held here [2] and also there [3] " * 3)
    assert not ev.looped("Don Quijote de la Mancha — Miguel de Cervantes, 1605 [1]\n"
                         "Don Quijote de la Mancha — Miguel de Cervantes, 1615 [2]\n"
                         "Don Quijote de la Mancha — Miguel de Cervantes, 1780 [3]\n"), "editions differ"
    assert not ev.looped("No no no."), "too short to be a loop"
    assert not ev.looped("Moby-Dick is a novel by Herman Melville [1]. It tells of Ahab's hunt [2].")
    qs = ev.read_queries(_queries(tmp_path))[:1]
    ask = _fake_ask({"Dumas novel about the necklace": {"answer": "ok " * 30, "turns": []}})
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "en")
    assert ev._rows(tmp_path, "m")[0]["looped"] is True
    assert ev.summary(ev._rows(tmp_path, "m"))["looped"] == 1


def test_an_answer_that_thinks_aloud_is_counted(tmp_path):
    """The Q8 pilots, 2026-11-06: Qwen3.5-9B at 0.4 and 0.7 wrote its
    reasoning into the answer, invented titles retracted in place."""
    assert ev.self_talk("The novel is titled *La Dame de Monsoreau*? No, that's incorrect.")
    assert ev.self_talk("known as *The Lady of the Camellias*? No, that's different. Actually, it's")
    assert ev.self_talk("le titre est *La Reine Margot* ? Non, encore faux.")
    assert not ev.self_talk("Is it in the collections? The sources [1] say it is *The Queen's Necklace*.")
    assert not ev.self_talk("No source holds the work; the closest is [2].")
    qs = ev.read_queries(_queries(tmp_path))[:2]
    answers = {qs[0]["query"]: {"answer": "It is *Monsoreau*? No, that is incorrect. It is X [1].", "turns": []},
               qs[1]["query"]: {"answer": "È X [1].", "turns": []}}
    ev.run(tmp_path, "m", model="m", queries=qs, ask=lambda q, collections=None: answers[q],
           guess=lambda t: "en")
    assert ev.summary(ev._rows(tmp_path, "m"))["self_talk"] == 1


def test_the_size_of_asks_request_is_counted_by_the_models_own_template(tmp_path, monkeypatch):
    """2026-11-06: Command R7B serves 8,192 tokens. Does Ask's first request
    fit? The body Ask sends, max_tokens 1; usage read, or a refusal's count."""
    from app import config
    monkeypatch.setattr(config, "CHAT_API", "openai")
    monkeypatch.setattr(config, "CHAT_URL", "http://chat")
    sizes = {"short": 3000, "long": 21673}
    bodies = []

    def post(url, body):
        bodies.append(body)
        n = sizes[body["messages"][-1]["content"]]
        if n > 8192:
            return 400, {"error": {"message": f"request ({n} tokens) exceeds the available "
                                              f"context size (8192 tokens)"}}
        return 200, {"usage": {"prompt_tokens": n}, "choices": [{"message": {"content": "x"}}]}

    def ask(q, collections=None, call=None, model=None):
        content, calls, _ = model([{"role": "user", "content": q}], [{"type": "function"}])
        assert calls == [] and content
        return {"answer": content}
    qs = [{"key": "a:en", "lang": "en", "query": "short"}, {"key": "b:it", "lang": "it", "query": "long"}]
    rows = ev.prompt_sizes("m", qs, post=post, ask=ask)
    assert [r["tokens"] for r in rows] == [3000, 21673] and rows[1]["refused"] == 400
    assert all(b["max_tokens"] == 1 and b["tools"] for b in bodies), "nothing generated, tools declared"
    report = ev.sizes_report(rows, window=8192)
    assert "1 of 2 fit 8192 with 1500 for the answer" in report and "1 exceed 8192" in report


def test_a_failed_row_keeps_what_the_server_said(tmp_path):
    """2026-11-06, Command R7B's pilot: two rows said only "Server error '500
    Internal Server Error' for url …"; LocalAI logs no reason at INFO and the
    backend's ring had rotated past both. The row keeps the body and the note
    Ask adds when its fallback without tools fails too."""
    import httpx

    qs = ev.read_queries(_queries(tmp_path))[:1]
    refused = httpx.HTTPStatusError(
        "Server error '500 Internal Server Error'", request=httpx.Request("POST", "http://x"),
        response=httpx.Response(500, text='{"error":{"code":500,"message":"failed to parse tool call"}}'))
    refused.add_note("the request with tools failed first: 500: Unexpected error in RPC handling")

    def ask(query, collections=None):
        raise refused
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "en")
    error = ev._rows(tmp_path, "m")[0]["error"]
    assert error.startswith("HTTPStatusError: Server error '500")
    assert "failed to parse tool call" in error, "the server's body"
    assert "the request with tools failed first: 500: Unexpected error" in error, "the note"
    assert ev.failure(RuntimeError("endpoint down")) == "RuntimeError: endpoint down"


def test_german_nouns_labels_and_translated_names_are_not_flagged():
    """The walk of 2026-11-06 (München): the measure's "names" were German
    nouns (Quelle ×18, Werk, Konzerte), list labels ("Auteur:") and the
    reader's spelling of a source's name ("Principe" for "Prince", "Nápoles"
    for "Naples", "Calibán"); a name no source holds is still flagged."""
    assert ev.stated_facts("In der Quelle wird erwähnt, dass Berlioz Konzerte gab.", "de")[0] == []
    assert ev.stated_facts("* **Auteur:** Hector Berlioz, avec Liszt.", "fr")[0] == ["Berlioz", "Liszt"]
    assert ev.stated_facts("Personnages principaux: la comtesse et Rohan.", "fr")[0] == ["Rohan"]
    turns = [{"tool": "louise_search", "result": {"lists": [{"hits": [
        {"uid": "gutenberg:1232", "title": "The Prince", "text": "Machiavelli on Cesare Borgia"},
        {"uid": "gutenberg:23042", "title": "The Tempest", "text": "Caliban; Alonso, King of Naples"}]}]}}]
    sources = [{"n": 1, "uid": "gutenberg:1232"}, {"n": 2, "uid": "gutenberg:23042"}]
    es = "La obra incluye a Calibán y al rey Alonso de Nápoles [2]. También aparece Próspero [2]."
    s = ev.citation_support({"answer": es, "sources": sources}, turns, "La tempestad", "es")
    assert (s["sentences"], s["supported"]) == (2, 1)
    assert [u["missing"] for u in s["unsupported"]] == [["Próspero"]], "a name no source holds"
    it = "Nel testo si parla del Principe e di Cesare Borgia [1]."
    s = ev.citation_support({"answer": it, "sources": sources}, turns, "Machiavelli", "it")
    assert (s["sentences"], s["supported"]) == (1, 1)
    assert ev.unsupported(es, "La tempestad", turns, "es")["added_names"] == ["Próspero"]
    assert not ev.held("Principe", ev.fold("the tempest caliban"))


def test_rescore_rewrites_a_run_from_its_recorded_searches(tmp_path, capsys):
    """`rescore`: the searches replayed from their recording, numbered as Ask
    numbers them, the answer checked anew; a row whose sources cannot be
    rebuilt is kept as it was, and the rows as they were are kept beside."""
    args = {"collections": ["gutenberg"], "query": "La tempestad", "translate": True}
    result = {"lists": [{"hits": [
        {"uid": "gutenberg:23042", "title": "The Tempest", "text": "Caliban; Alonso, King of Naples"}]}]}
    cache = tmp_path / "s.jsonl"
    cache.write_text(json.dumps({"data": {"gutenberg": "1"}}) + "\n"
                     + json.dumps({"key": json.dumps(args, sort_keys=True, ensure_ascii=False),
                                   "result": result, "seconds": 1.0}) + "\n", encoding="utf-8")
    answer = "La obra incluye a Calibán y al rey Alonso de Nápoles [1]."
    old = {"sentences": 1, "supported": 0, "quotes": 0, "quotes_found": 0,
           "unsupported": [{"sentence": answer, "missing": ["Calibán", "Nápoles"]}],
           "uncited_claims": 0, "uncited": []}
    rows = [{"key": "W1:es", "lang": "es", "query": "La tempestad", "answer": answer, "error": None,
             "support": old, "facts": {}, "calls": [{"tool": "louise_search", "arguments": args}]},
            {"key": "W2:es", "lang": "es", "query": "otra", "answer": answer, "error": None,
             "support": old, "facts": {}, "calls": [{"tool": "louise_search", "arguments": {"query": "x"}}]}]
    run = tmp_path / "runs" / "m"
    run.mkdir(parents=True)
    (run / "answers.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert ev.main(["rescore", "--out", str(tmp_path), "--runs", "m", "--search-cache", str(cache)]) == 1
    got = ev._rows(tmp_path, "m")
    assert got[0]["checker"] == ev.CHECKER and got[0]["support"]["supported"] == 1
    assert "checker" not in got[1] and got[1]["support"] == old, "its search is not recorded: kept"
    assert (run / "answers.checker-1.jsonl").read_text(encoding="utf-8").count("\n") == 2
    assert "1 rows rescored" in capsys.readouterr().out
    # a second pass, its cache grown: the file kept beside is the rows before checker 2, untouched
    # (named after the first row's checker, it was answers.checker-2.jsonl, and a checker 3 pass
    # would then have found it there and kept none of the rows checker 2 wrote)
    with cache.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"key": json.dumps({"query": "x"}, sort_keys=True, ensure_ascii=False),
                            "result": result, "seconds": 1.0}) + "\n")
    assert ev.main(["rescore", "--out", str(tmp_path), "--runs", "m", "--search-cache", str(cache)]) == 0
    assert all(r["checker"] == ev.CHECKER for r in ev._rows(tmp_path, "m"))
    assert not (run / "answers.checker-2.jsonl").exists()
    assert [json.loads(x)["support"] for x in (run / "answers.checker-1.jsonl").read_text(
        encoding="utf-8").splitlines()] == [old, old], "the rows as checker 1 left them"
