"""SEARCH over gutenberg — `POST /api/ask/search` (05 §5.1–§5.4): what a ranked
query promises, one promise a test."""
import pytest

from tests_campaign.client import hits, post, search, work_of
from tests_campaign.ground_truth import SAME_WORK

HIT_FIELDS = {"collection_id", "lineage_id", "snapshot_id", "uid", "chunk_id", "layer", "rank", "legs",
              "trust_level", "title", "lang", "text", "annotations"}


def test_the_answer_is_one_list_of_ranked_hits_with_every_field_the_spec_names():
    """05 §5.1: a space searched is one list; §5.4: each hit names its record,
    chunk, rank, legs, trust and text."""
    got = hits(search("Moby Dick"))
    assert got, "a query over a held collection answers"
    assert [h["rank"] for h in got] == list(range(1, len(got) + 1)), "ranks are 1…n, in order"
    for h in got:
        assert HIT_FIELDS <= set(h), f"missing {HIT_FIELDS - set(h)} in {h['uid']}"
        assert h["uid"].startswith("gutenberg:") and h["text"], h["uid"]
        assert isinstance(h["annotations"], list)
        assert h["legs"].get("text") is not None or h["legs"].get("vector") is not None, \
            "a hit came from at least one leg"


def test_limit_caps_the_list():
    assert len(hits(search("Moby Dick", limit=3))) <= 3


def test_the_same_query_twice_gives_the_same_list():
    """05 §5.1: the fusion is deterministic (ties broken by (collection_id, uid) bytes)."""
    a = [(h["uid"], h["chunk_id"], h["rank"]) for h in hits(search("Sherlock Holmes", limit=10))]
    b = post("/api/ask/search", {"query": "Sherlock Holmes", "collections": ["gutenberg"], "limit": 10})
    assert a == [(h["uid"], h["chunk_id"], h["rank"]) for h in b.json["lists"][0]["hits"]]


@pytest.mark.parametrize("query,anchor", [
    ("Moby Dick", "gutenberg:2701"),
    ("Pride and Prejudice", "gutenberg:1342"),
    ("Alice's Adventures in Wonderland", "gutenberg:11"),
    ("Frankenstein", "gutenberg:84"),
    ("War and Peace", "gutenberg:2600"),
])
def test_a_title_finds_its_work_at_the_top(query, anchor):
    """A title is the plainest query there is: the work is among the first three."""
    got = hits(search(query))[:3]
    family = {anchor, *SAME_WORK.get(anchor, set())}
    assert any(work_of(h) & family for h in got), \
        f"{query!r}: {[(h['uid'], h['title'][:30]) for h in got]}"


@pytest.mark.parametrize("query,anchor", [
    ("a captain obsessed with hunting a white whale", "gutenberg:2701"),
    ("a girl falls down a rabbit hole into a strange world of talking animals", "gutenberg:11"),
    ("a scientist creates a living creature from dead bodies", "gutenberg:84"),
    ("a man wakes up transformed into a giant insect", "gutenberg:5200"),
])
def test_a_description_finds_its_work_by_meaning(query, anchor):
    """The vector leg (05 §5.1): a paraphrase with no title word finds the work
    among the first five."""
    got = hits(search(query))[:5]
    family = {anchor, *SAME_WORK.get(anchor, set())}
    assert any(work_of(h) & family for h in got), \
        f"{query!r}: {[(h['uid'], h['title'][:30], h['legs']) for h in got]}"


def test_one_hit_per_work():
    """05 §5.3: records joined by same-work claims show once, at the best
    edition's position; the others are under `editions`, nothing dropped."""
    got = hits(search("Pride and Prejudice"))
    seen: set[str] = set()
    for h in got:
        w = work_of(h)
        assert not (w & seen), f"{h['uid']} repeats a work already listed: {w & seen}"
        seen |= w


@pytest.mark.parametrize("query,lang", [
    ("Pride and Prejudice", "en"), ("Moby Dick", "en"), ("Alice's Adventures in Wonderland", "en"),
    ("Don Quijote de la Mancha, el ingenioso hidalgo", "es"),
])
def test_the_edition_shown_is_in_the_query_language_whenever_one_ranked(query, lang):
    """05 §5.3: the record shown is the best-ranked of the work's records in
    the query's language — or, when none of those is in the list, its
    best-ranked record. An edition that ranked carries its `rank`."""
    for h in hits(search(query)):
        ranked_in_lang = [e["uid"] for e in h.get("editions") or [] if e.get("rank") and e["lang"] == lang]
        assert h["lang"] == lang or not ranked_in_lang, \
            f"{h['uid']} shown in {h['lang']} although {ranked_in_lang} ranked in {lang}"


