"""tests/share/test_fetch.py — `share fetch`, first brick (04 §4.2, §4.3;
09 §9.4, §9.9; 07 §7.3): who is asked and in what order, the DHT probe,
the Codex and snapshot chains pulled and verified, a lie banned, the
collection held with its signed snapshots and an unsigned one staged."""
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from nacl import signing

from app import api
from app.hold import collection as col, envelope as env, snapshot as sn
from app.hold.codex import canonical_json
from app.share import fetch, members as mem, messages, peers
from tests.deposit.test_deposit_identity import node  # noqa: F401
from tests.hold.conftest import KEY, NOW
from tests.share.test_messages import PEER, PEER_KEY, a_shard
from tests.share.test_share_api import OURS, OUR_KEY

OTHER = signing.SigningKey(b"\x0c" * 32)
OTHER_KEY = env.key_text(OTHER.verify_key)
SHARER = "http://sharer.test:8742"
#: two daughters of one lineage whose root nobody offers (07 §7.6: a ref that
#: IS a held collection's id resolves to it first, lineage last)
CID_A, CID_B, LIN = ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64)


def entry(cid, lineage, sid, **over):
    return {"collection_id": cid, "lineage_id": lineage, "title": "t", "snapshot_id": sid,
            "snapshot_hash": "sha256:" + "1" * 64, "codex_hash": "sha256:" + "2" * 64,
            "license": "green", "privacy": "public", "members": [], "coverage": [], **over}


def digest(key, address, entries, now, **extra):
    body = messages.digest_body(key=key, address=address, neighbours=[], benchmarks=[],
                                collections=entries, now=now)
    return {**body, **extra}


@pytest.fixture
def fetcher(tmp_path):
    """The fetching peer's root; the sharer's is the node's (the web answers
    pulls from it)."""
    root = tmp_path / "fetcher" / "collections"
    root.mkdir(parents=True)
    return root


def second_snapshot(d, first: sn.Snapshot, *, previous=None, signed: bool):
    """A 2026-09 beside the sharer's 2026-08, linking to `previous` (the
    first's hash by default), signed or not."""
    snap = sn.Snapshot(collection_id=first.collection_id, snapshot_id="2026-09",
                       previous=first.hash if previous is None else previous,
                       shards=first.shards, delta=sn.Delta((), (), ()), files={})
    (d / "snapshots/2026-09.json").write_bytes(snap.canonical())
    if signed:
        (d / "snapshots/2026-09.sig.json").write_bytes(
            canonical_json(sn.sidecar_add(None, snap.hash, KEY, now=NOW)))
    return snap


# ---- finding ----------------------------------------------------------------------------

def test_sources_are_asked_in_order_and_a_lineage_resolves_to_the_leading_daughter(node, monkeypatch):  # noqa: F811
    now = time.time()
    with peers.connect() as con:
        peers.record_digest(con, digest(PEER_KEY, "http://peer.test",
                                        [entry(CID_A, LIN, "2026-08")], now), now=now)
        peers.record_digest(con, digest(OTHER_KEY, "http://other.test",
                                        [entry(CID_B, LIN, "2026-09")], now, peer_port=6881), now=now)
        monkeypatch.setattr(peers, "bootstrap_addresses", lambda: ("http://boot.test", "http://peer.test"))
        found = fetch.sources(con, LIN, now=now, from_address="hand.test:8742")
        neighbours = sorted([(PEER_KEY, "http://peer.test"), (OTHER_KEY, "http://other.test")])
        assert [s.address for s in found] == ["http://hand.test:8742"] + [a for _, a in neighbours] \
            + ["http://boot.test"], "by hand, then the neighbours that hold it, then the list"
        by_addr = {s.address: s for s in found}
        assert by_addr["http://other.test"].peer_port == 6881 and by_addr["http://peer.test"].peer_port is None
        assert by_addr["http://boot.test"].entry is None, "asked blind"
        assert fetch.target_id(found, LIN) == CID_B, "the newest snapshot: the leading daughter"
        assert fetch.target_id(found, CID_A) == CID_A
        blind = fetch.sources(con, "sha256:" + "f" * 64, now=now)
        assert [s.address for s in blind] == ["http://boot.test", "http://peer.test"]
        assert fetch.target_id(blind, "sha256:" + "f" * 64) == "sha256:" + "f" * 64


