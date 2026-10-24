"""tools/wikidata_coverage.py — a read-only measurement over a wikidata pack:
parser 1's label chain against parser 2's (`mul`), and the monuments,
buildings and events selection (the P279 closure over the dump, the classes
themselves, heritage designations, per-language coverage, P279 cycles, the
cost of one more language) — on a pack built from fixture-shaped entities."""
import bz2
import json
from pathlib import Path

import pytest
from tokenizers import Tokenizer, models, pre_tokenizers

from app.plugins.wikidata import chunker, pack
from tools import wikidata_coverage as wc

FIXTURES = Path(__file__).parent / "fixtures" / "wikidata-mini"
SHA = "sha256:" + "ab" * 32


def _claim(eid: str, pid: str, target: str, rank: str = "normal") -> dict:
    kind = "item" if target[0] == "Q" else "property"
    return {"mainsnak": {"snaktype": "value", "property": pid, "datatype": "wikibase-item",
                         "datavalue": {"value": {"entity-type": kind, "numeric-id": int(target[1:]), "id": target},
                                       "type": "wikibase-entityid"}},
            "type": "statement", "id": f"{eid}$x{pid}{target}", "rank": rank}


def entity(eid: str, labels: dict, claims: list[tuple] = (), descriptions: dict | None = None,
           sitelinks: int = 0) -> bytes:
    obj = {"type": "item", "id": eid,
           "labels": {k: {"language": k, "value": v} for k, v in labels.items()} or [],
           "descriptions": {k: {"language": k, "value": v} for k, v in (descriptions or {}).items()} or [],
           "aliases": [], "claims": {}, "sitelinks": {f"w{i}wiki": {"site": f"w{i}wiki", "title": "t", "badges": []}
                                                       for i in range(sitelinks)} or [],
           "lastrevid": 1, "modified": "2026-10-01T00:00:00Z"}
    for pid, target, *rank in claims:
        obj["claims"].setdefault(pid, []).append(_claim(eid, pid, target, *rank))
    obj["claims"] = obj["claims"] or []
    return json.dumps(obj, ensure_ascii=False).encode()


def fixture_lines() -> list[bytes]:
    out = []
    for name in ("entities.jsonl", "dump-sample.jsonl"):
        for line in (FIXTURES / name).read_bytes().split(b"\n"):
            line = line.strip()
            if line and line not in (b"[", b"]"):
                out.append(line[:-1] if line.endswith(b",") else line)
    return out


LINES = [
    entity("Q811979", {"en": "architectural structure", "it": "struttura architettonica"}),
    entity("Q57821", {"en": "fortification", "it": "fortificazione"}, [("P279", "Q811979")]),
    entity("Q100", {"en": "barbican", "it": "barbacane", "de": "Barbakane"}, [("P279", "Q57821")],
           {"en": "fortified outpost or gateway"}),
    # an instance, labelled only in mul, naming a country labelled only in mul
    entity("Q101", {"mul": "Barbakan"}, [("P31", "Q100"), ("P17", "Q500")], sitelinks=7),
    entity("Q500", {"mul": "Polska"}),
    json.dumps({"type": "property", "datatype": "wikibase-item", "id": "P17",
                "labels": {"en": {"language": "en", "value": "country"}, "it": {"language": "it", "value": "paese"}},
                "descriptions": [], "aliases": [], "claims": [], "lastrevid": 1}).encode(),
    entity("Q102", {"en": "old mill"}, [("P31", "Q999"), ("P1435", "Q600")]),
    entity("Q103", {"en": "not a barbican"}, [("P31", "Q100", "deprecated")]),
    entity("Q1656682", {"en": "event", "it": "evento"}),
    entity("Q700", {"en": "battle", "it": "battaglia"}, [("P279", "Q1656682")]),
    entity("Q701", {"de": "Schlacht bei Hastings"}, [("P31", "Q700")], {"de": "Schlacht 1066"}),
    entity("Q800", {"en": "cycle one"}, [("P279", "Q801"), ("P279", "Q811979")]),
    entity("Q801", {"en": "cycle two"}, [("P279", "Q800")]),
]


@pytest.fixture
def dump(tmp_path):
    pack.reset_caches()
    d = tmp_path / "wikidatawiki" / "entities" / "20261012"
    d.mkdir(parents=True)
    p = d / "wikidata-20261012-all.json.bz2"
    p.write_bytes(bz2.compress(b"[\n" + b",\n".join(fixture_lines() + LINES) + b"\n]\n"))
    assert pack.build(p, languages=("en",), chunk=4)["complete"]
    yield p
    pack.reset_caches()


@pytest.fixture
def word_tokenizer():
    tok = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    chunker.register(SHA, tok)
    yield
    chunker._tokenizers.pop(SHA, None)


