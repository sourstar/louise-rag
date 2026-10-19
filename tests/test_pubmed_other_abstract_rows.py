"""tools/pubmed_other_abstract_rows.py: the records cut to a title alone while
the origin gives them an abstract (an OtherAbstract), found 2026-10-19."""
from tools import pubmed_other_abstract_rows as scan


def test_a_title_alone_beside_an_other_abstract_is_found_and_nothing_else():
    recs = [
        {"uid": "pubmed:1", "sections": [["Title", "Japan's TFR plunges"]],
         "extra": {"other_abstracts": [{"type": "PIP", "language": "eng", "text": "This news…"}]}},
        {"uid": "pubmed:2", "sections": [["Title", "T"], ["Abstract", "A"]],
         "extra": {"other_abstracts": [{"type": "KIE", "text": "x"}]}},          # has its abstract
        {"uid": "pubmed:3", "sections": [["Title", "Editorial."]], "extra": {}},  # nothing more
        {"uid": "pubmed:4", "sections": [["Title", "T"]],
         "extra": {"other_abstracts": [{"type": "PIP", "text": ""}]}},           # an empty one
    ]
    assert [r["uid"] for r in scan.other_abstract_only(recs)] == ["pubmed:1"]
