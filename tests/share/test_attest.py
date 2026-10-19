"""tests/share/test_attest.py — `share attest` (07 §7.3; 04 §4.2; 03 §3.6):
a completed check's attestation becomes one event, kept as seen for the
beat to forward, never twice; an attestation event received for a held
collection is kept beside the catalogue, one for a collection not held or
one that does not verify is not."""
import json
import time

from nacl import signing

from app.hold import envelope as env
from app.hold.codex import canonical_json
from app import identity as identity_mod
from app.bundles import keys as node_keys_mod
from app.share import attest, gossip, messages, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.share.test_messages import PEER, PEER_KEY, a_shard
from tests.share.test_share_api import OURS, OUR_KEY, FakeClient

OTHER = signing.SigningKey(b"\x0d" * 32)
OTHER_KEY = env.key_text(OTHER.verify_key)


def an_attestation(codex, key, *, check="roots", n=0, time_text="2026-10-06T00:00:00Z"):
    body = {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": "2026-08", "snapshot_hash": "sha256:" + "c" * 64, "check": check,
            "seed": None, "shards": [], "result": "pass", "detail": {"n": n},
            "platform_image": "sha256:" + "1f" * 32, "encoder_engine": None,
            "encoder_weights": None, "key": env.key_text(key.verify_key), "time": time_text}
    return env.sign(env.ATTESTATION, body, key)


def test_an_attestation_is_published_once_as_an_event_the_beat_forwards(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)                 # two of PEER's attestations lie in state/
    att = an_attestation(codex, OURS, check="chunks", n=7)
    aid = env.body_id(att["body"])
    p = attest.attestation_path(node, name, codex, aid)
    p.write_bytes(canonical_json(att))
    now = time.time()
    with peers.connect() as con:
        said: list[str] = []
        out = attest.publish(node, name, codex, [aid, "sha256:" + "0" * 64], key=OURS, own_key=OUR_KEY,
                             con=con, now=now, log=said.append)
        assert out == {"published": [aid], "already": [], "missing": ["sha256:" + "0" * 64]}
        assert said and "chunks pass published" in said[0]
        (pending,) = peers.to_forward(con)
        assert pending["hops"] == peers.HOPS
        body = pending["envelope"]["body"]
        assert (body["type"], body["key"], body["payload"]) == ("attestation", OUR_KEY, att)
        assert messages.open_event({**pending["envelope"], "hops": 8}, now=now)[0] == body, "a valid event"
        again = attest.publish(node, name, codex, [aid], key=OURS, own_key=OUR_KEY, con=con,
                               now=now + 3600)
        assert again == {"published": [], "already": [aid], "missing": []}, "never republished"
        assert len(peers.to_forward(con)) == 1
        assert attest.published(con) == {aid: pending["id"]}
        assert attest.unacted(con) == [], "our own event: nothing to act on here"
        # the beat forwards it to a neighbour and never back to us
        peers.record_digest(con, messages.digest_body(key=PEER_KEY, address="http://peer.test",
                                                      neighbours=[], benchmarks=[], collections=[],
                                                      now=now), now=now)
    monkeypatch.setattr(gossip, "_last_digest_at", now)
    monkeypatch.setattr(gossip, "_sent_to", frozenset({"http://peer.test", "http://other.test"}))
    fake = FakeClient()
    assert gossip.beat([], now=now, client=fake)["forwarded"] == 1
    assert [u for u, _ in fake.posts] == ["http://peer.test/api/share/event/share"]
    assert fake.posts[0][1]["body"]["payload"] == att


def test_a_received_attestation_event_is_kept_beside_a_held_collection_and_dropped_otherwise(node):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    now = time.time()
    good = an_attestation(codex, OTHER, check="roots")
    good_id = env.body_id(good["body"])
    forged = {**good, "sig": "00" * 64}
    elsewhere = an_attestation(codex, OTHER, check="chunks")
    elsewhere["body"] = {**elsewhere["body"], "collection_id": "sha256:" + "f" * 64}
    elsewhere = env.sign(env.ATTESTATION, elsewhere["body"], OTHER)     # verifies, not held here
    with peers.connect() as con:
        for payload in (good, forged, elsewhere):
            body = messages.event_body(key=OTHER_KEY, type_="attestation", payload=payload, now=now)
            peers.mark_seen(con, messages.event_id(body), now=now, time_text=body["time"],
                            type_="attestation", hops=7, envelope=env.sign(env.EVENT, body, OTHER))
        snap_body = messages.event_body(key=OTHER_KEY, type_="snapshot",
                                        payload={"collection_id": codex.collection_id}, now=now)
        peers.mark_seen(con, messages.event_id(snap_body), now=now, time_text=snap_body["time"],
                        type_="snapshot", hops=7, envelope=env.sign(env.EVENT, snap_body, OTHER))
        assert len(attest.unacted(con)) == 4
        said: list[str] = []
        out = attest.store_received(node, con, log=said.append)
        assert out == {"stored": 1, "held_elsewhere": 1, "dropped": 1, "other": 1}
        left = attest.unacted(con)
        assert [ev["type"] for ev in left] == ["snapshot"], \
            "the attestations acted on; the snapshot event left for its handler (2026-10-07: marked here, it never reached accept)"
        from app.share import accept
        assert [ev["id"] for ev in accept.pending(con, "snapshot")] == [left[0]["id"]]
        assert peers.to_forward(con) and len(peers.to_forward(con)) == 4, "acting is not forwarding"
    p = attest.attestation_path(node, name, codex, good_id)
    assert json.loads(p.read_bytes()) == good
    files = sorted(p.parent.glob("*.json"))
    assert len(files) == 3, "a_shard's two and this one; the forged and the foreign one are not here"
    assert any("dropped" in s for s in said) and any("kept" in s for s in said)
    # the attestation now counts where copy_state reads (03 §3.6)
    from app import copy_state
    bodies = copy_state.attestations(node, name, codex)
    assert any(b.get("key") == OTHER_KEY and b.get("check") == "roots" for b in bodies)


