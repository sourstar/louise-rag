"""tools/measure/summary_eval.py: summaries from the original and from its
English version, per model, for language accuracy and speed (operator,
2026-11-03)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.measure import summary_eval as ev

DOC_DE = "Der Hund läuft schnell über die Wiese. " * 60
DOC_EN = "The dog runs quickly across the meadow. " * 60
REF = "A dog runs across a meadow."


def _english_rows():
    return [{"url": "https://wikihow/en/dog", "article": {
        "section_name": ["Running", "Short"], "document": [DOC_EN, "too short"],
        "summary": [REF, "x"]}}]


def _german_rows():
    # one section as a list of dicts, the other shape a reader may give
    return [{"url": "https://wikihow/de/hund", "article": [
        {"section_name": "Laufen", "document": DOC_DE, "summary": "Ein Hund läuft.",
         "english_url": "https://wikihow/en/dog", "english_section_name": "Running"},
        {"section_name": "Kurz", "document": "zu kurz", "summary": "x",
         "english_url": "https://wikihow/en/dog", "english_section_name": "Short"}]}]


def _fetched(tmp_path: Path) -> Path:
    out = tmp_path / "sum"
    classical = tmp_path / "evalset.json"
    classical.write_text(json.dumps([
        {"id": "la-01", "lang": "la", "src": "Gallia est omnis divisa in partes tres.",
         "ref": "All Gaul is divided into three parts."},
        {"id": "xx-01", "lang": "xx", "src": "ignored", "ref": "ignored"}]))
    rows = {"wikilingua-english.parquet": _english_rows(), "wikilingua-german.parquet": _german_rows()}

    def download(url, target):
        target.write_text("downloaded " + url)
    ev.fetch(out, langs=["de"], per_lang=5, booksum=1, classical=classical, download=download,
             parquet_rows=lambda p: rows[p.name],
             csv_rows=lambda p: [{"chapter": "Chapter one. " * 400, "summary_text": "It begins."},
                                 {"chapter": "short", "summary_text": "x"}])
    return out


def test_fetch_pairs_each_section_with_its_english_twin_and_records_licence_and_sums(tmp_path):
    out = _fetched(tmp_path)
    items = ev.items_of(out)
    assert [(i["set"], i["lang"]) for i in items] == [("wikilingua", "de"), ("booksum", "en"),
                                                      ("classical", "la")]
    de = items[0]
    assert de["text"] == DOC_DE and de["pair_text"] == DOC_EN and de["reference"] == REF
    assert "CC BY 3.0" in (out / "LICENCE.txt").read_text()
    assert set(json.loads((out / "SHA256SUMS.json").read_text())) == {
        "wikilingua-english.parquet", "wikilingua-german.parquet", "booksum-test.csv"}
    assert ev.jobs_of(items)[:2] == [(de["id"], DOC_DE), (de["id"] + ":en", DOC_EN)]
    assert len(ev.jobs_of(items)) == 5, "booksum has no English twin to summarise"


def test_the_draw_is_deterministic():
    a = ev.wikilingua_items("de", _german_rows(), _english_rows(), per_lang=5)
    assert a == ev.wikilingua_items("de", _german_rows(), _english_rows(), per_lang=5)


def test_a_run_times_warm_answers_strips_thinking_resumes_and_refuses_other_settings(tmp_path):
    out = _fetched(tmp_path)
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        if "Gallia" in prompt:
            raise RuntimeError("endpoint down")
        text = "<think>hmm</think>A dog runs." if "Hund" in prompt else "A dog runs."
        return {"content": text, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    ticks = iter(range(0, 1000, 2))
    ev.run(out, "m", model="m", url="http://x", ask=ask, clock=lambda: next(ticks))
    assert "Warm-up" in prompts[0], "the first request pays the load and is not timed"
    rows = [json.loads(l) for l in (out / "runs" / "m" / "answers.jsonl").read_text().splitlines()]
    assert len(rows) == 5 and all(r["seconds"] == 2 for r in rows)
    de = next(r for r in rows if r["key"].startswith("wl-de") and not r["key"].endswith(":en"))
    assert de["summary"] == "A dog runs." and de["thinking"] is True
    assert next(r for r in rows if r["key"] == "cl-la-01")["error"].startswith("RuntimeError")
    ev.run(out, "m", model="m", url="http://x", ask=ask)
    assert len((out / "runs" / "m" / "answers.jsonl").read_text().splitlines()) == 5, "resumed"
    with pytest.raises(SystemExit, match="settings differ"):
        ev.run(out, "m", model="other", url="http://x", ask=ask)


def test_score_gives_language_accuracy_the_gap_agreement_and_speed(tmp_path, capsys):
    out = _fetched(tmp_path)
    ask = lambda p: {"content": "A dog runs." if "Hund" not in p else "Ein Hund läuft.",
                     "usage": {"completion_tokens": 10}}
    ev.run(out, "m", model="m", url="http://x", ask=ask, clock=iter(range(0, 1000, 5)).__next__)
    chrf = lambda h, r: 100.0 if h.split()[0] == r.split()[0] else 20.0
    english = lambda t: t.startswith("A ")
    s = ev.score_run(out, "m", chrf=chrf, english=english)
    de = s[("wikilingua", "de")]
    assert de["english"] == 0.0, "the summary of the German text came back in German"
    assert de["chrf_orig"] == [20.0] and de["chrf_en"] == [100.0] and de["agree"] == [20.0]
    assert de["median_s"] == 5 and de["tok_s"] == 2.0
    la = s[("classical", "la")]
    assert la["chrf_orig"] == [] and la["agree"] == [100.0], "no reference: the two summaries compared"
    capsys.readouterr()
    ev.score(out, chrf=chrf, english=english)
    table = capsys.readouterr().out
    assert "| m | wikilingua | de | 1 | 0.00 | 20.0 | 100.0 | +80.0 | 20.0 | 0 | 0 | 5.0 | 2.0 |" in table


def test_the_prompt_hash_names_the_fixed_parts():
    assert ev.prompt_hash() == ev.prompt_hash()
    assert str(ev.SUMMARY_WORDS) in ev.prompt_for("x")