def test_previous_snapshot_id_and_the_probe_ids():
    assert fetch.previous_snapshot_id("2026-01") == "2025-12"
    assert fetch.previous_snapshot_id("2026-W01") == "2025-W52"
    for sid in ("2026-01", "2026-07", "2026-W01", "2026-W40", "2020-W53"):
        assert sn.next_snapshot_id(fetch.previous_snapshot_id(sid)) == sid
    ids = fetch.probe_ids(datetime(2026, 10, 6, tzinfo=timezone.utc))
    assert len(ids) == 24 and ids[0] == "2026-09" and ids[11] == "2025-10"
    assert ids[12] == "2026-W40" and ids[23] == "2026-W29"


class dht_get_peers_reply_alert:   # noqa: N801 — libtorrent's name is the test
    def __init__(self, info_hash, peers_):
        self.info_hash, self._peers = info_hash, peers_

    def peers(self):
        return self._peers


class FakeDHT:
    def __init__(self, answers):
        self.asked: list[bytes] = []
        self.answers = answers          # {key bytes: [(ip, port)]}
        self.served = False

    def dht_get_peers(self, h):
        self.asked.append(h.to_bytes())

    def pop_alerts(self):
        if self.served:
            return []
        self.served = True
        lt = pytest.importorskip("libtorrent")
        return [dht_get_peers_reply_alert(lt.sha1_hash(k), v) for k, v in self.answers.items()]


def test_find_dht_probes_both_keys_of_twelve_ids_and_takes_the_newest_answered():
    pytest.importorskip("libtorrent")
    when = datetime(2026, 10, 6, tzinfo=timezone.utc)
    ref = CID_A
    session = FakeDHT({mem.lineage_key(ref, "2026-08"): [("10.0.0.2", 30042), ("10.0.0.2", 30042)],
                       mem.collection_key(ref, "2026-07"): [("10.0.0.3", 30042)],
                       mem.collection_key(ref, "2026-W40"): [("fe80::1", 30042)]})
    t = [0.0]

    def clock():
        t[0] += 0.5
        return t[0]
    found = fetch.find_dht(session, ref, now=when, wait_s=30, sleep=lambda s: None, clock=clock)
    assert len(session.asked) == 24 and len(set(session.asked)) == 24, \
        "twelve ids of two cadences; a root's lineage key is its collection key, probed once"
    assert session.asked[0] == mem.collection_key(ref, "2026-09")
    daughter = FakeDHT({})
    fetch.find_dht(daughter, CID_B, now=when, wait_s=1, sleep=lambda s: None, clock=clock)
    assert len(daughter.asked) == 24, "a daughter's own id: its lineage is another id, not probed here"
    assert [s.address for s in found] == ["http://[fe80::1]:30042"], \
        "the newest id with announcers, each peer once"
    assert fetch.find_dht(FakeDHT({}), ref, now=when, wait_s=3, sleep=lambda s: None, clock=clock) == []


# ---- pulling and holding -------------------------------------------------------------------

def test_fetch_holds_the_codex_and_the_signed_snapshot_from_a_peer_given_by_hand(node, fetcher):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    client = TestClient(api.app)
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client,
                      key=OURS, own_key=OUR_KEY, encoder="http://models:1/upstream/x")
    assert (out["from"], out["chain"], out["pre_ack"], out["tried"]) == (SHARER, 1, True, [])
    assert out["snapshots"] == {"promoted": ["2026-08"], "kept": [], "staged": []}
    assert out["snapshot_id"] == "2026-08" and out["snapshot_hash"] == snap.hash and out["signatures"] == 1
    fname = col.name_of(fetcher, codex.collection_id)
    fd = col.shard_dir_of(fetcher, fname)
    assert (fd / "codex.json").read_bytes() == (d / "codex.json").read_bytes(), "byte for byte"
    assert (fd / "snapshots/2026-08.json").read_bytes() == snap.canonical()
    side = json.loads((fd / "snapshots/2026-08.sig.json").read_bytes())
    assert sn.sidecar_keys(side, snap.hash)[0] == [env.key_text(KEY.verify_key)]
    held = fetcher / f"{fname}.toml"
    text = held.read_text(encoding="utf-8")
    assert out["file"] == str(held) and f'codex = "{codex.collection_id}"' in text \
        and "share = true" in text and 'url = "http://models:1/upstream/x"' in text, \
        "a fetched public collection is shared (04 §4.3: sharing is automatic; 2026-10-19)"
    assert not (fetcher / fname / "state" / fetch.STAGING).exists()
    again = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client,
                        key=OURS, own_key=OUR_KEY, encoder="http://models:1/upstream/x")
    assert again["snapshots"] == {"promoted": [], "kept": ["2026-08"], "staged": []}, "nothing twice"
    assert (fd / "snapshots/2026-08.sig.json").read_bytes() == canonical_json(side), "the union of one"


