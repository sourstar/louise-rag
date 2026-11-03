"""tools/measure/chat_eval.py: Ask's own loop per chat model, scored
deterministically (tool use, the answer's language, time)."""
from __future__ import annotations

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


def test_score_splits_lightnings_six_from_the_others(tmp_path, capsys):
    qs = ev.read_queries(_queries(tmp_path))
    ask = lambda q, collections=None: {"answer": q, "rounds": 1, "exhausted": False, "turns": []}
    ev.run(tmp_path, "m", model="m", queries=qs, ask=ask, guess=lambda t: "it")
    ev.score(tmp_path)
    table = capsys.readouterr().out
    assert "| m | all | 4 |" in table and "| m | Lightning's six | 3 |" in table
    assert "| m | the others | 1 |" in table


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