def test_the_beat_keeps_what_arrived_before_forwarding_it(node, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    now = time.time()
    good = an_attestation(codex, OTHER, check="roots", time_text=messages.time_text(now))
    with peers.connect() as con:
        body = messages.event_body(key=OTHER_KEY, type_="attestation", payload=good, now=now)
        peers.mark_seen(con, messages.event_id(body), now=now, time_text=body["time"],
                        type_="attestation", hops=7, envelope=env.sign(env.EVENT, body, OTHER))
    monkeypatch.setattr(gossip, "_last_digest_at", now)
    monkeypatch.setattr(gossip, "_sent_to", frozenset({"http://peer.test", "http://other.test"}))
    out = gossip.beat([], now=now, client=FakeClient())
    assert out["acted"] == {"stored": 1, "held_elsewhere": 0, "dropped": 0, "other": 0}
    assert out["forwarded"] == 1, "no neighbour yet: forwarded to nobody, and done"
    assert attest.attestation_path(node, name, codex, env.body_id(good["body"])).is_file()
    assert gossip.beat([], now=now, client=FakeClient())["acted"]["stored"] == 0, "once"


def test_the_command_checks_then_publishes_each_attestation_once(isolated_config, tmp_path, corpus_dir,
                                                                 monkeypatch, capsys):
    from app import identity, louise
    from app.bundles import keys as node_keys
    from tests.hold.test_vectors_export import build_shard
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot = build_shard(tmp_path, corpus_dir, records_per_shard=2)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "1f" * 32)
    argv = ["share", "attest", "gutenberg", "--root", str(root), "--checks", "roots"]

    def printed():
        """The command's JSON, after the stage log lines a check prints first."""
        text = capsys.readouterr().out
        return json.loads(text[text.index("\n{") + 1:] if "\n{" in text else text)
    capsys.readouterr()
    assert louise.main(argv) == 0
    out = printed()
    (check,) = out["check"]["checks"]
    assert (check["check"], check["result"]) == ("roots", "pass") and check["attestation"] == out["published"][0]
    assert out["already"] == [] and out["missing"] == []
    with peers.connect() as con:
        (pending,) = peers.to_forward(con)
        payload = pending["envelope"]["body"]["payload"]
        assert payload["body"]["check"] == "roots" and payload["body"]["key"] == OUR_KEY
        assert set(attest.published(con)) == set(out["published"])
    assert louise.main(argv) == 0
    second = printed()
    first_id = out["published"][0]
    assert first_id not in second["published"] and (second["already"] == [first_id] or second["published"]), \
        "within the same second it is the same attestation (not republished); across one, a new one"
    time.sleep(1.1)                       # 09 §9.1's time is to the second
    assert louise.main(argv) == 0
    again = printed()
    assert again["published"] and first_id not in again["published"] and again["already"] == [], \
        "a later run is a new attestation, published; the old one is not republished"
    with peers.connect() as con:
        n = len(attest.published(con))
        assert 2 <= n <= 3 and len(peers.to_forward(con)) == n, "one event per attestation published"


