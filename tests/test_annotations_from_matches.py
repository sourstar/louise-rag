"""tests/test_annotations_from_matches.py — tools/annotations_from_matches.py:
classical_coverage.py's matches as a claims file `hold collection create
annotations` reads — one `same-work` claim per matched pair, its confidence
from the strongest signal's hand-checked precision, the cut at the confidence
that gave 90 % precision, and every claim one the claims reader accepts."""
import json

import pytest

from app.hold import annotations as ann
from tools import annotations_from_matches as tool

COVERAGE = {"summary": {}, "books": [
    {"uid": "gutenberg:17814", "lang": "el", "title": "Λυσιστράτη", "matches": [
        {"uid": "gutenberg:7700", "title": "Lysistrata", "why": ["wikidata-edition", "title 1.00"]},
        {"uid": "gutenberg:77760", "title": "Lysistrata : A modern paraphrase", "why": ["title 1.00"]}]},
    {"uid": "gutenberg:26784", "lang": "el", "title": "Ιφιγένεια εν Αυλίδι", "matches": [
        {"uid": "gutenberg:5063", "title": "The Iphigenia in Tauris", "why": ["llm"]}]},
    {"uid": "gutenberg:218", "lang": "la", "title": "De Bello Gallico", "matches": [
        {"uid": "gutenberg:10657", "title": "Caesar's Commentaries", "why": ["title 0.67", "llm"]}]},
    {"uid": "gutenberg:999", "lang": "la", "title": "Uncovered", "matches": []},
]}


def test_tiers_and_confidences_come_from_the_hand_check():
    assert tool.tier_of(["llm", "wikidata-wikipedia"]) == "wikidata"
    assert tool.tier_of(["title 0.60", "llm"]) == "title" and tool.tier_of(["llm"]) == "llm"
    with pytest.raises(ValueError):
        tool.tier_of(["guess"])
    assert (tool.confidence("wikidata"), tool.confidence("title"), tool.confidence("llm")) == \
        ("0.95", "0.95", "0.73"), "(right + 1) / (checked + 2): 18/19 and 11/15"
    assert tool.THRESHOLD == tool.confidence("llm"), "the 90 % cut includes the llm tier"


def test_one_same_work_claim_per_pair_the_la_or_el_book_its_subject():
    cs = tool.claims(COVERAGE)
    # 77760, "A modern paraphrase", is an adaptation, not a version (review of 2026-10-19)
    assert [(c["subject"], c["object"]) for c in cs] == [
        ("gutenberg:17814", "gutenberg:7700"),
        ("gutenberg:218", "gutenberg:10657"), ("gutenberg:26784", "gutenberg:5063")]
    lys = cs[0]
    assert (lys["type"], lys["subject_lang"], lys["object_lang"], lys["confidence"]) == \
        ("same-work", "el", "en", "0.95")
    assert lys["method"] == {"kind": "matcher", "id": "louise-classical-matcher", "version": 1}
    assert lys["evidence"]["signals"] == ["wikidata-edition", "title 1.00"]
    assert lys["evidence"]["precision"] == {
        "right": 17, "checked": 17, "unit": "book", "seed": "precision-30",
        "note": "a book's matches were judged together; pair precision is lower where a book has several"}
    assert cs[2]["confidence"] == "0.73" and cs[2]["evidence"]["tier"] == "llm"
    strong = tool.claims(COVERAGE, min_confidence="0.95")
    assert [c["object"] for c in strong] == ["gutenberg:7700", "gutenberg:10657"]
    assert tool.summary(cs) == {"claims": 3, "books": 3, "books_by_lang": {"el": 2, "la": 1},
                                "claims_by_tier": {"llm": 1, "title": 1, "wikidata": 1}}


def test_the_file_written_is_one_the_claims_reader_accepts(tmp_path):
    src = tmp_path / "coverage.json"
    src.write_text(json.dumps(COVERAGE, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "claims.jsonl"
    assert tool.main([str(src), "--out", str(out), "--contributor", "me"]) == 0
    got = ann.read_claims(out.read_bytes(), target_plugin="gutenberg")
    assert len(got) == 3 and {s.claim["contributor"] for s in got} == {"me"}
    assert got[0].raw == out.read_bytes().split(b"\n")[0], "the line is the origin bytes a record hashes"


def test_an_adaptation_is_left_out_with_its_reason():
    """The review of 2026-10-19: 45 of the 62 pairs the wikidata claims had not
    joined were retellings, children's versions, paraphrases, or Campe's Latin
    Robinson matched to Defoe. A title that marks an adaptation, or a la/el
    book with a creator the English record lacks, is no `same-work` claim; an
    English volume of several authors still contains the book."""
    cov = {"books": [
        {"uid": "gutenberg:30613", "lang": "el", "title": "Ομήρου Οδύσσεια", "matches": [
            {"uid": "gutenberg:6370", "title": "The Story of the Odyssey", "why": ["title 1.00"]},
            {"uid": "gutenberg:21584", "title": "The Iliad for Boys and Girls", "why": ["title 0.70"]},
            {"uid": "gutenberg:48895", "title": "The Odysseys of Homer", "why": ["title 1.00"]}]},
        {"uid": "gutenberg:73030", "lang": "la", "title": "Robinson Crusoëus", "matches": [
            {"uid": "gutenberg:521", "title": "The Life and Adventures of Robinson Crusoe", "why": ["title 1.00"]}]},
        {"uid": "gutenberg:37352", "lang": "el", "title": "Δάφνης και Χλόη", "matches": [
            {"uid": "gutenberg:55406", "title": "The Greek Romances of Heliodorus, Longus and Achilles Tatius",
             "why": ["title 0.80"]}]},
        {"uid": "gutenberg:226", "lang": "la", "title": "Cicero's Orations", "matches": [
            {"uid": "gutenberg:11080", "title": "The Orations, Volume 4", "why": ["title 0.90"]}]}]}
    creators = tool.read_creators([
        '{"uid": "gutenberg:73030", "creators": ["Campe, Joachim Heinrich", "Defoe, Daniel"]}',
        '{"uid": "gutenberg:521", "creators": ["Defoe, Daniel"]}',
        '{"uid": "gutenberg:37352", "creators": ["Longus"]}',
        '{"uid": "gutenberg:55406", "creators": ["Achilles Tatius", "Heliodorus, of Emesa", "Longus"]}'])
    left: list = []
    cs = tool.claims(cov, creators=creators, left_out=left,
                     exclude={("gutenberg:226", "gutenberg:11080"): "excluded by hand: unverified"})
    assert [(c["subject"], c["object"]) for c in cs] == [
        ("gutenberg:30613", "gutenberg:48895"), ("gutenberg:37352", "gutenberg:55406")]
    assert {(x["object"], x["why"].split(" (")[0].split(":")[0]) for x in left} == {
        ("gutenberg:6370", "the English title marks an adaptation"),
        ("gutenberg:21584", "the English title marks an adaptation"),
        ("gutenberg:521", "the la book has a creator the English record lacks"),
        ("gutenberg:11080", "excluded by hand")}
    assert tool.summary(cs, left)["left_out"] == 4
    assert [c["object"] for c in tool.claims(cov)] == ["gutenberg:11080", "gutenberg:48895",
                                                       "gutenberg:55406", "gutenberg:521"], \
        "without --records and --exclude, the title rule alone"
