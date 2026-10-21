"""tests/share/test_share_api.py — the five routes under /api/share (09
§9.9; 04 §4.2) in the web process, and the worker's gossip beat: a digest
makes a neighbour, an event is kept once with one hop fewer, a banned peer
is not heard, pulls answer exact bytes; this node's digest goes to its
recipients signed with its key, events are forwarded once."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from nacl import signing

from app import api, collections as collections_mod, identity
from app.bundles import keys as node_keys
from app.hold import envelope as env
from app.share import gossip, messages, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY
from tests.share.test_messages import PEER, PEER_KEY, a_shard

OURS = KEY
OUR_KEY = env.key_text(OURS.verify_key)


@pytest.fixture
def client(node):  # noqa: F811
    return TestClient(api.app)


def digest_envelope(address="http://peer.test:8742", now=None):
    body = messages.digest_body(key=PEER_KEY, address=address, neighbours=[], benchmarks=[],
                                collections=[], now=now)
    return env.sign(env.DIGEST, body, PEER)


# ---- the routes --------------------------------------------------------------------

def test_a_digest_from_a_fresh_key_is_accepted_and_makes_a_neighbour(client):
    r = client.post("/api/share/digest/share", json=digest_envelope())
    assert r.status_code == 200 and r.json()["accepted"] is True
    with peers.connect() as con:
        assert peers.neighbours(con, now=time.time()) == [{"key": PEER_KEY,
                                                           "address": "http://peer.test:8742"}]
    tampered = digest_envelope()
    tampered["body"]["address"] = "http://evil"
    assert client.post("/api/share/digest/share", json=tampered).status_code == 400
    future = digest_envelope(now=time.time() + 2 * peers.INTERVAL_S)
    assert client.post("/api/share/digest/share", json=future).status_code == 400
    assert client.post("/api/share/digest/share", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400


def test_an_event_is_kept_once_with_one_hop_fewer(client):
    body = messages.event_body(key=PEER_KEY, type_="snapshot",
                               payload={"collection_id": "sha256:" + "0" * 64})
    message = {**env.sign(env.EVENT, body, PEER), "hops": 8}
    r = client.post("/api/share/event/share", json=message)
    assert r.status_code == 200 and r.json() == {"accepted": True, "id": env.body_id(body),
                                                 "hops": 7}
    r = client.post("/api/share/event/share", json=message)
    assert r.json()["seen"] is True and r.json()["accepted"] is False
    with peers.connect() as con:
        pending = peers.to_forward(con)
    assert len(pending) == 1 and pending[0]["hops"] == 7
    assert pending[0]["envelope"]["body"] == body
    assert client.post("/api/share/event/share", json={**message, "hops": 9}).status_code == 400
    assert client.post("/api/share/event/share", json=env.sign(env.EVENT, body, PEER)).status_code == 400


def test_a_banned_peer_is_not_heard(client):
    with peers.connect() as con:
        peers.ban(con, PEER_KEY, now=time.time(), why="a lie")
    r = client.post("/api/share/digest/share", json=digest_envelope())
    assert r.status_code == 202 and r.json()["accepted"] is False
    pull = env.sign(env.EVENT, messages.pull_body(key=PEER_KEY, what="codex",
                                                  collection_id="sha256:" + "0" * 64), PEER)
    assert client.post("/api/share/codex/fetch", json=pull).status_code == 403


def test_pulls_answer_exact_bytes_and_a_route_answers_its_own_kind(client, node):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    pull = env.sign(env.EVENT, messages.pull_body(key=PEER_KEY, what="codex",
                                                  collection_id=codex.collection_id), PEER)
    r = client.post("/api/share/codex/fetch", json=pull)
    assert r.status_code == 200
    (path, data), = messages.verify_items(r.json()["items"])
    assert data == (d / "codex.json").read_bytes()
    assert client.post("/api/share/snapshot/fetch", json=pull).status_code == 400
    pull = env.sign(env.EVENT, messages.pull_body(key=PEER_KEY, what="snapshot",
                                                  collection_id=codex.collection_id), PEER)
    items = dict(messages.verify_items(client.post("/api/share/snapshot/fetch", json=pull).json()["items"]))
    assert items["snapshots/2026-08.json"] == snap.canonical()
    pull = env.sign(env.EVENT, messages.pull_body(key=PEER_KEY, what="attestations",
                                                  collection_id=codex.collection_id,
                                                  since="2026-10-03T00:00:00Z"), PEER)
    assert len(client.post("/api/share/attestations/fetch", json=pull).json()["items"]) == 1


# ---- the worker's beat ----------------------------------------------------------------

class FakeClient:
    """An httpx-shaped client that remembers what was posted and answers 200."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.bytes = 0

    def post(self, url, content=None, headers=None):
        assert headers == {"content-type": "application/json"}
        self.bytes += len(content)
        self.posts.append((url, json.loads(content)))

        class R:
            status_code = 200

            def json(self_inner):
                return {"accepted": True}
        return R()


