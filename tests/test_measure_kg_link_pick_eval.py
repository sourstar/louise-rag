"""tools/measure/kg_link_pick_eval.py: wiki-gutenberg's linking pick on
Mewsli-X, the top 10 from BM25 over the set's own descriptions."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from tools.measure import kg_link_pick_eval as ev

CANDS = [
    {"entity_id": "Q23111", "description": "Suffolk. Suffolk is a county in East Anglia, England."},
    {"entity_id": "Q1", "description": "Suffolk County. Suffolk County is a county in New York."},
    {"entity_id": "Q2", "description": "Suffolk Punch. The Suffolk Punch is a breed of draught horse."},
    {"entity_id": "Q720285", "description": "Brooks Laich. Brooks Laich is a Canadian ice hockey player."},
    {"entity_id": "Q3", "description": "Ipswich. Ipswich is a town in Suffolk, England."},
]
MENTIONS = [
    {"mention": "Suffolk: convicted of the murders of five women found dead around the "
                "{ Suffolk } town in England.", "example_id": "a", "entity_id": "Q23111"},
    {"mention": "Brooks Laich: { Brooks Laich } picked up a power play goal.",
     "example_id": "b", "entity_id": "Q720285"},
    {"mention": "Nowhere: { Nowhere } is not a candidate.", "example_id": "c", "entity_id": "Q999"},
]


def _fetched(tmp_path: Path) -> Path:
    out = tmp_path / "mewsli"

    def download(url, target):
        if url.endswith(ev.CANDIDATES_TAR):
            data = "".join(json.dumps(c) + "\n" for c in CANDS).encode()
            with tarfile.open(target, "w:gz") as t:
                info = tarfile.TarInfo(ev.CANDIDATES)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        else:
            target.write_text("".join(json.dumps(m) + "\n" for m in MENTIONS))
    ev.fetch(out, ["en"], download=download)
    return out


def test_the_surface_and_its_sentence_are_read_from_mewsli_x_s_form():
    s, c = ev.surface_and_context(MENTIONS[1]["mention"])
    assert s == "Brooks Laich" and c.startswith("{ Brooks Laich }")


def test_fetch_unpacks_the_candidates_and_records_licence_and_sums(tmp_path):
    out = _fetched(tmp_path)
    assert "Apache-2.0" in (out / "LICENCE.txt").read_text()
    assert len(ev._jsonl(out / ev.CANDIDATES)) == len(CANDS)
    assert set(json.loads((out / "SHA256SUMS.json").read_text())) == {ev.CANDIDATES, "en.jsonl"}
    assert [m["lang"] for m in ev.mentions(out)] == ["en"] * 3


def test_the_word_search_gives_each_mention_its_top_ten(tmp_path, capsys):
    out = _fetched(tmp_path)
    ev.candidates(out)
    got = {r["example_id"]: r["candidates"] for r in ev._jsonl(out / "candidates.jsonl")}
    assert "Q23111" in got["a"] and got["b"][0] == "Q720285"
    assert "Q999" not in got["c"]
    assert "recall@10 of the word search: 0.667" in capsys.readouterr().out


@pytest.mark.parametrize("answer,n,expected", [
    ('{"choice": 2}', 3, 1), ('```json\n{"choice": "1"}\n```', 3, 0),
    ('{"choice": null}', 3, None), ('{"choice": 4}', 3, None), ("the first one", 3, None),
])
def test_a_choice_is_parsed_strictly(answer, n, expected):
    assert ev.parse_choice(answer, n) == expected


def test_a_pick_asks_only_mentions_whose_gold_is_in_the_ten_and_resumes(tmp_path, capsys):
    out = _fetched(tmp_path)
    ev.candidates(out)
    asked = []

    def picker(mention, docs):
        asked.append(mention)
        return next(i for i, d in enumerate(docs) if d.startswith(ev.surface_and_context(mention)[0] + "."))
    ev.pick(out, "p", kind="llm", model="m", url="http://x", limit=1, picker=picker)
    ev.pick(out, "p", kind="llm", model="m", url="http://x", picker=picker)
    assert len(asked) == 2, "c's gold is not in its ten; a is not asked twice"
    rows = ev._jsonl(out / "runs" / "p" / "answers.jsonl")
    assert all(r["correct"] for r in rows)
    ev.pick(out, "wrong", kind="rerank", model="r", url="http://x",
            picker=lambda m, docs: len(docs) - 1)
    capsys.readouterr()
    ev.score(out, baseline="wrong")
    table = capsys.readouterr().out
    assert "word search recall@10: 0.667" in table and "| p | 2 | **1.000**" in table


def test_other_settings_under_the_same_name_are_refused(tmp_path):
    out = _fetched(tmp_path)
    ev.candidates(out)
    ev.pick(out, "p", kind="llm", model="m", url="http://x", limit=1, picker=lambda m, d: 0)
    with pytest.raises(SystemExit, match="settings differ"):
        ev.pick(out, "p", kind="rerank", model="m", url="http://x", picker=lambda m, d: 0)


def test_the_two_kinds_have_their_own_prompt_hash():
    assert ev.prompt_hash("llm") != ev.prompt_hash("rerank")
    assert ev.prompt_hash("llm") == ev.prompt_hash("llm")
