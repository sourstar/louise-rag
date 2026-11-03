"""tools/measure/kg_relations_eval.py: Re-DocRED relations through any chat
endpoint (wiki-gutenberg-amendments.md, "Nemotron for the task")."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.measure import kg_relations_eval as ev

NAMES = {"P17": "country", "P131": "located in the administrative territorial entity",
         "P569": "date of birth"}


def _doc(sents, vertex, labels, title="T"):
    return {"title": title, "sents": sents, "vertexSet": vertex, "labels": labels}


def _set(tmp_path: Path) -> Path:
    docs = [
        _doc([["Turin", "is", "in", "Italy", "."]],
             [[{"name": "Turin", "type": "LOC"}], [{"name": "Italy", "type": "LOC"}]],
             [{"h": 0, "t": 1, "r": "P17"}, {"h": 0, "t": 1, "r": "P131"}]),
        _doc([["Dante", "was", "born", "in", "1265", "."]],
             [[{"name": "Dante", "type": "PER"}], [{"name": "1265", "type": "TIME"}]],
             [{"h": 0, "t": 1, "r": "P569"}]),
    ]
    out = tmp_path / "redocred"
    out.mkdir()
    (out / "test_revised.json").write_text(json.dumps(docs))
    (out / "dev_revised.json").write_text("[]")
    (out / "properties.json").write_text(json.dumps(NAMES))
    return out


def test_load_and_the_prompt_carry_the_entities_and_the_properties(tmp_path):
    out = _set(tmp_path)
    docs = ev.load_docs(out / "test_revised.json")
    assert docs[0].text == "Turin is in Italy ." and docs[0].entities == [("Turin", "LOC", ()),
                                                                          ("Italy", "LOC", ())]
    assert docs[0].gold == {(0, "P17", 1), (0, "P131", 1)}
    assert ev.properties_of(docs) == ["P17", "P131", "P569"], "Wikidata's numeric order"
    p = ev.prompt_for(docs[0], NAMES)
    assert "0: Turin [LOC]\n1: Italy [LOC]" in p and "P131 located in" in p
    assert ev.prompt_for(docs[0], NAMES) == p, "one text for every model"


def test_the_prompt_hash_names_the_fixed_parts_only(tmp_path):
    assert ev.prompt_hash(NAMES) == ev.prompt_hash(dict(NAMES))
    assert ev.prompt_hash(NAMES) != ev.prompt_hash({**NAMES, "P50": "author"})


@pytest.mark.parametrize("answer,expected,ok", [
    ('{"relations": [{"head": 0, "relation": "P17", "tail": 1}]}', {(0, "P17", 1)}, True),
    ('```json\n{"relations": [{"head": "0", "relation": "P17", "tail": "1"}]}\n```',
     {(0, "P17", 1)}, True),
    ('{"relations": []}', set(), True),
    ('{"relations": [{"head": 0, "relation": "P999", "tail": 1}, '
     '{"head": 0, "relation": "P17", "tail": 0}, {"head": 0, "relation": "P17", "tail": 7}]}',
     set(), True),
    ('{"relations": [{"head": 0, "relation": "P17", "tail": 1}, '
     '{"head": 0, "relation": "P17", "tail": 1}]}', {(0, "P17", 1)}, True),
    ("I think Turin is in Italy.", set(), False),
    ('{"relations": "none"}', set(), False),
    ("{not json}", set(), False),
])
def test_an_answer_is_parsed_strictly(answer, expected, ok):
    got, parsed = ev.parse(answer, 2, set(NAMES))
    assert got == expected and parsed is ok


def test_micro_f1_is_re_docreds_own():
    assert ev.f1(*ev.prf({(0, "P17", 1)}, {(0, "P17", 1), (0, "P131", 1)})) == pytest.approx(
        (1.0, 0.5, 2 / 3))
    assert ev.f1(0, 0, 3) == (0.0, 0.0, 0.0)


def test_a_run_answers_each_document_once_and_resumes(tmp_path):
    out = _set(tmp_path)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        if "Turin" in prompt:
            return '{"relations": [{"head": 0, "relation": "P17", "tail": 1}]}'
        return '{"relations": [{"head": 0, "relation": "P569", "tail": 1}]}'
    ev.run(out, "m", model="m", url="http://x", limit=1, ask=ask)
    ev.run(out, "m", model="m", url="http://x", ask=ask)
    assert len(asked) == 2, "the first document is not asked again"
    s = ev.score_run(out, "m")
    assert s["docs"] == 2 and (s["precision"], s["recall"]) == (1.0, pytest.approx(2 / 3))
    assert s["per_doc"] == {0: pytest.approx(2 / 3), 1: 1.0}


def test_a_run_with_other_settings_under_the_same_name_is_refused(tmp_path):
    out = _set(tmp_path)
    ev.run(out, "m", model="m", url="http://x", limit=1, ask=lambda p: '{"relations": []}')
    with pytest.raises(SystemExit, match="settings differ"):
        ev.run(out, "m", model="m", url="http://x", extra={"chat_template_kwargs": {}},
               ask=lambda p: '{"relations": []}')


def test_score_prints_the_paired_test_against_the_baseline(tmp_path, capsys):
    out = _set(tmp_path)
    ev.run(out, "a", model="a", url="http://x", ask=lambda p: '{"relations": []}')
    ev.run(out, "b", model="b", url="http://x",
           ask=lambda p: '{"relations": [{"head": 0, "relation": "P17", "tail": 1}]}')
    ev.score(out, baseline="a")
    table = capsys.readouterr().out
    assert "| a | 2 |" in table and "| b | 2 |" in table and "over 2" in table


def test_fetch_records_the_licence_the_sums_and_the_property_names(tmp_path):
    out = tmp_path / "f"
    src = _set(tmp_path)

    def download(url, target):
        target.write_bytes((src / url.rsplit("/", 1)[1]).read_bytes())
    ev.fetch(out, download=download, labels=lambda ids: {i: NAMES[i] for i in ids})
    assert "MIT" in (out / "LICENCE.txt").read_text()
    assert json.loads((out / "properties.json").read_text()) == NAMES
    assert set(json.loads((out / "SHA256SUMS.json").read_text())) == set(ev.SPLITS)


def test_an_entity_is_named_by_its_most_frequent_mention_with_its_other_forms():
    """Re-DocRED's first test document: "Loud" first, "Loud Tour" three times."""
    v = [{"name": "Loud", "type": "MISC"}] + [{"name": "Loud Tour", "type": "MISC"}] * 3
    assert ev._entity(v) == ("Loud Tour", "MISC", ("Loud",))
    doc = ev.Doc(0, "", "x", [ev._entity(v)], frozenset())
    assert "0: Loud Tour (also: Loud) [MISC]" in ev.prompt_for(doc, NAMES)