def test_the_chain_is_walked_back_and_an_unsigned_snapshot_is_staged_not_promoted(node, fetcher):  # noqa: F811
    codex, name, d, first = a_shard(node)
    second = second_snapshot(d, first, signed=False)
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    assert out["snapshots"] == {"promoted": ["2026-08"], "kept": [], "staged": ["2026-09"]}
    assert out["snapshot_id"] == "2026-09" and out["signatures"] == 0 \
        and out["unsigned_because"] == ["no sidecar came with it: unsigned"]
    fname = col.name_of(fetcher, codex.collection_id)
    fd = col.shard_dir_of(fetcher, fname)
    assert (fd / "snapshots/2026-08.json").is_file() and not (fd / "snapshots/2026-09.json").exists()
    staged = fetcher / fname / "state" / fetch.STAGING / "snapshots/2026-09.json"
    assert staged.read_bytes() == second.canonical()
    assert "[encoder]" not in (fetcher / f"{fname}.toml").read_text(encoding="utf-8"), \
        "no --encoder: the file names none until the operator adds one"
    # the sharer signs it later: the next fetch promotes it and pulls nothing older
    (d / "snapshots/2026-09.sig.json").write_bytes(
        canonical_json(sn.sidecar_add(None, second.hash, KEY, now=NOW)))
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    assert out["snapshots"] == {"promoted": ["2026-09"], "kept": [], "staged": []}
    assert (fd / "snapshots/2026-09.json").read_bytes() == second.canonical()


def test_a_chain_that_does_not_link_is_refused_and_nothing_is_written(node, fetcher):  # noqa: F811
    codex, name, d, first = a_shard(node)
    second_snapshot(d, first, previous="sha256:" + "9" * 64, signed=True)
    with pytest.raises(fetch.FetchError, match="does not link") as e:
        fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                    key=OURS, own_key=OUR_KEY)
    assert e.value.exit_code == 2 and list(fetcher.iterdir()) == []


def test_a_lie_bans_the_sender_and_the_next_source_is_tried(node, fetcher, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    now = time.time()
    lie = messages.collection_entry(codex, snap, (), codex_bytes=b"not the bytes",
                                    lineage_id=codex.collection_id)
    with peers.connect() as con:
        peers.record_digest(con, digest(PEER_KEY, "http://liar.test", [lie], now), now=now)
    monkeypatch.setattr(peers, "bootstrap_addresses", lambda: ("http://honest.test",))
    said: list[str] = []
    out = fetch.fetch(fetcher, codex.collection_id, client=TestClient(api.app), key=OURS,
                      own_key=OUR_KEY, now=now, log=said.append)
    assert out["from"] == "http://honest.test"
    assert len(out["tried"]) == 1 and "otherwise than its digest advertised" in out["tried"][0]
    assert said and "liar.test" in said[0] and PEER_KEY in said[0]
    with peers.connect() as con:
        assert peers.is_banned(con, PEER_KEY, now=now), "not a neighbour for a day (04 §4.2)"
        assert fetch.sources(con, codex.collection_id, now=now) == [fetch.Source("http://honest.test")]


def test_tampered_bytes_are_a_lie_and_a_bad_hash_claim_is_refused(node, fetcher, monkeypatch):  # noqa: F811
    codex, name, d, snap = a_shard(node)
    real = messages.answer_pull

    def tampered(root, body):
        got = real(root, body)
        if body["what"] == "codex" and got["items"]:
            got["items"][0]["b64"] = messages.item("x", b"other bytes")["b64"]
        return got
    monkeypatch.setattr(messages, "answer_pull", tampered)
    with pytest.raises(fetch.FetchError, match="a lie"):
        fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                    key=OURS, own_key=OUR_KEY)
    assert list(fetcher.iterdir()) == []


