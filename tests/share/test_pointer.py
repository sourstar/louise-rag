"""tests/share/test_pointer.py — the certifier's pointer on the wire (03
§3.7; 09 §9.11): put into the DHT under the certifier's key with the
collection's raw id as salt, read back and verified, read from the
anchor domain's well-known path when the DHT gives nothing, the highest
seq kept; the seeder puts a certified collection's pointer when this peer
is its certifier, once per announcement interval."""
import json
import sqlite3

import pytest
from types import SimpleNamespace

from nacl import signing

from app import collections as collections_mod
from app.hold import certify, envelope as env
from app.hold.codex import canonical_json
from app.share import pointer, seeder
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, a_root
from tests.hold.test_anchor import CERT_KEY, T0, a_signed_shard, certified

OTHER = signing.SigningKey(b"\x0d" * 32)


class dht_mutable_item_alert:                            # noqa: N801 — libtorrent's own name is what is matched
    def __init__(self, key, salt, item, seq, authoritative=True):
        self.key, self.salt, self.item, self.seq, self.authoritative = key, salt, item, seq, authoritative


class FakeSession:
    def __init__(self, answers=()):
        self.puts, self.gets, self.answers = [], [], list(answers)

    def dht_put_mutable_item(self, private_key, public_key, data, salt):
        # what libtorrent 2.1's binding checks (its argument order and its
        # lengths): the fake mirrored the code's wrong order, and passed
        # while the node refused every put (2026-10-07)
        if len(private_key) != 64:
            raise ValueError("private key has wrong length, should be 64")
        if len(public_key) != 32:
            raise ValueError("public key has wrong length, should be 32")
        self.puts.append((public_key, private_key, data, salt))

    def dht_get_mutable_item(self, pk, salt):
        self.gets.append((pk, salt))

    def pop_alerts(self):
        out, self.answers = self.answers, []
        return out


class FakeClient:
    def __init__(self, status, body):
        self.status, self.body, self.urls = status, body, []

    def get(self, url):
        self.urls.append(url)
        return SimpleNamespace(status_code=self.status, content=self.body)


def a_certified_shard(node, codex):
    a_root(node, codex)
    name, d, snap, _ = a_signed_shard(node, codex)
    certify.certify(node, name, codex, "2026-08", KEY, now=T0)
    return name, snap, certify.read_pointer(node, name, codex)


def test_put_under_the_key_with_the_raw_id_as_salt(node):  # noqa: F811
    codex = certified()
    name, snap, doc = a_certified_shard(node, codex)
    session = FakeSession()
    data = pointer.put(session, codex, doc, KEY)
    (pk, sk, put_data, salt), = session.puts
    assert pk == bytes(KEY.verify_key) and sk == pointer.dht_secret(KEY) != bytes(KEY) + pk
    from nacl.bindings import crypto_scalarmult_ed25519_base_noclamp
    assert crypto_scalarmult_ed25519_base_noclamp(sk[:32]) == pk, \
        "the expanded secret's scalar IS the certifier's key: what the DHT verifies a put against"
    assert put_data == data == canonical_json(doc) and salt == pointer.salt_of(codex) and len(salt) == 32
    assert pointer.well_known_url(codex) == \
        "https://gazzetta.example/.well-known/louise-pointer/" + codex.collection_id.split(":")[1] + ".json"


def test_read_from_the_dht_verified_and_the_highest_seq_kept(node):  # noqa: F811
    codex = certified()
    name, snap, doc1 = a_certified_shard(node, codex)
    certify.certify(node, name, codex, "2026-08", KEY, now=T0 + 1)
    doc2 = certify.read_pointer(node, name, codex)
    assert doc2["body"]["seq"] == 2
    pk, salt = env.key_bytes(CERT_KEY), pointer.salt_of(codex)
    forged = env.sign(env.POINTER, dict(doc2["body"], seq=9, key=env.key_text(OTHER.verify_key)), OTHER)
    session = FakeSession([dht_mutable_item_alert(pk, salt, canonical_json(forged), 9, False),
                           dht_mutable_item_alert(pk, b"x" * 32, canonical_json(doc2), 2, False),
                           dht_mutable_item_alert(pk, salt, canonical_json(doc1), 1, False),
                           dht_mutable_item_alert(pk, salt, canonical_json(doc2), 2, True)])
    got = pointer.get_dht(session, codex, wait_s=1.0, sleep=lambda s: None)
    assert session.gets == [(pk, salt)] and got["body"]["seq"] == 2, "the forgery and the wrong salt dropped"
    assert pointer.get_dht(FakeSession(), codex, wait_s=0.0, sleep=lambda s: None) is None
    node_con = sqlite3.connect(":memory:")
    assert pointer.remember(node_con, doc2, codex, now=1.0) is True
    assert pointer.remember(node_con, doc1, codex, now=2.0) is False, "a lower seq is discarded"
    assert pointer.kept(node_con, codex) == {"seq": 2, "snapshot_id": "2026-08", "snapshot_hash": snap.hash,
                                             "time": doc2["body"]["time"]}