def test_the_closure_the_classes_heritage_and_the_cycles(dump, word_tokenizer):
    got = wc.run_classes(dump, roots=("Q811979", "Q1656682"), langs=("en", "it", "de"), every=1, tokenizer=SHA,
                         max_tokens=64)
    arch, event = got["roots"]["Q811979"], got["roots"]["Q1656682"]
    assert arch["label"] == "architectural structure" and event["label"] == "event"
    assert arch["classes_in_tree"] == 5, "the root, fortification, barbican and the two classes of the cycle"
    assert arch["instances"]["entities"] == 1, "Q101; Q103's instance of is deprecated"
    assert arch["classes"]["entities"] == 5, "the vocabulary: barbican is a class, not an instance"
    assert event["instances"]["entities"] == 1 and event["classes"]["entities"] == 2
    assert got["heritage_only"] == 1, "the mill is in by its heritage designation alone"
    assert got["selected"]["entities"] == 1 + 5 + 2 + 1 + 1
    assert got["p279_cycles"]["count"] == 1 and got["p279_cycles"]["largest"][0]["size"] == 2
    assert {c["id"] for c in got["p279_cycles"]["largest"][0]["classes"]} == {"Q800", "Q801"}
    # the instance labelled only in mul has a name in every language; its description in none
    inst = arch["instances"]
    assert inst["it"]["label"] == 0 and inst["it"]["label_or_mul"] == 1 and inst["en"]["description"] == 0
    assert inst["with_sitelinks"] == 1
    assert event["instances"]["de"]["label"] == 1 and event["instances"]["de"]["description"] == 1
    # the textifier's lookups: P31, P17 and their values, per selected entity
    ref = got["referenced_labels"]
    assert ref["label_lookups"] > 0 and ref["to_properties"] > 0
    assert 0 < ref["coverage"]["it"]["with_mul"] <= 1 and ref["coverage"]["it"]["own_language"] <= \
        ref["coverage"]["it"]["with_mul"]
    cost = got["cost_per_added_language"]
    assert cost["sampled"] == got["selected"]["entities"], "every=1: all rendered"
    assert cost["chunks_per_entity"] >= 1 and cost["tokens_per_entity"] > 0
    assert cost["chunks_per_language"] >= got["selected"]["entities"]
    found = {f["id"]: f for f in got["found"]}
    assert set(found) == {"Q100"} and ("it", "barbacane") in [tuple(x) for x in found["Q100"]["labels"]]
    assert found["Q100"]["class_bits"] == 1, "barbacane is a class of the architecture tree"
    per = got["top_classes_per_root"]
    assert [c["id"] for c in per["Q811979"]] == ["Q100"] and [c["id"] for c in per["Q1656682"]] == ["Q700"]
    assert per["Q811979"][0]["path"] == ["Q100 barbican", "Q57821 fortification", "Q811979 architectural structure"], \
        "the chain that brings the class in, the class first"
    assert wc.shortest_path({5: [4], 4: [3, 9], 3: [1], 9: [1]}, 5, 1) == [5, 4, 3, 1]
    assert wc.shortest_path({5: [4]}, 5, 7) == []
    top = {t["id"]: t for t in got["top_classes"]}
    assert top["Q100"]["instances"] == 1 and top["Q100"]["roots"] == ["Q811979"]
    assert got["textifier_errors"] == {}
    # the worklist: named by the selection's texts, no Italian label and no mul one
    it = {m["id"]: m for m in got["missing_labels"]["it"]["most_referenced"]}
    assert {"Q800", "Q801"} <= set(it) and it["Q801"]["label"] == "cycle two"
    assert "Q500" not in it, "a mul label is a name in every language"
    assert "Q999" not in it, "an id the dump does not hold is not a label to add"


def test_the_pass_is_the_same_on_several_processes(dump):
    one = wc.run_classes(dump, roots=("Q811979", "Q1656682"), langs=("en", "it"), every=0, processes=1)
    two = wc.run_classes(dump, roots=("Q811979", "Q1656682"), langs=("en", "it"), every=0, processes=2)
    for g in (one, two):
        g.pop("seconds")
    assert one == two


def test_parser_1_wrote_ids_where_parser_2_writes_the_mul_label(dump):
    got = wc.run_mul(dump, groups=100, per_group=100, counts=True)
    assert got["entities"] == len(fixture_lines()) - 1 + len(LINES), "every entity but the pruned article"
    assert got["texts_changed"] >= 1 and got["written_as_id_parser1"] > got["written_as_id_parser2"]
    assert {"entity": "Q101", "referenced": "Q500", "mul_label": "Polska"} in got["examples"]
    assert got["label_rows"]["mul"] == 2 and got["mul_without_en"] == 2
    # Q101 and Q500 have only mul; Q701 only German, under its class: named by the rule, not its id
    assert got["no_english_label"] == 3 and got["no_english_nor_mul"] == 1 and got["titled_by_id"] == 0
    assert got["no_english_nor_mul_examples"] == [{"entity": "Q701", "label_count": 1,
                                                   "labels": {"de": "Schlacht bei Hastings"},
                                                   "instance_of": ["Q700 (battle)"], "sitelinks": [],
                                                   "named": ("de", "Schlacht bei Hastings")}]


def test_cycles_and_closure_on_their_own():
    child, parent = [2, 3, 4, 5, 6, 6], [1, 2, 3, 4, 5, 6]
    trees = wc.closure(child, parent, [1, 4])
    assert trees[1] == {1, 2, 3, 4, 5, 6} and trees[4] == {4, 5, 6}
    assert wc.cycles(child, parent, {1, 2, 3, 4, 5, 6}) == [[6]], "a class that is its own subclass"
    assert sorted(wc.cycles([1, 2, 3, 7], [2, 3, 1, 1], {1, 2, 3, 7})) == [[1, 2, 3]]
    assert wc.cycles([1, 2], [2, 1], {1}) == [], "only inside the closure"