def test_no_peer_known_exits_2_and_every_peer_down_exits_3(node, fetcher):  # noqa: F811
    with pytest.raises(fetch.FetchError, match="no peer is known") as e:
        fetch.fetch(fetcher, CID_A, key=OURS, own_key=OUR_KEY)
    assert e.value.exit_code == 2

    class Down:
        def post(self, url, content=None, headers=None):
            raise ConnectionError("connection refused")
    with pytest.raises(fetch.Unreachable, match="no peer answered") as e:
        fetch.fetch(fetcher, CID_A, from_address="http://down.test", client=Down(),
                    key=OURS, own_key=OUR_KEY)
    assert e.value.exit_code == 3
    # a peer that answers but does not hold it: refused, not deferred
    with pytest.raises(fetch.FetchError, match="does not hold") as e:
        fetch.fetch(fetcher, CID_A, from_address=SHARER, client=TestClient(api.app),
                    key=OURS, own_key=OUR_KEY)
    assert e.value.exit_code == 2


def test_the_command_defers_on_a_peer_that_is_down_and_writes_nothing(node, fetcher, monkeypatch, capsys):  # noqa: F811
    from app import identity, louise
    from app.bundles import keys as node_keys
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    assert louise.main(["share", "fetch", CID_A[7:], "--root", str(fetcher),
                        "--from", "http://127.0.0.1:9"]) == 3, "deferred: the peer is down (07 §7.7)"
    assert "no peer answered" in capsys.readouterr().err
    assert louise.main(["share", "fetch", CID_A, "--root", str(fetcher)]) == 2, "no peer known"
    assert list(fetcher.iterdir()) == []


