"""tools/annotations_from_titles.py — the same-title rule (operator,
2026-10-22), on München's cases."""
from tools import annotations_from_titles as t


def rec(n, title, creators, lang="en"):
    return {"uid": f"gutenberg:{n}", "lang": lang, "title": title, "creators": creators}


WEBSTER = [rec(247, "Webster's Unabridged Dictionary (1st 100 Pages)", ["Webster, Noah"]),
           rec(248, "Webster's Unabridged Dictionary (2nd 100 Pages)", ["Webster, Noah"]),
           rec(660, "The Gutenberg Webster's Unabridged Dictionary: Section A and B",
               ["Project Gutenberg", "Webster, Noah"]),
           rec(661, "The Gutenberg Webster's Unabridged Dictionary: Section C", ["Project Gutenberg", "Webster, Noah"]),
           rec(663, "The Gutenberg Webster's Unabridged Dictionary: Section F, G and H",
               ["Project Gutenberg", "Webster, Noah"]),
           rec(664, "The Gutenberg Webster's Unabridged Dictionary: Section I, J, K, and L",
               ["Project Gutenberg", "Webster, Noah"]),
           rec(670, "The Gutenberg Webster's Unabridged Dictionary: Section X, Y, and Z",
               ["Project Gutenberg", "Webster, Noah"]),
           rec(29765, "Webster's Unabridged Dictionary", ["Various"])]


def test_the_title_loses_branding_and_part_designators():
    assert t.base_title("The Gutenberg Webster's Unabridged Dictionary: Section I, J, K, and L") == \
        "webster's unabridged dictionary"
    assert t.base_title("Webster's Unabridged Dictionary (2nd 100 Pages)") == "webster's unabridged dictionary"
    assert t.base_title("The History of Korea, Volume 2 (of 2)") == "the history of korea"
    assert t.base_title("King Henry IV, Part 1") == "king henry iv"
    assert t.base_title("The Book of Job") == "the book of job", "a designator only at the end"
    assert t.base_title("Pollyanna") == "pollyanna"


def test_the_webster_parts_are_one_work_and_various_claims_nothing():
    claims = t.claims_of(WEBSTER)
    assert {c["object"] for c in claims} == {"gutenberg:247"}, "a star on the lowest ebook"
    assert {c["subject"] for c in claims} == {f"gutenberg:{n}" for n in (248, 660, 661, 663, 664, 670)}
    assert all(c["type"] == "same-work" and c["confidence"] == "0.9230769" for c in claims)


def test_different_authors_of_one_title_are_not_one_work():
    claims = t.claims_of([rec(1, "The Eyes Have It", ["Garrett, Randall"]),
                          rec(2, "The Eyes Have It", ["McKimmey, James"]),
                          rec(3, "The Eyes Have It", ["Dick, Philip K."]),
                          rec(4, "Poems", ["Anonymous"]), rec(5, "Poems", ["Various"])])
    assert claims == []


def test_a_pair_already_joined_is_not_claimed_again():
    recs = [rec(1407, "A Message from the Sea", ["Dickens, Charles"]),
            rec(9692, "A Message from the Sea", ["Dickens, Charles"])]
    existing = [{"subject": "gutenberg:9692", "object": "gutenberg:1407"}]
    assert t.claims_of(recs, existing) == []
    assert len(t.claims_of(recs)) == 1
