"""tools/annotations_from_titles.py — the same-title rule (operator,
2026-10-22), on the cases München's read-through of the catalogue found."""
from tools import annotations_from_titles as t


def rec(n, title, agents=None, lang="en", creators=None, **kw):
    """`agents`: [(role, id, name)]; ids become `2009/agents/<id>`."""
    r = {"uid": f"gutenberg:{n}", "lang": lang, "title": title,
         "creators": creators if creators is not None else [a[2] for a in agents or [] if a[0] == "creator"]}
    if agents is not None:
        r["agents"] = [[role, f"2009/agents/{i}", name] for role, i, name in agents]
    r.update(kw)
    return r


def pairs(claims):
    return sorted((c["subject"], c["object"], c["evidence"]["test"]) for c in claims)


def test_the_title_loses_branding_and_part_designators():
    assert t.split_title("The Gutenberg Webster's Unabridged Dictionary: Section I, J, K, and L")[:2] == \
        ("webster's unabridged dictionary", True)
    assert t.split_title("Webster's Unabridged Dictionary (2nd 100 Pages)")[:2] == \
        ("webster's unabridged dictionary", True)
    assert t.split_title("The History of Korea, Volume 2 (of 2)") == ("the history of korea", True, 2)
    assert t.split_title("King Henry IV, Part 1") == ("king henry iv", True, 1)
    assert t.split_title("Kertomuksia II") == ("kertomuksia", True, 2)
    assert t.split_title("Richard II") == ("richard ii", False, None), "a bare numeral after a name is the title"
    assert t.base_title("The Book of Job") == "the book of job", "a designator only at the end"
    assert t.base_title("Pollyanna") == "pollyanna"


def test_one_person_under_two_names_is_one_agent():
    """"Bates, Lindon" and "Bates, Lindon, Jr." are both agent 58742."""
    claims = t.claims_of([rec(77077, "The Russian road to China", [("creator", 58742, "Bates, Lindon")]),
                          rec(77082, "The Russian road to China", [("creator", 58742, "Bates, Lindon, Jr.")])])
    assert pairs(claims) == [("gutenberg:77082", "gutenberg:77077", "a shared agent id")]
    c = claims[0]
    assert c["type"] == "same-work" and c["confidence"] == t.CONFIDENCE
    assert c["evidence"]["through"] == "gutenberg:77077"


def test_the_same_scan_or_lccn_is_one_book():
    claims = t.claims_of([rec(1, "Some Book", [("creator", 1, "A")], scan="https://archive.org/details/x/page/n5"),
                          rec(2, "Some Book", [("creator", 2, "B")], scan="https://archive.org/details/x"),
                          rec(3, "Other Book", [("creator", 3, "C")], lccn="01000503"),
                          rec(4, "Other Book", [("creator", 4, "D")], lccn="01000503")])
    assert pairs(claims) == [("gutenberg:2", "gutenberg:1", "the same source scan (MARC 904)"),
                             ("gutenberg:4", "gutenberg:3", "the same LCCN (MARC 010)")]


def test_volumes_crediting_different_people_are_one_work():
    """Operator: "different volumes of the same collection may have
    different authors" — Legends of the Jews, Szold on vols 1–2, Radin on 3–4."""
    claims = t.claims_of([rec(1493, "The Legends of the Jews — Volume 1", [("trl", 10, "Szold, Henrietta")]),
                          rec(2881, "The Legends of the Jews — Volume 3", [("trl", 11, "Radin, Paul")])])
    assert pairs(claims) == [("gutenberg:2881", "gutenberg:1493", "a volume run")]


def test_two_sets_whose_volume_numbers_collide_are_two_works():
    """Beard's and Andrews' "History of the United States, Volume 1"."""
    beard = [("creator", 1, "Beard, Charles A."), ("creator", 2, "Beard, Mary Ritter")]
    andrews = [("creator", 3, "Andrews, Elisha Benjamin")]
    claims = t.claims_of([rec(16960, "History of the United States", beard),
                          rec(20925, "History of the United States, Volume 1", andrews),
                          rec(22567, "History of the United States, Volume 2", andrews),
                          rec(22985, "History of the United States, Volume 1", beard),
                          rec(53019, "History of the United States", [("creator", 4, "Ridpath, John Clark")])])
    assert pairs(claims) == [("gutenberg:22567", "gutenberg:20925", "a shared agent id"),
                             ("gutenberg:22985", "gutenberg:16960", "a shared agent id")]


