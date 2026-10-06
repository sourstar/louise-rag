"""tests/ask/test_cli.py — `louise ask …` (07 §7.3): the verbs are in the
tree, their flags reach the functions, a refusal is exit 2."""
import json

from app import louise
from app.ask import lookup, results, traverse
from tests.deposit.test_deposit_identity import node  # noqa: F401


def test_the_verbs_are_in_the_tree():
    p = louise.build_parser()
    a = p.parse_args(["ask", "search", "a", "truth", "--limit", "3", "--translate", "--filter", "subjects=Humans"])
    assert a.fn is louise.cmd_ask_search and a.query == ["a", "truth"] and a.limit == 3 and a.translate
    assert p.parse_args(["ask", "facets", "pubmed", "subjects", "--under", "Neoplasms"]).fn is louise.cmd_ask_facets
    assert p.parse_args(["ask", "lookup", "hgnc:1101"]).fn is louise.cmd_ask_lookup
    a = p.parse_args(["ask", "traverse", "pubmed:1", "--relation", "pubmed:cites", "--depth", "2"])
    assert a.fn is louise.cmd_ask_traverse and a.depth == 2
    assert p.parse_args(["ask", "serve"]).fn is louise.cmd_ask_serve


def test_search_passes_its_flags_and_prints_the_object(node, monkeypatch, capsys):  # noqa: F811
    seen = {}

    def fake(query, **kw):
        seen.update(query=query, **kw)
        return {"lists": [], "facts": [], "unresolved": [], "withheld": [], "legs_withheld": [], "translations": {}}
    monkeypatch.setattr(results, "search", fake)
    assert louise.main(["ask", "search", "a", "truth", "--root", str(node), "--limit", "3", "--space", "s",
                        "--filter", "subjects=Humans", "--filter", "lang=!de"]) == 0
    assert json.loads(capsys.readouterr().out)["lists"] == []
    assert seen["query"] == "a truth" and seen["limit"] == 3 and seen["space"] == "s"
    assert seen["filters"] == {"subjects": "Humans", "lang": {"not": "de"}}
    assert louise.main(["ask", "search", "q", "--root", str(node), "--filter", "nonsense"]) == 2


def test_lookup_and_traverse_print_their_objects(node, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(lookup, "lookup", lambda id_: {"facts": [], "unresolved": [id_]})
    assert louise.main(["ask", "lookup", "hgnc:1101"]) == 0
    assert json.loads(capsys.readouterr().out) == {"facts": [], "unresolved": ["hgnc:1101"]}
    monkeypatch.setattr(traverse, "traverse",
                        lambda id_, **kw: {"id": id_, "edges": [], **{k: v for k, v in kw.items() if k != "root"}})
    assert louise.main(["ask", "traverse", "pubmed:1", "--root", str(node), "--depth", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "pubmed:1" and out["depth"] == 3 and out["relation"] is None
