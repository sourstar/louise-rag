"""tools/annotations_from_works.py — gutenberg's work claims from what the
origin states (Wikipedia lines, editions notes) joined by Wikidata: stars,
not cliques; no claim through an anthology; one claim per pair; a claims
file `read_claims` accepts. On the measurement's data it gives the same
1,694 works of 6,060 books as docs/notes/one-language-per-work.md §1."""
import json

from app.hold import annotations as ann
from tools import annotations_from_works as tool

WIKI = {"titles": {"en.wikipedia.org|Don Quixote": "Q480", "es.wikipedia.org|Don Quijote": "Q480",
                   "en.wikipedia.org|King Lear": "Q181598", "de.wikipedia.org|Ahnfrau": "Q9",
                   "en.wikipedia.org|Old": "Q1"},
        "items": {"Q1": {"redirect_to": "Q480"}}}


def rec(n, lang, wiki=(), editions=None):
    r = {"uid": f"gutenberg:{n}", "lang": lang, "wiki": list(wiki)}
    if editions:
        r["editions"], r["editions_note"] = editions, "Project Gutenberg has several editions of this eBook"
    return r


RECORDS = [
    rec(996, "en", ["https://en.wikipedia.org/wiki/Don_Quixote"]),
    rec(2000, "es", ["https://es.wikipedia.org/wiki/Don_Quijote"]),
    rec(5903, "fi", ["https://en.wikipedia.org/wiki/Old"]),               # a redirect to Q480
    rec(7, "de", ["https://en.wikipedia.org/wiki/King_Lear", "https://de.wikipedia.org/wiki/Ahnfrau"]),
    rec(1532, "en", ["https://en.wikipedia.org/wiki/King_Lear"]),
    rec(46, "en", editions=[46, 19337, 99999]),
    rec(19337, "en"),
]


def test_a_work_is_a_star_of_claims_and_an_anthology_claims_nothing():
    claims = tool.claims_of(RECORDS, WIKI)
    pairs = {(c["subject"], c["object"], c["method"].get("id", c["method"]["kind"])) for c in claims}
    assert pairs == {("gutenberg:2000", "gutenberg:996", "wikidata"),
                     ("gutenberg:5903", "gutenberg:996", "wikidata"),
                     ("gutenberg:46", "gutenberg:19337", "gutenberg-editions-note")}, \
        "Don Quixote: two claims on its lowest ebook; King Lear alone (the anthology joins nothing); " \
        "the editions note's held edition only"
    dq = next(c for c in claims if c["subject"] == "gutenberg:2000")
    assert dq["evidence"] == {"wikidata": "Q480", "subject_wikipedia": "https://es.wikipedia.org/wiki/Don_Quijote",
                              "object_wikipedia": "https://en.wikipedia.org/wiki/Don_Quixote"}
    assert (dq["subject_lang"], dq["object_lang"], dq["confidence"]) == ("es", "en", "0.96875")


def test_the_file_is_one_claims_file_the_annotations_collection_accepts(tmp_path):
    (tmp_path / "r.jsonl").write_text("\n".join(json.dumps(r) for r in RECORDS), "utf-8")
    (tmp_path / "w.json").write_text(json.dumps(WIKI), "utf-8")
    assert tool.main([str(tmp_path / "r.jsonl"), str(tmp_path / "w.json"),
                      "--out", str(tmp_path / "c.jsonl")]) == 0
    got = ann.read_claims((tmp_path / "c.jsonl").read_bytes(), target_plugin="gutenberg")
    assert len(got) == 3


def test_an_editions_note_inside_one_wikidata_work_adds_no_claim():
    recs = [rec(996, "en", ["https://en.wikipedia.org/wiki/Don_Quixote"], editions=[2000]),
            rec(2000, "es", ["https://es.wikipedia.org/wiki/Don_Quijote"], editions=[996])]
    claims = tool.claims_of(recs, WIKI)
    assert [(c["subject"], c["object"]) for c in claims] == [("gutenberg:2000", "gutenberg:996")]