def test_an_anonymous_book_beside_a_named_one_joins_nothing():
    """An anonymous "Winter" bridged Sharp's and Thijsse's different books;
    an anonymous "The Wright Brothers" is not Kelly's biography."""
    winter = t.claims_of([rec(42199, "Winter", [("creator", 216, "Anonymous")]),
                          rec(45390, "Winter", [("creator", 1, "Sharp, Dallas Lore")]),
                          rec(76866, "Winter", [("creator", 2, "Thijsse, Jac. P.")])])
    assert winter == []
    wright = t.claims_of([rec(66194, "The Wright Brothers", [("creator", 216, "Anonymous")]),
                          rec(67672, "The Wright Brothers", [("creator", 1, "Kelly, Fred C.")])])
    assert wright == []
    webster = t.claims_of([rec(247, "Webster's Unabridged Dictionary (1st 100 Pages)", [("creator", 9, "Webster, Noah")]),
                           rec(660, "The Gutenberg Webster's Unabridged Dictionary: Section A and B",
                               [("creator", 99, "Project Gutenberg"), ("creator", 9, "Webster, Noah")]),
                           rec(29765, "Webster's Unabridged Dictionary", [("creator", 98, "Various")])])
    assert pairs(webster) == [("gutenberg:660", "gutenberg:247", "a shared agent id")]


def test_a_translator_is_someone():
    """Tagore, credited as translator of his own "The Crescent Moon", beside
    F. B. Young's novel of that name."""
    claims = t.claims_of([rec(6520, "The Crescent Moon", [("trl", 1, "Tagore, Rabindranath")]),
                          rec(58886, "The Crescent Moon", [("creator", 2, "Young, Francis Brett")])])
    assert claims == []


def test_books_crediting_nobody_join_each_other():
    claims = t.claims_of([rec(44531, "The Boyhood of Jesus", [("creator", 216, "Anonymous")]),
                          rec(49652, "The Boyhood of Jesus", [("creator", 216, "Anonymous")])])
    assert pairs(claims) == [("gutenberg:49652", "gutenberg:44531", "no book of the title names anyone")]


def test_a_genre_title_joins_only_volumes_of_one_person():
    wettergrund = [("creator", 1, "Wettergrund, Josefina")]
    claims = t.claims_of([rec(53554, "Kertomuksia I", [("creator", 2, "Gorky, Maksim")], lang="fi"),
                          rec(76525, "Kertomuksia I", wettergrund, lang="fi"),
                          rec(77185, "Kertomuksia II", wettergrund, lang="fi"),
                          rec(10, "Poems", [("creator", 3, "Someone")]),
                          rec(11, "Poems", [("creator", 3, "Someone")]),
                          rec(12, "Poems, Volume 2", [("creator", 4, "Another")]),
                          rec(13, "Poems, Volume 3", [("creator", 5, "A Third")])])
    assert pairs(claims) == [("gutenberg:77185", "gutenberg:76525", "volumes of one person under a genre title")]


def test_different_authors_of_one_title_are_not_one_work():
    claims = t.claims_of([rec(30833, "The Eyes Have It", [("creator", 1, "Garrett, Randall")]),
                          rec(30438, "The Eyes Have It", [("creator", 2, "McKimmey, James")]),
                          rec(31516, "The Eyes Have It", [("creator", 3, "Dick, Philip K.")])])
    assert claims == []


def test_a_group_is_a_star_on_its_lowest_ebook():
    """Each claim names the test that brought the book in and the book it
    came through, while its object is the group's head."""
    claims = t.claims_of([rec(5, "Le vicomte de Bragelonne, Tome II.", [("creator", 1, "Dumas, Alexandre")], lang="fr"),
                          rec(3, "Le vicomte de Bragelonne, Tome I.", [("creator", 1, "Dumas, Alexandre")], lang="fr"),
                          rec(7, "Le vicomte de Bragelonne, Tome III.", [("trl", 2, "Someone")], lang="fr")])
    assert {c["object"] for c in claims} == {"gutenberg:3"}
    assert pairs(claims) == [("gutenberg:5", "gutenberg:3", "a shared agent id"),
                             ("gutenberg:7", "gutenberg:3", "a volume run")]
    assert {c["subject"]: c["evidence"]["through"] for c in claims}["gutenberg:7"] in ("gutenberg:3", "gutenberg:5")


def test_a_record_without_agents_falls_back_to_creator_names():
    claims = t.claims_of([rec(1407, "A Message from the Sea", creators=["Dickens, Charles"]),
                          rec(9692, "A Message from the Sea", creators=["Dickens, Charles"]),
                          rec(9, "A Message from the Sea", creators=["Someone, Else"])])
    assert pairs(claims) == [("gutenberg:9692", "gutenberg:1407", "a shared agent id")]


def test_a_pair_already_joined_is_not_claimed_again():
    recs = [rec(1407, "A Message from the Sea", [("creator", 37, "Dickens, Charles")]),
            rec(9692, "A Message from the Sea", [("creator", 37, "Dickens, Charles")])]
    existing = [{"subject": "gutenberg:9692", "object": "gutenberg:1407"}]
    assert t.claims_of(recs, existing) == []
    assert len(t.claims_of(recs)) == 1
