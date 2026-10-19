"""tests/test_annotations_merge.py — tools/annotations_merge.py: two claims
files as one collection's file; a pair stated twice keeps the higher
confidence (the first file's on a tie); `same-work` and `translation-of`
over one pair both stay; a bad line is refused by file and line; the
result is one the claims reader accepts."""
import json

import pytest

from app.hold import annotations as ann
from tests.hold.test_annotations import a_claim, claims_file
from tools import annotations_merge as tool


def test_a_pair_stated_twice_keeps_the_higher_confidence_and_both_types_stay():
    works = claims_file(a_claim("gutenberg:1", "gutenberg:2", confidence="0.96875",
                                method={"kind": "wikidata"}, evidence={"item": "Q1"}),
                        a_claim("gutenberg:3", "gutenberg:4", confidence="1",
                                method={"kind": "matcher", "id": "gutenberg-editions-note", "version": 1}))
    classical = claims_file(a_claim("gutenberg:2", "gutenberg:1", confidence="0.95"),   # the same pair, reversed
                            a_claim("gutenberg:5", "gutenberg:6", confidence="0.73"),
                            a_claim("gutenberg:5", "gutenberg:6", type="translation-of", confidence="0.73"))
    claims, summary = tool.merge([("works", works), ("classical", classical)])
    assert [(c["subject"], c["object"], c["type"]) for c in claims] == [
        ("gutenberg:1", "gutenberg:2", "same-work"), ("gutenberg:3", "gutenberg:4", "same-work"),
        ("gutenberg:5", "gutenberg:6", "same-work"), ("gutenberg:5", "gutenberg:6", "translation-of")]
    assert claims[0]["confidence"] == "0.96875" and claims[0]["method"] == {"kind": "wikidata"}
    assert summary["files"] == {"works": 2, "classical": 3} and summary["kept"] == 4
    assert summary["dropped"] == {"classical": 1} and summary["from"] == {"classical": 2, "works": 2}
    assert summary["by_type"] == {"same-work": 3, "translation-of": 1}
    assert summary["by_method"] == {"matcher:gutenberg-editions-note": 1, "matcher:louise-classical-matcher": 2,
                                    "wikidata": 1}
    # the reader takes the result whole
    merged = b"".join(json.dumps(c).encode() + b"\n" for c in claims)
    assert len(ann.read_claims(merged, target_plugin="gutenberg")) == 4
    # a tie: the first file's claim
    claims, summary = tool.merge([("a", claims_file(a_claim(confidence="0.9", evidence={"from": "a"}))),
                                  ("b", claims_file(a_claim(confidence="0.9", evidence={"from": "b"})))])
    assert claims[0]["evidence"] == {"from": "a"} and summary["dropped"] == {"b": 1}
    # the higher confidence wins whichever file states it first
    claims, _ = tool.merge([("a", claims_file(a_claim(confidence="0.5", evidence={"from": "a"}))),
                            ("b", claims_file(a_claim(confidence="0.95", evidence={"from": "b"})))])
    assert claims[0]["evidence"] == {"from": "b"}


def test_a_bad_line_is_refused_by_file_and_line():
    bad = claims_file(a_claim()) + b'{"type": "guess"}\n'
    with pytest.raises(ann.AnnotationsError, match="second line 2"):
        tool.merge([("first", claims_file(a_claim("gutenberg:7", "gutenberg:8"))), ("second", bad)])
    with pytest.raises(ann.AnnotationsError, match="not JSON"):
        tool.merge([("x", b"{not json\n")])


def test_the_command_writes_the_merged_file_and_prints_the_summary(tmp_path, capsys):
    (tmp_path / "a.jsonl").write_bytes(claims_file(a_claim("gutenberg:1", "gutenberg:2")))
    (tmp_path / "b.jsonl").write_bytes(claims_file(a_claim("gutenberg:1", "gutenberg:2", confidence="0.5"),
                                                   a_claim("gutenberg:3", "gutenberg:4")))
    assert tool.main([str(tmp_path / "a.jsonl"), str(tmp_path / "b.jsonl"), "--out", str(tmp_path / "out.jsonl")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kept"] == 2 and out["dropped"] == {str(tmp_path / "b.jsonl"): 1}
    lines = (tmp_path / "out.jsonl").read_bytes().split(b"\n")
    assert len([l for l in lines if l]) == 2 and json.loads(lines[0])["confidence"] == "0.95"
    assert len(ann.read_claims((tmp_path / "out.jsonl").read_bytes())) == 2
