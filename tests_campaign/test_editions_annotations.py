"""gutenberg-book-editions — the annotations collection over gutenberg (02
§2.10, 05 §5.3, §5.5): claims placed on hits, editions grouped, edges traversed."""
from tests_campaign.client import hits, post, search, work_of
from tests_campaign.ground_truth import DON_QUIXOTE, EDITIONS_ID, RELATION_TYPES, SAME_WORK

CLAIM_FIELDS = {"collection_id", "snapshot_id", "claim", "type", "relation", "role", "record", "subject_lang",
                "object_lang", "method", "confidence", "contributor", "state", "trust_level"}


def test_a_hit_of_a_work_carries_its_claims_with_every_field():
    """05 §5.3: annotations are placed on the hit, never ranked; each names its
    collection, claim, relation, the other record, method, confidence and state."""
    top = next(h for h in hits(search("Pride and Prejudice")) if work_of(h) & {"gutenberg:1342"})
    assert top["annotations"], "the work's hit carries its same-work claims"
    for c in top["annotations"]:
        assert CLAIM_FIELDS <= set(c), CLAIM_FIELDS - set(c)
        assert c["collection_id"] == EDITIONS_ID
        assert c["relation"] in RELATION_TYPES and c["type"] == "same-work"
        assert c["state"] in ("current", "stale", "unknown")
        assert {"uid", "title", "lang", "held"} <= set(c["record"])


def test_the_editions_listed_are_the_whole_work():
    """05 §5.3: the other editions are under `editions` — nothing dropped."""
    top = next(h for h in hits(search("Pride and Prejudice")) if work_of(h) & {"gutenberg:1342"})
    want = {"gutenberg:1342", *SAME_WORK["gutenberg:1342"]}
    assert want <= work_of(top), f"missing {want - work_of(top)}"
    for e in top["editions"]:
        assert {"uid", "title", "lang", "held"} <= set(e)


def test_a_translation_and_its_original_are_one_work():
    """The Spanish Don Quijote (2000) and the English Don Quixote (996) are one work."""
    got = hits(search("Don Quixote"))
    top = next((h for h in got if work_of(h) & DON_QUIXOTE), None)
    assert top is not None, [(h["uid"], h["title"][:30]) for h in got]
    assert DON_QUIXOTE <= work_of(top), work_of(top) & DON_QUIXOTE


def test_traverse_follows_the_claims_from_a_book_to_every_edition():
    """05 §5.5: louise_traverse follows the relations member's edges and
    resolves them against the target held here."""
    a = post("/api/ask/traverse", {"id": "gutenberg:1342"})
    assert a.status == 200, a
    edges = a.json["edges"]
    other = {e["from"] if e["to"] == "gutenberg:1342" else e["to"] for e in edges}
    assert SAME_WORK["gutenberg:1342"] <= other, f"missing {SAME_WORK['gutenberg:1342'] - other}"
    for e in edges:
        assert e["type"] in RELATION_TYPES and e["collection_id"] == EDITIONS_ID, e
        assert e["resolved"] is True or e["needs"], e


def test_traverse_by_relation_narrows_to_that_relation():
    """No translation-of claim exists in this collection: asking for that relation answers no edge."""
    a = post("/api/ask/traverse", {"id": "gutenberg:996", "relation": "annotations:translation-of"})
    assert a.status == 200, a
    assert a.json["edges"] == []


def test_traverse_at_depth_two_reaches_the_editions_through_the_anchor():
    """An edition's claim points at 1342; at depth 2 the other editions are reached through it."""
    a = post("/api/ask/traverse", {"id": "gutenberg:20686", "depth": 2})
    assert a.status == 200, a
    reached = {e["from"] for e in a.json["edges"]} | {e["to"] for e in a.json["edges"]}
    assert {"gutenberg:1342", *SAME_WORK["gutenberg:1342"]} <= reached, \
        {"gutenberg:1342", *SAME_WORK["gutenberg:1342"]} - reached


def test_lookup_of_a_record_uid_is_unresolved_never_an_empty_fact():
    """05 §5.3: an id with no reference row is `unresolved`; it never produces an
    empty fact. gutenberg registers no reference namespace."""
    a = post("/api/ask/lookup", {"id": "gutenberg:1342"})
    assert a.status == 200, a
    assert a.json["facts"] == [] and a.json["unresolved"] == ["gutenberg:1342"], a.json