def test_the_hourly_pull_asks_the_neighbours_that_list_the_collection_once_an_interval(node, tmp_path, monkeypatch):  # noqa: F811
    """04 §4.2 / §4.7: attestations reach a peer from the events that carry
    them AND from `pull attestations`; the worker's beat pulls once an
    interval from the neighbours whose digest lists the collection, from
    the newest attestation it holds less one interval."""
    from fastapi.testclient import TestClient
    from app import api
    from app.share import fetch
    from tests.share.test_fetch import SHARER
    codex, name, d, snap = a_shard(node)
    fetcher = tmp_path / "fetcher" / "collections"
    fetcher.mkdir(parents=True)
    client = TestClient(api.app)
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client, key=OURS, own_key=OUR_KEY)
    fname = out["name"]
    assert out["attestations"]["kept"] == 0, "the node's version-1 bodies name no collection"
    # the node's check runs after the peer joined: only the pull can bring it
    late = an_attestation(codex, OTHER, check="records-vs-origin", time_text=messages.time_text(time.time()))
    attest.attestation_path(node, name, codex, env.body_id(late["body"])).write_bytes(canonical_json(late))

    class C:
        def __init__(self):
            self.name, self.codex = fname, codex
    now = time.time()
    monkeypatch.setattr(attest, "_last_pull_at", 0.0)
    with peers.connect() as con:
        assert attest.pull_due(fetcher, [C()], key=OURS, own_key=OUR_KEY, con=con, now=now, client=client) == {}, \
            "no neighbour lists it: nobody to ask"
        entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "t",
                 "snapshot_id": "2026-08", "snapshot_hash": snap.hash, "codex_hash": "sha256:" + "2" * 64,
                 "license": "green", "privacy": "public", "members": [], "coverage": []}
        peers.record_digest(con, messages.digest_body(key=PEER_KEY, address=SHARER, neighbours=[], benchmarks=[],
                                                      collections=[entry], now=now), now=now)
        monkeypatch.setattr(attest, "_last_pull_at", 0.0)
        said: list[str] = []
        got = attest.pull_due(fetcher, [C()], key=OURS, own_key=OUR_KEY, con=con, now=now, client=client,
                              log=said.append)
        assert got[fname]["kept"] == 1 and got[fname]["asked"] == 1 and got[fname]["ids"] == [env.body_id(late["body"])]
        assert attest.attestation_path(fetcher, fname, codex, env.body_id(late["body"])).is_file()
        assert any("1 attestation(s) pulled" in s for s in said)
        assert attest.pull_due(fetcher, [C()], key=OURS, own_key=OUR_KEY, con=con, now=now + 60, client=client) is None, \
            "once an interval"
        got = attest.pull_due(fetcher, [C()], key=OURS, own_key=OUR_KEY, con=con, now=now + peers.INTERVAL_S + 1,
                              client=client)
        assert got[fname]["kept"] == 0 and got[fname]["seen"] >= 1, "asked again an hour on, nothing new kept"
    # the beat runs it when the digest is due, with the digest's key
    monkeypatch.setattr(node_keys_mod, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity_mod, "own", lambda: OUR_KEY)
    monkeypatch.setattr(gossip, "_last_digest_at", 0.0)
    monkeypatch.setattr(attest, "_last_pull_at", 0.0)
    fake = FakeClient()
    out = gossip.beat([], now=now, client=fake, force=True)
    assert out["digest"]["sent"] == 1 and out["attestations"] == {}, "no collection held on the node side: nothing to pull"


def test_republish_publishes_this_key_s_held_attestations_without_a_check(isolated_config, tmp_path, corpus_dir,
                                                                           monkeypatch, capsys):
    from app import identity, louise
    from app.bundles import keys as node_keys
    from app.hold import collection as col
    from tests.hold.test_vectors_export import build_shard
    monkeypatch.delenv("EMBED_URL", raising=False)
    root, snapshot = build_shard(tmp_path, corpus_dir, records_per_shard=2)
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.setenv("LOUISE_PLATFORM_IMAGE", "sha256:" + "1f" * 32)
    codex = col.read(root, "gutenberg")
    # held: two of this key's (one from a check that ran before any publish, as
    # the node's 2026-10-11 run), one of another key's
    mine = [an_attestation(codex, OURS, check="roots", time_text="2026-10-11T21:13:00Z"),
            an_attestation(codex, OURS, check="records-vs-origin", time_text="2026-10-11T21:13:01Z")]
    theirs = an_attestation(codex, OTHER, check="chunks", time_text="2026-10-11T21:13:02Z")
    for envelope in mine + [theirs]:
        p = attest.attestation_path(root, "gutenberg", codex, env.body_id(envelope["body"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(canonical_json(envelope))
    capsys.readouterr()
    assert louise.main(["share", "attest", "gutenberg", "--root", str(root), "--republish"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["republish"] is True and out["held_by_this_key_unpublished"] == 2
    assert sorted(out["published"]) == sorted(env.body_id(e["body"]) for e in mine) and out["already"] == []
    with peers.connect() as con:
        pending = peers.to_forward(con)
        assert len(pending) == 2 and {p["envelope"]["body"]["payload"]["body"]["check"] for p in pending} == \
            {"roots", "records-vs-origin"}, "this key's two, as events; the other key's is not this peer's to publish"
    assert louise.main(["share", "attest", "gutenberg", "--root", str(root), "--republish"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["published"] == [] and again["held_by_this_key_unpublished"] == 0, "never twice"