def test_the_well_known_path_when_the_dht_gives_nothing(node):  # noqa: F811
    codex = certified()
    name, snap, doc = a_certified_shard(node, codex)
    node_con = sqlite3.connect(":memory:")
    client = FakeClient(200, canonical_json(doc))
    said = []
    got = pointer.current(codex, node_con=node_con, session=FakeSession(), client=client, now=1.0, log=said.append)
    assert got["seq"] == 1 and client.urls == [pointer.well_known_url(codex)] and "well-known" in said[0]
    assert pointer.get_well_known(codex, client=FakeClient(404, b"")) is None
    drawn = []
    real_draw, pointer._draw = pointer._draw, drawn.append
    try:
        assert pointer.get_well_known(codex, client=FakeClient(200, b"<html>")) is None
    finally:
        pointer._draw = real_draw
    assert drawn == [len(b"<html>")], "invariant 5: the pointer's bytes are drawn, even when refused"
    forged = env.sign(env.POINTER, dict(doc["body"], key=env.key_text(OTHER.verify_key)), OTHER)
    assert pointer.get_well_known(codex, client=FakeClient(200, json.dumps(forged).encode())) is None
    assert pointer.current(codex, node_con=sqlite3.connect(":memory:"), session=None,
                           client=FakeClient(404, b""), now=1.0) is None
    from tests.hold.conftest import pubmed
    assert pointer.current(pubmed(), node_con=node_con, client=client) is None, "open: no pointer"


def test_the_seeder_puts_this_peer_s_own_certified_pointer_once_per_interval(node, monkeypatch):  # noqa: F811
    codex = certified()
    name, snap, doc = a_certified_shard(node, codex)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    monkeypatch.setattr(seeder, "_last_pointer_put", {})
    session = FakeSession()
    c = SimpleNamespace(name=name, codex=codex)
    said = []
    assert seeder.publish_pointer(c, session, now=T0, key=KEY, own_key=CERT_KEY, log=said.append) is True
    assert len(session.puts) == 1 and "seq 1" in said[0]
    assert seeder.publish_pointer(c, session, now=T0 + 60, key=KEY, own_key=CERT_KEY) is False, "once per interval"
    assert seeder.publish_pointer(c, session, now=T0 + seeder.ANNOUNCE_EVERY_S, key=KEY, own_key=CERT_KEY) is True
    assert seeder.publish_pointer(c, session, now=T0 + 2 * seeder.ANNOUNCE_EVERY_S, key=OTHER,
                                  own_key=env.key_text(OTHER.verify_key)) is False, "not the certifier: nothing put"
    from tests.hold.conftest import pubmed
    assert seeder.publish_pointer(SimpleNamespace(name="p", codex=pubmed()), session, now=T0, key=KEY,
                                  own_key=CERT_KEY) is False
    assert seeder.publish_pointer(c, None, now=T0 + 9 * seeder.ANNOUNCE_EVERY_S, key=KEY, own_key=CERT_KEY) is False


def test_a_put_that_fails_waits_the_interval_not_the_next_beat(node, monkeypatch):  # noqa: F811
    """Live, 2026-10-07: the failing put was retried, and logged, every beat."""
    codex = certified()
    name, snap, doc = a_certified_shard(node, codex)
    monkeypatch.setattr(collections_mod, "louise_root", lambda: node.parent)
    monkeypatch.setattr(seeder, "_last_pointer_put", {})
    c = SimpleNamespace(name=name, codex=codex)

    class Refusing(FakeSession):
        def dht_put_mutable_item(self, *a):
            raise ValueError("the DHT is down")
    with pytest.raises(ValueError):
        seeder.publish_pointer(c, Refusing(), now=T0, key=KEY, own_key=CERT_KEY)
    assert seeder.publish_pointer(c, Refusing(), now=T0 + 10, key=KEY, own_key=CERT_KEY) is False
    assert seeder.publish_pointer(c, FakeSession(), now=T0 + seeder.ANNOUNCE_EVERY_S, key=KEY,
                                  own_key=CERT_KEY) is True