def test_the_beat_sends_this_nodes_digest_signed_to_every_recipient(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.setattr(gossip, "_last_digest_at", 0.0)
    now = time.time()
    with peers.connect() as con:
        peers.record_digest(con, messages.digest_body(key=PEER_KEY, address="http://peer.test",
                                                      neighbours=[], benchmarks=[],
                                                      collections=[], now=now), now=now)
    from app import louise
    louise._write_held_file(node, codex, "http://models:1/upstream/x")
    fake = FakeClient()
    out = gossip.beat(collections_mod.load_all(), now=now, client=fake, force=True)
    assert out["digest"] == {"sent": 1, "failed": 0, "recipients": ["http://peer.test"]}
    (url, envelope), = fake.posts
    assert url == "http://peer.test/api/share/digest/share"
    body = messages.open_digest(envelope, now=now)
    assert body["key"] == OUR_KEY and body["address"].startswith("http://")
    assert body["neighbours"] == [{"key": PEER_KEY, "address": "http://peer.test"}]
    # not shared: not takeable, so not announced (operator, 2026-10-21)
    assert [e for e in body["collections"] if e["collection_id"] == codex.collection_id] == []
    assert gossip.beat(collections_mod.load_all(), now=now + 60, client=fake)["digest"] is None, \
        "once an interval"
    # a peer heard from meanwhile is not kept waiting for the interval: it
    # gets the digest at the next beat, it alone (2026-10-07: the second
    # peer waited for the node's first digest, the one naming its swarm)
    newcomer = env.key_text(signing.SigningKey(bytes([7]) * 32).verify_key)
    with peers.connect() as con:
        peers.record_digest(con, messages.digest_body(key=newcomer, address="http://new.test", neighbours=[],
                                                      benchmarks=[], collections=[], now=now + 90), now=now + 90)
    fake.posts.clear()
    out = gossip.beat(collections_mod.load_all(), now=now + 120, client=fake)
    assert out["digest"]["recipients"] == ["http://new.test"] and len(fake.posts) == 1
    assert gossip.beat(collections_mod.load_all(), now=now + 180, client=fake)["digest"] is None, \
        "then it is in the interval's set: once, never every beat"


def test_the_beat_forwards_an_event_once_to_the_neighbours_and_never_back(node, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gossip, "_last_digest_at", time.time())     # no digest this beat
    monkeypatch.setattr(gossip, "_sent_to", frozenset({"http://peer.test", "http://other.test"}))
    now = time.time()
    other = signing.SigningKey(b"\x09" * 32)
    other_key = env.key_text(other.verify_key)
    with peers.connect() as con:
        for key, address in ((PEER_KEY, "http://peer.test"), (other_key, "http://other.test")):
            peers.record_digest(con, messages.digest_body(key=key, address=address, neighbours=[],
                                                          benchmarks=[], collections=[], now=now),
                                now=now)
        body = messages.event_body(key=PEER_KEY, type_="bundle", payload={"x": 1}, now=now)
        peers.mark_seen(con, env.body_id(body), now=now, time_text=body["time"], type_="bundle",
                        hops=7, envelope=env.sign(env.EVENT, body, PEER))
    fake = FakeClient()
    assert gossip.beat([], now=now, client=fake)["forwarded"] == 1
    assert [u for u, _ in fake.posts] == ["http://other.test/api/share/event/share"]
    assert fake.posts[0][1]["hops"] == 7 and fake.posts[0][1]["body"] == body
    assert gossip.beat([], now=now, client=fake)["forwarded"] == 0, "forwarded once"


def test_a_digest_that_did_not_reach_a_peer_is_retried_with_a_backoff_not_an_hour_later(node, monkeypatch):  # noqa: F811
    """Perugia, 2026-10-19: its digest after a redeploy was refused — the
    node was restarting at that moment — and counted as the interval's
    send, so the next reached the node an hour later. A failed address is
    retried after RETRY_S, then two, four… up to the interval."""
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.setattr(gossip, "_last_digest_at", 0.0)
    monkeypatch.setattr(gossip, "_sent_to", frozenset())
    monkeypatch.setattr(gossip, "_retry_at", {})
    now = time.time()
    with peers.connect() as con:
        for key, address in ((PEER_KEY, "http://peer.test"), (env.key_text(signing.SigningKey(bytes([9]) * 32).verify_key),
                                                              "http://down.test")):
            peers.record_digest(con, messages.digest_body(key=key, address=address, neighbours=[], benchmarks=[],
                                                          collections=[], now=now), now=now)

    class Flaky(FakeClient):
        down = True

        def post(self, url, content=None, headers=None):
            if url.startswith("http://down.test") and self.down:
                raise ConnectionError("[Errno 111] Connection refused")
            return super().post(url, content, headers)
    fake = Flaky()
    out = gossip.beat([], now=now, client=fake, force=True)
    assert (out["digest"]["sent"], out["digest"]["failed"]) == (1, 1) and "unreached" not in out["digest"]
    assert gossip.beat([], now=now + 15, client=fake)["digest"] is None, "within the backoff: not every beat"
    fake.down = False
    out = gossip.beat([], now=now + gossip.RETRY_S + 1, client=fake)
    assert out["digest"]["recipients"] == ["http://down.test"] and out["digest"]["sent"] == 1, \
        "the one that was not reached, a minute on — not an hour"
    assert gossip.beat([], now=now + gossip.RETRY_S + 30, client=fake)["digest"] is None, "reached: in the interval's set"
    # a peer that stays down costs one refused connection a minute, then two, four…
    fake.down = True
    monkeypatch.setattr(gossip, "_last_digest_at", 0.0)
    t = now + 7200
    gossip.beat([], now=t, client=fake, force=True)
    tries = [t]
    for step in range(1, 400):
        at = t + step * 15
        got = gossip.beat([], now=at, client=fake)
        if got["digest"] is not None and "http://down.test" in got["digest"]["recipients"]:
            tries.append(at)
        if len(tries) == 5:
            break
    gaps = [b - a for a, b in zip(tries, tries[1:])]
    assert gaps == [60, 120, 240, 480], gaps



def test_a_digest_announces_only_what_can_be_taken(node, monkeypatch):  # noqa: F811
    """Operator, 2026-10-21: a collection that cannot be taken is not
    announced. Held without a snapshot (wikipedia on the node: a Codex, no
    records build) or held and not shared, it is in no digest entry; shared
    with a snapshot, it is."""
    from app import louise
    from app.share import seeder
    codex, name, d, snap = a_shard(node)
    louise._write_held_file(node, codex, "http://models:1/upstream/x")
    root = collections_mod.louise_root() / "collections"

    def ids():
        return [e["collection_id"] for e in gossip.entries(root, collections_mod.load_all())]

    assert codex.collection_id not in ids(), "not shared"
    monkeypatch.setattr(seeder, "wants_serving", lambda c: True)
    assert ids() == [codex.collection_id], "shared, with a snapshot"
    monkeypatch.setattr(seeder, "current_snapshot", lambda tree: None)
    assert codex.collection_id not in ids(), "shared, never built"