def test_the_command_writes_the_slices_and_kinds_asked_for_into_the_file(node, fetcher, monkeypatch, capsys):  # noqa: F811
    import tomllib
    from app import louise
    from tests.hold.conftest import a_root, pubmed
    codex = a_root(fetcher, pubmed())
    held = louise._write_held_file(fetcher, codex, None)
    monkeypatch.setattr(fetch, "fetch", lambda root, ref, **kw: {"file": str(held), "name": "pubmed",
                                                                 "collection_id": ref})
    cid = codex.collection_id
    assert louise.main(["share", "fetch", cid, "--root", str(fetcher), "--from", SHARER,
                        "--slice", "year=2023", "year=2024", "--kinds", "records,relations"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["selection"] == {"slices": ["year=2023", "year=2024"],
                                "kinds": ["records", "vectors", "relations"]}
    with open(held, "rb") as f:
        raw = tomllib.load(f)
    assert raw["slices"] == ["year=2023", "year=2024"] and raw["kinds"] == ["records", "vectors", "relations"]
    assert louise.main(["share", "fetch", cid, "--root", str(fetcher), "--from", SHARER,
                        "--slice", "year=2025"]) == 0
    assert json.loads(capsys.readouterr().out)["selection"]["slices"] == ["year=2023", "year=2024", "year=2025"], \
        "joined with what the file said"
    assert louise.main(["share", "fetch", cid, "--root", str(fetcher), "--from", SHARER]) == 0
    assert json.loads(capsys.readouterr().out)["selection"]["slices"] == "all", "no --slice: every slice"
    with open(held, "rb") as f:
        assert tomllib.load(f)["slices"] == "all"


def test_show_lists_the_snapshots_and_the_holdings(node, fetcher, capsys):  # noqa: F811
    from app import louise
    codex, name, d, snap = a_shard(node)
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=TestClient(api.app),
                      key=OURS, own_key=OUR_KEY)
    assert louise.main(["hold", "collection", "show", out["name"], "--root", str(fetcher)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["snapshots"] == [{"snapshot_id": "2026-08", "hash": snap.hash, "signatures": 1,
                                   "held": False}], "the snapshot is held, its record members are not yet"
    assert shown["holdings"] == {"slices": [], "kinds": [], "members": 0}
    assert louise.main(["hold", "collection", "show", name, "--root", str(node)]) == 0
    sharer = json.loads(capsys.readouterr().out)
    assert sharer["snapshots"][0]["held"] is True and sharer["holdings"]["slices"] == ["year=2023"]


def test_sidecars_merge_by_union_and_a_bad_signature_is_dropped():
    h = "sha256:" + "7" * 64
    a = sn.sidecar_add(None, h, KEY, now=NOW)
    b = sn.sidecar_add(None, h, PEER, now="2026-09-09T00:00:00Z")
    forged = {**b, "signatures": b["signatures"] + [{"key": OTHER_KEY, "time": NOW, "sig": "00" * 64}]}
    merged = fetch.merge_sidecars(a, forged, h)
    assert [s["key"] for s in merged["signatures"]] == sorted([env.key_text(KEY.verify_key), PEER_KEY])
    assert fetch.merge_sidecars(a, {**b, "snapshot": "sha256:" + "8" * 64}, h)["signatures"] == a["signatures"], \
        "a sidecar of another snapshot has nothing to say about this one"
    assert fetch.merge_sidecars(None, None, h)["signatures"] == []


def test_an_encoder_naming_no_model_is_refused_before_anything_is_pulled(node, fetcher, monkeypatch, capsys):  # noqa: F811
    """07 §7.8 (amended 2026-10-09): `--encoder` on LocalAI names no model,
    so `--embed-model` must; never the space's label in its place. Refused
    before a peer is asked; given, the keys reach the file."""
    from app import louise
    asked = []
    monkeypatch.setattr(fetch, "fetch", lambda root, ref, **kw: asked.append(kw) or {})
    assert louise.main(["share", "fetch", CID_A, "--root", str(fetcher), "--from", SHARER,
                        "--encoder", "http://localai:8080"]) == 2
    assert "--embed-model" in capsys.readouterr().err and asked == []
    monkeypatch.undo()
    from app.hold.codex import CodexError
    with pytest.raises(CodexError, match="--embed-model"):
        fetch.fetch(fetcher, CID_A, encoder="http://localai:8080", key=KEY, own_key=OUR_KEY)


def test_the_held_file_of_a_fetch_carries_the_encoder_keys(node, fetcher):  # noqa: F811
    import tomllib
    from app import louise
    from tests.hold.conftest import a_root, pubmed
    codex = a_root(fetcher, pubmed())
    held = louise._write_held_file(fetcher, codex, "http://localai:8080", source="shard",
                                   api="vega", embed_model="vega-medcpt-article",
                                   query_model="vega-medcpt-query")
    with open(held, "rb") as f:
        enc = tomllib.load(f)["encoder"]
    assert enc == {"url": "http://localai:8080", "api": "vega",
                   "embed_model": "vega-medcpt-article", "query_model": "vega-medcpt-query"}


def test_a_slice_asked_for_is_named_as_the_snapshot_lists_it(node, fetcher, monkeypatch, capsys, tmp_path):  # noqa: F811
    """`--slice la` fetched no part, silently (2026-10-12): a name the pulled
    snapshot does not list is refused, with its listed spelling."""
    from app import louise
    from app.hold import snapshot as sn
    from app.share import seeder
    from tests.hold.conftest import a_root, pubmed
    codex = a_root(fetcher, pubmed())
    held = louise._write_held_file(fetcher, codex, None)
    monkeypatch.setattr(fetch, "fetch", lambda root, ref, **kw: {
        "file": str(held), "name": "pubmed", "collection_id": ref, "shard": str(tmp_path)})
    e = dict(root="r", records_root="r", records=1, chunks=1, chunks_root="c", tokens=1, bytes=1,
             first_id="1", last_id="2")
    snap = sn.Snapshot(collection_id=codex.collection_id, snapshot_id="2026-08", previous=None,
                       shards=(sn.Shard(slice="year=2023", n=1, **e),), delta=sn.Delta((), (), ()), files={})
    monkeypatch.setattr(seeder, "current_snapshot", lambda d: snap)
    args = ["share", "fetch", codex.collection_id, "--root", str(fetcher), "--from", SHARER, "--slice"]
    assert louise.main(args + ["2023"]) == 2
    assert "year=2023" in capsys.readouterr().err
    assert louise.main(args + ["year=2023"]) == 0


def test_fetch_pulls_the_attestations_the_sharer_holds_with_the_collection(node, fetcher):  # noqa: F811
    """04 §4.2 `pull attestations`, 2026-10-19: attestations travelled only
    as events, so a peer that joined after the node's checks could not
    contribute (no records-vs-origin for the snapshot) until the node's
    operator republished them by hand. `share fetch` pulls what the sharer
    holds, with the collection."""
    from app import copy_state
    from app.share import attest
    from tests.share.test_attest import an_attestation
    codex, name, d, snap = a_shard(node)
    held = [an_attestation(codex, OTHER, check="records-vs-origin", time_text="2026-10-11T21:13:00Z"),
            an_attestation(codex, OTHER, check="roots", time_text="2026-10-11T21:14:00Z")]
    for envelope in held:
        p = attest.attestation_path(node, name, codex, env.body_id(envelope["body"]))
        p.write_bytes(canonical_json(envelope))
    client = TestClient(api.app)
    out = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client,
                      key=OURS, own_key=OUR_KEY, encoder="http://models:1/upstream/x")
    got = out["attestations"]
    assert (got["asked"], got["seen"], got["kept"], got["unreachable"]) == (1, 4, 2, 0), \
        "the two naming the collection kept; a_shard's two version-1 bodies name none and are not"
    fname = out["name"]
    for envelope in held:
        p = attest.attestation_path(fetcher, fname, codex, env.body_id(envelope["body"]))
        assert json.loads(p.read_bytes()) == envelope
    bodies = copy_state.attestations(fetcher, fname, codex)
    assert any(b["check"] == "records-vs-origin" and b["result"] == "pass" for b in bodies), \
        "what `share contribute` wants is here (04 §4.5)"
    again = fetch.fetch(fetcher, codex.collection_id, from_address=SHARER, client=client,
                        key=OURS, own_key=OUR_KEY, encoder="http://models:1/upstream/x")
    assert again["attestations"]["kept"] == 0 and again["attestations"]["seen"] == 4, "held already: nothing twice"
    assert attest.newest_held(fetcher, fname, codex) == "2026-10-11T21:14:00Z"
    assert attest.since_for(fetcher, fname, codex) == "2026-10-11T20:14:00Z", "one interval of overlap"


def test_the_held_file_carries_the_embed_mode_asked_for_and_p2p_needs_sharing(node, fetcher):  # noqa: F811
    """07 §7.8, `share fetch --embed` / the Library's take (operator,
    2026-10-20): what this node's encoder spends is the taker's choice,
    `off` unless said; `p2p` publishes, so on a collection that is not
    shared it is written as `all`, which is how the worker runs it anyway."""
    from app import louise
    from tests.hold.conftest import a_root, pubmed
    codex = a_root(fetcher, pubmed())
    held = louise._write_held_file(fetcher, codex, "http://localai:8080", source="shard",
                                   embed_model="m", share=True, embed="p2p")
    assert 'embed = "p2p"' in held.read_text(encoding="utf-8")
    held.unlink()
    held = louise._write_held_file(fetcher, codex, "http://localai:8080", source="shard",
                                   embed_model="m", share=False, embed="p2p")
    assert 'embed = "all"' in held.read_text(encoding="utf-8")
    held.unlink()
    held = louise._write_held_file(fetcher, codex, "http://localai:8080", source="shard", embed_model="m")
    assert 'embed = "off"' in held.read_text(encoding="utf-8")
    assert louise.check_embed_mode("lazy") == "on-demand" and louise.check_embed_mode(None) == "off"
    with pytest.raises(Exception, match="embed is one of"):
        louise.check_embed_mode("sometimes")
