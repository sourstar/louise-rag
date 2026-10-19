"""tests/test_annotations_merge.py — tools/annotations_merge.py: the wikidata
claims whole, a matcher claim only where its two books are not already one
work through any chain of claims, every line's bytes unchanged, and the result
one file the claims reader accepts (operator, 2026-10-19: one collection)."""
import json

from app.hold import annotations as ann
from tools import annotations_merge as tool


def _claim(s, o, method):
    return json.dumps({"type": "same-work", "subject": f"gutenberg:{s}", "object": f"gutenberg:{o}",
                       "subject_lang": "en", "object_lang": "en", "method": method,
                       "evidence": {}, "confidence": "0.95", "contributor": "me"},
                      sort_keys=True, separators=(",", ":")).encode()


WD, MATCH = {"kind": "wikidata"}, {"kind": "matcher", "id": "m", "version": 1}


def test_a_matched_pair_inside_one_work_adds_nothing_and_a_new_one_goes_in():
    works = [_claim(2, 1, WD), _claim(3, 1, WD), _claim(11, 10, WD)]
    matches = [_claim(3, 2, MATCH),          # 3 and 2 are one work through 1: nothing new
               _claim(20, 1, MATCH),         # a book the wikidata claims lack: in
               _claim(21, 1, MATCH),         # joins it too
               _claim(20, 21, MATCH),        # now inside one work: out
               _claim(10, 1, MATCH)]         # joins two works: in
    lines, s = tool.merge(works, matches)
    assert lines == works + [matches[1], matches[2], matches[4]], "the bytes as they were, in order"
    assert s == {"claims": 6, "from_works": 3, "from_matches": 3, "matches_already_one_work": 2,
                 "before": {"books": 5, "works": 2}, "after": {"books": 7, "works": 1}}


def test_the_merged_file_is_one_the_claims_reader_accepts(tmp_path):
    w, m, out = tmp_path / "w.jsonl", tmp_path / "m.jsonl", tmp_path / "out.jsonl"
    w.write_bytes(_claim(2, 1, WD) + b"\n")
    m.write_bytes(_claim(2, 1, MATCH) + b"\n" + _claim(5, 1, MATCH) + b"\n")
    assert tool.main([str(w), str(m), "--out", str(out)]) == 0
    got = ann.read_claims(out.read_bytes(), target_plugin="gutenberg")
    assert [(g.claim["subject"], g.claim["method"]["kind"]) for g in got] == [
        ("gutenberg:2", "wikidata"), ("gutenberg:5", "matcher")], \
        "the same pair twice would be refused; the merge never writes it"
