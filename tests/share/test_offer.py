"""tests/share/test_offer.py — the OFFER (04 §4.2, amended 2026-10-20): the
catalogue minus what this node holds, each entry with the Codex's own
words and the live neighbours serving it; superseded entries last; the
same list for `share offer` and the Library."""
import time

from app import collections as collections_mod
from app.hold import envelope as env
from app.share import catalogue, messages, offer, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import pubmed
from tests.share.test_messages import PEER_KEY

OTHER = env.key_text(__import__("nacl").signing.SigningKey(b"\x09" * 32).verify_key)


def announce(con, codex, *, by=PEER_KEY, now=None):
    catalogue.record(con, [codex], announced_by=by, now=now)


def digest(con, key, address, collections, *, now):
    body = messages.digest_body(key=key, address=address, neighbours=[], benchmarks=[],
                                collections=collections, now=now)
    peers.record_digest(con, body, now=now)


def test_the_offer_is_the_catalogue_minus_what_is_held_with_who_serves_each(node):  # noqa: F811
    now = time.time()
    a = pubmed()
    b = pubmed(descriptive=a.descriptive.__class__("Wikidata", "every entity, textified"), plugin="wikidata")
    assert a.collection_id != b.collection_id
    con = peers.connect()
    announce(con, a, now=now - 100)
    announce(con, b, by=OTHER, now=now - 50)
    digest(con, PEER_KEY, "http://peer.test:8742",
           [{"collection_id": a.collection_id, "title": "PubMed", "snapshot_id": "2026-10",
             "members": ["records-year=2024", "records-year=2025"], "license": "green", "privacy": "public"}],
           now=now)
    got = offer.offered(con, set(), now=now)
    assert [o["collection_id"] for o in got] == [a.collection_id, b.collection_id], "served first"
    pm = got[0]
    assert (pm["title"], pm["plugin"], pm["license"], pm["privacy"], pm["publication"]) == \
        ("PubMed", "pubmed", a.license.cls, "public", "open")
    assert pm["summary"] == a.descriptive.summary and pm["announced_by"] == PEER_KEY
    assert pm["served_by"] == [{"key": PEER_KEY, "address": "http://peer.test:8742",
                                "snapshot_id": "2026-10", "members": 2}]
    assert pm["superseded_by"] is None and pm["cli"].startswith(f"louise share fetch {a.collection_id}")
    assert got[1]["served_by"] == [] and got[1]["plugin"] == "wikidata"
    # held: gone from the offer
    assert [o["collection_id"] for o in offer.offered(con, {a.collection_id}, now=now)] == [b.collection_id]
    # a neighbour whose digest is too old is not live, so it serves nothing
    digest(con, OTHER, "http://other.test", [{"collection_id": b.collection_id, "members": ["x"]}],
           now=now - 10 * peers.NEIGHBOUR_WINDOW_S)
    assert offer.offered(con, set(), now=now)[1]["served_by"] == []
    con.close()


def test_a_codex_another_entry_continues_is_shown_superseded_and_listed_last(node):  # noqa: F811
    now = time.time()
    mother = pubmed()
    daughter = pubmed(continues=mother.collection_id, continues_through="2026-09")
    con = peers.connect()
    announce(con, mother, now=now - 100)
    announce(con, daughter, now=now - 10)
    got = offer.offered(con, set(), now=now)
    assert [o["collection_id"] for o in got] == [daughter.collection_id, mother.collection_id]
    assert got[1]["superseded_by"] == daughter.collection_id and got[0]["superseded_by"] is None
    assert offer.in_catalogue(mother.collection_id) and not offer.in_catalogue("sha256:" + "0" * 64)
    con.close()


def test_offered_for_node_subtracts_the_collections_held(node, monkeypatch):  # noqa: F811
    now = time.time()
    held = pubmed()
    other = pubmed(plugin="wikidata")
    con = peers.connect()
    announce(con, held, now=now)
    announce(con, other, now=now)
    con.close()

    class Held:
        codex = held
    monkeypatch.setattr(collections_mod, "load_all", lambda: [Held()])
    monkeypatch.setattr(collections_mod, "load_annotations", lambda: [])
    assert offer.held_ids() == {held.collection_id}
    assert [o["collection_id"] for o in offer.offered_for_node(now=now)] == [other.collection_id]


def test_a_collection_a_live_digest_lists_is_offered_even_before_its_codex_arrives(node):  # noqa: F811
    """The operator, 2026-10-20: "I still don't see the list". Nobody sends a
    `codex` event for a collection it merely holds, so the catalogue alone
    showed nothing; the digests, hourly, name every collection a neighbour
    holds and serves (09 §9.9) — THAT is the offer. Title, classes and the
    servers come from the digest; the Codex's plugin and summary once one is
    in the catalogue; the take pulls the Codex itself."""
    now = time.time()
    con = peers.connect()
    cid = "sha256:" + "d" * 64
    digest(con, PEER_KEY, "http://peer.test:8742",
           [{"collection_id": cid, "lineage_id": cid, "title": "Normattiva", "snapshot_id": "2026-10",
             "members": ["records-x"], "license": "yellow", "privacy": "public"}], now=now)
    (only,) = offer.offered(con, set(), now=now)
    assert (only["collection_id"], only["title"], only["license"], only["privacy"]) == \
        (cid, "Normattiva", "yellow", "public")
    assert only["plugin"] is None and only["codex_known"] is False and only["first_seen"] is None
    assert only["served_by"] == [{"key": PEER_KEY, "address": "http://peer.test:8742",
                                  "snapshot_id": "2026-10", "members": 1}]
    assert only["relation"] == "root" and only["announced_by"] == PEER_KEY
    assert offer.announced(cid, now=now) and not offer.in_catalogue(cid)
    assert not offer.announced("sha256:" + "e" * 64, now=now)
    # the Codex arrives (a codex event): the same entry, now with its plugin
    codex = pubmed()
    digest(con, PEER_KEY, "http://peer.test:8742",
           [{"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "PubMed",
             "snapshot_id": "2026-10", "members": [], "license": "green", "privacy": "public"}], now=now)
    announce(con, codex, now=now)
    (pm,) = offer.offered(con, set(), now=now)
    assert pm["collection_id"] == codex.collection_id and pm["plugin"] == "pubmed" and pm["codex_known"]
    assert pm["served_by"][0]["key"] == PEER_KEY and pm["first_seen"]
    # held: gone, whichever source named it
    assert offer.offered(con, {codex.collection_id}, now=now) == []
    con.close()