@pytest.mark.parametrize("query,anchor", [
    ("Pride and Prejudice", "gutenberg:1342"),
    ("Moby Dick", "gutenberg:2701"),
    ("Alice's Adventures in Wonderland", "gutenberg:11"),
])
def test_an_english_title_retrieves_an_english_edition_of_the_work(query, anchor):
    """Retrieval, not presentation: the work's English editions are held, so
    the English title reaches at least one of them — otherwise §5.3 shows a
    translation, correctly, of a list that missed the reader's book."""
    family = {anchor, *SAME_WORK.get(anchor, set())}
    for h in hits(search(query)):
        if work_of(h) & family:
            ranked = [h["lang"]] + [e["lang"] for e in h.get("editions") or [] if e.get("rank")]
            assert "en" in ranked, f"{query!r}: the work ranked only in {ranked} ({h['uid']})"
            return
    pytest.fail(f"{query!r}: the work is not in the list at all")


def test_a_spanish_query_shows_the_spanish_edition():
    top = hits(search("Don Quijote de la Mancha, el ingenioso hidalgo"))[0]
    assert "gutenberg:2000" in work_of(top) and top["lang"] == "es", (top["uid"], top["lang"])


def test_a_language_filter_restricts_every_hit_and_is_named():
    """05 §5.2: a filter restricts, nothing boosts; the response names it."""
    a = search("love and marriage", filters={"lang": "fr"})
    got = hits(a)
    assert got and all(h["lang"] == "fr" for h in got), [(h["uid"], h["lang"]) for h in got]
    assert a.json["filters"], "the filter is named in the answer"


ALICE_SUBJECT = "Alice (Fictitious character from Carroll) -- Juvenile fiction"


def _ranked(answer) -> dict[str, str]:
    """uid -> lang of every record that ranked: the heads and the editions with a rank."""
    out = {}
    for h in hits(answer):
        out[h["uid"]] = h["lang"]
        out.update({e["uid"]: e["lang"] for e in h.get("editions") or [] if e.get("rank")})
    return out


def test_an_exclusion_filter_removes_the_value_and_nothing_else():
    """05 §5.2: `{"not": v}` removes the records holding v. The German Alice
    (19778) ranks under the subject alone, and not once German is excluded."""
    both = _ranked(search("Alice in Wonderland", filters={"subjects": ALICE_SUBJECT}))
    assert "gutenberg:19778" in both, both
    without = _ranked(search("Alice in Wonderland", filters={"subjects": ALICE_SUBJECT, "lang": {"not": "de"}}))
    assert without and all(lang != "de" for lang in without.values()), without
    assert {u for u, lang in both.items() if lang != "de"} <= set(without), \
        f"the exclusion removed more than German: {set(both) - set(without)}"


def test_an_exclusion_of_a_broad_value_is_refused_as_an_exclusion():
    """05 §5.7: the postings hold only values of at most 20 000 records, so
    excluding English (63 000) cannot be computed — refused, never cut, and
    the refusal says it was an exclusion."""
    a = search("whale hunting", filters={"lang": {"not": "en"}})
    assert a.status == 409 and "excluding 'lang' = 'en'" in str(a.json), a


def test_a_filter_that_matches_nothing_returns_nothing_which_is_an_answer():
    """05 §5.2: an empty answer, not an error."""
    a = search("whale hunting", filters={"lang": "xx"})
    assert a.status == 200, a
    assert hits(a) == []


def test_a_filter_on_an_undeclared_facet_is_refused_by_name():
    a = search("whale", filters={"colour": "blue"})
    assert a.status in (400, 409), a
    assert "colour" in str(a.json)


def test_facts_are_placed_beside_the_lists_never_in_them():
    """05 §5.1, invariant 4: reference facts are never ranked or fused; for
    gutenberg (keys = []) there is no fact, and no hit has a fact's shape."""
    a = search("Moby Dick")
    assert isinstance(a.json["facts"], list)
    assert all("source_id" not in h and "ref_version" not in h for h in hits(a))


def test_an_annotations_collection_is_not_a_search_target():
    """02 §2.10: an annotations collection has no space and no legs; naming it is refused."""
    a = search("Moby Dick", collections=["gutenberg-book-editions"])
    assert a.status == 400, a


def test_an_unknown_collection_is_refused_by_name():
    a = search("Moby Dick", collections=["no-such-collection"])
    assert a.status == 400 and "no-such-collection" in str(a.json), a


def test_nothing_is_withheld_silently():
    """05 §5.3: a copy not searched is named under `withheld`; a missing leg under `legs_withheld`."""
    a = search("Moby Dick")
    assert a.json["withheld"] == [] and a.json["legs_withheld"] == [], (a.json["withheld"], a.json["legs_withheld"])
