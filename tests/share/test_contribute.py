"""tests/share/test_contribute.py — `share contribute --once` (07 §7.3;
04 §4.5; 09 §9.7, §9.16): the preconditions refuse first; alone, a peer
leads every open shard, embeds it, bundles it under state/bundles/, passes
its own ladder, records the coverage and announces a `bundle` event; a
second pass finds nothing open; a heavier neighbour takes the shards it
leads; the seeder serves the bundles as members."""
import json
import time

import pytest

from app import config, db, embeddings
from app.hold import collection as col, envelope as env, records as rec
from app.share import attest, contribute, coverage as cov, members as mem, peers, rendezvous, seeder
from tests.hold.conftest import KEY
from tests.hold.test_admit import reference_and_envelope, space_embed
from tests.hold.test_vectors_build import _statement
from tests.hold.test_vectors_import import _member
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import OURS, OUR_KEY

pytestmark = pytest.mark.strict_encoder


def attested(root, codex, snapshot, *, check, key, time_text="2026-10-06T00:00:00Z", name="gutenberg"):
    body = {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "check": check,
            "seed": None, "shards": [], "result": "pass", "detail": {},
            "platform_image": "sha256:" + "1f" * 32, "encoder_engine": None, "encoder_weights": None,
            "key": env.key_text(key.verify_key), "time": time_text}
    envelope = env.sign(env.ATTESTATION, body, key)
    p = attest.attestation_path(root, name, codex, env.body_id(body))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps(envelope).encode())


@pytest.fixture
def ready(isolated_config, tmp_path, corpus_dir, monkeypatch):
    """A built collection this peer may contribute to: its own roots and
    chunks passed, another key's records-vs-origin exists, the calibration
    statement is there, and the calibration member is this encoder's."""
    monkeypatch.delenv("EMBED_URL", raising=False)
    # build_shard's three commands, with this encoder's calibration member
    # written BEFORE the snapshot lists it — so the member the sharer serves
    # is the one the snapshot names, and the one a fetcher judges with
    from app.hold import snapshot as sn
    from tests.hold.conftest import KEY as CREATOR, NOW, PLATFORM, fake_embed, fake_query_embed
    from tests.hold.test_template import recipe
    root = tmp_path / "collections"
    col.create_from_recipe(root, "gutenberg", recipe(), platform_image=PLATFORM, embed=fake_embed,
                           embed_query=fake_query_embed, name="gutenberg", key=CREATOR, now=NOW)
    codex = col.read(root, "gutenberg")
    ref, envp = reference_and_envelope(codex)
    _member(root, codex, ref, envp)
    rec.build(root, "gutenberg", corpus_dir=corpus_dir, log=lambda *_: None, records_per_shard=2)
    rec.write_validation(root, "gutenberg")
    snapshot = sn.create(root, "gutenberg", log=lambda *_: None)
    _statement(root, codex)
    local = rec.local_collection(root, "gutenberg", codex)
    con = db.connect(local)
    node_con = peers.connect()
    kw = dict(key=OURS, own_key=OUR_KEY,
              embed=lambda t: space_embed(t, dim=codex.space.dim),
              vectors_for=lambda ids: embeddings.vectors_by_chunk_id(ids, collection=local),
              write=lambda rows: embeddings.upsert_chunks(rows, collection=local, dtype=codex.space.dtype),
              con=con, node_con=node_con, batch=2)
    yield root, snapshot, codex, kw
    con.close()
    node_con.close()


def test_the_preconditions_refuse_before_any_gpu_is_spent(ready):
    root, snapshot, codex, kw = ready
    with pytest.raises(contribute.ContributeRefused) as e:
        contribute.once(root, "gutenberg", codex, live=[], **kw)
    text = str(e.value)
    assert "own roots" in text and "own chunks" in text and "records-vs-origin" in text
    assert e.value.exit_code == 2
    for check in ("roots", "chunks"):
        attested(root, codex, snapshot, check=check, key=OURS)
    with pytest.raises(contribute.ContributeRefused, match="records-vs-origin"):
        contribute.once(root, "gutenberg", codex, live=[], **kw)
    attested(root, codex, snapshot, check="records-vs-origin", key=PEER)
    assert contribute.preconditions(root, "gutenberg", codex, snapshot, own_key=OUR_KEY) == []
    (root / "gutenberg" / "state" / "calibration.json").unlink()
    from app.hold import runs
    reasons = contribute.preconditions(root, "gutenberg", codex, snapshot, own_key=OUR_KEY)
    if runs.applies(codex):
        assert reasons == [], "a version-3 collection's gate is per answer (03 §3.3), not a statement"
    else:
        assert any("calibration statement" in r for r in reasons)
    assert contribute.preconditions(root, "gutenberg", codex, None, own_key=OUR_KEY) == \
        ["no snapshot in the SHARD: nothing to embed for"]
    assert "no key" in " ".join(contribute.preconditions(root, "gutenberg", codex, snapshot, own_key=None))


def test_alone_a_peer_takes_every_open_shard_builds_bundles_accepts_and_announces_them(ready):
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    said: list[str] = []
    me = [rendezvous.Peer(OUR_KEY, 1000.0)]
    out = contribute.once(root, "gutenberg", codex, live=me, log=said.append, **kw)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    assert out["mine"] == shards, "alone: every open shard is this peer's"
    assert out["built"] > 0 and out["refused"] == [] and out["interrupted"] is False
    assert len(out["bundles"]) == len(shards) and out["accepted"] == [b["bundle_id"] for b in out["bundles"]]
    assert out["events"] == len(shards)
    for b in out["bundles"]:
        d = contribute.bundle_dir(root, "gutenberg", codex, b["shard"], b["bundle_id"])
        assert d.is_dir() and sorted(p.name for p in d.iterdir()) == sorted(mem.BUNDLE_FILES)
        assert b["tiers"] == {"T0": "pass", "T1": "pass", "T2": "pass", "T3": "skipped"}
        assert b["member"] == f"bundle-{codex.space_segment}-{d.name}"
    shown = cov.show(codex, snapshot, kw["con"], peers=me)
    assert shown["covered"] == shown["chunks"] > 0 and all(not s["open"] for s in shown["shards"])
    rows = cov.rows(kw["con"])
    assert all(r["source"] == "local" and r["accepted_at"] and r["producer"] == OUR_KEY for r in rows)
    assert len(rows) == len(shards)
    pending = peers.to_forward(kw["node_con"])
    assert len(pending) == len(shards) and all(p["hops"] == peers.HOPS for p in pending)
    payload = pending[0]["envelope"]["body"]["payload"]
    assert set(payload) == {"collection", "space_id", "snapshot_id", "shard", "uids", "bundle_id", "root",
                            "bytes", "files"}
    assert payload["collection"] == codex.collection_id and payload["bundle_id"] in out["accepted"]
    assert [f[0] for f in payload["files"]] == sorted(mem.BUNDLE_FILES), "the four files, in path order (09 §9.16)"
    assert any("accepted and announced" in s for s in said)
    # nothing left open: a second pass builds nothing and announces nothing
    again = contribute.once(root, "gutenberg", codex, live=me, **kw)
    assert again["mine"] == [] and again["built"] == 0 and again["events"] == 0
    assert len(peers.to_forward(kw["node_con"])) == len(shards)
    # the seeder serves the bundles as members, four torrents each
    class C:
        name, codex_ = "gutenberg", codex
    c = C()
    c.codex = codex
    entries = seeder.publish(c, snapshot, col.shard_dir_of(root, "gutenberg"),
                             root / "gutenberg" / "state", kw["con"])
    served = {e["member"] for e in entries if e["member"].startswith("bundle-")}
    assert served == {b["member"] for b in out["bundles"]}
    assert all(sum(1 for e in entries if e["member"] == m) == 4 for m in served)
    for e in entries:
        if e["member"].startswith("bundle-"):
            assert (root / "gutenberg" / "state" / "share" / snapshot.snapshot_id / e["member"]
                    / e["path"]).is_file()
    # the digest offers the bundles and claims the coverage (09 §9.9)
    from app import copy_state
    from app.share import gossip
    c.share = True
    c.manifest_path = root / "gutenberg" / "state" / "manifest.sqlite"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(copy_state, "of", lambda collection: copy_state.ALWAYS_IN_USE)
        (entry,) = gossip.entries(root, [c])
    offered = {m["name"] for m in entry["members"]}
    assert served <= offered and any(m.startswith("records-") for m in offered)
    assert entry["coverage"] == [{"shard": cov.shard_id(codex, s), "bundles": sorted(
        b["bundle_id"] for b in out["bundles"] if b["shard"] == s)} for s in shards]
    assert gossip.coverage_of(root, "nobody") == []
    # a neighbour's claim about one of our bundles is folded beside the row and counts for nothing
    claimed = out["bundles"][0]["bundle_id"]
    with peers.connect() as ncon:
        from app.share import messages
        d = messages.digest_body(key=PEER_KEY, address="http://peer.test", neighbours=[], benchmarks=[],
                                 collections=[{"collection_id": codex.collection_id,
                                               "coverage": [{"shard": cov.shard_id(codex, shards[0]),
                                                             "bundles": [claimed, "sha256:" + "e" * 64]}]}])
        peers.record_digest(ncon, d, now=time.time())
    assert gossip.fold_claims(root, [c], now=time.time()) == 2
    rows = {r["bundle_id"]: r for r in cov.rows(kw["con"])}
    assert rows[claimed]["claimed_by"] == [PEER_KEY] and rows[claimed]["accepted_at"]
    assert rows["sha256:" + "e" * 64]["accepted_at"] is None and rows["sha256:" + "e" * 64]["claimed_by"] == [PEER_KEY]
    assert cov.show(codex, snapshot, kw["con"], peers=me)["covered"] == shown["covered"], "a claim covers nothing"


def test_the_command_runs_a_pass_per_collection_under_the_lock(ready, monkeypatch, capsys):
    from app import identity, louise
    from app.bundles import keys as node_keys
    from app.hold import lock as lock_mod
    root, snapshot, codex, kw = ready
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    monkeypatch.setattr(louise, "_embedding_callables",
                        lambda root_, name, codex_, stop, holder=None: (kw["embed"], None))
    calls: list[dict] = []

    def fake_once(root_, name, codex_, **kwargs):
        calls.append(kwargs)
        return {"mine": [], "built": 0}
    monkeypatch.setattr(contribute, "once", fake_once)
    assert louise.main(["share", "contribute", "gutenberg", "--root", str(root), "--once"]) == 0
    text = capsys.readouterr().out
    out = json.loads(text[text.index("\n{") + 1:] if "\n{" in text else text)
    assert out == {"gutenberg": {"mine": [], "built": 0}} and len(calls) == 1
    assert calls[0]["own_key"] == OUR_KEY and calls[0]["live"] == [] and calls[0]["before_write"] is None
    assert lock_mod.holder(root / "gutenberg" / "state") is None, "released with the command"
    assert louise.main(["share", "contribute", "--root", str(root), "--once"]) == 2, \
        "no collection named outside the node's root"


def test_a_heavier_neighbour_leads_the_shards_the_hash_gives_it(ready):
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1000.0)]
    limit = cov.period_seconds(codex.work.per)
    expected = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards
                if rendezvous.leader(rendezvous.ShardWork(
                    cov.shard_id(codex, f"{s.slice}/{s.n:05d}"), s.tokens), live, limit).key == OUR_KEY]
    out = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert out["mine"] == expected, "only the shards rendezvous gives this peer"
    assert len(out["bundles"]) == len(expected)
    # 04 §4.5 amended 2026-10-20 (operator: "for the bounty list there is no
    # leader"): the ordering is an order of work, not an owner, and no grace
    # window hands a part on — a peer with capacity walks the open parts in
    # its own order (`work_order`). The other peer's parts stay first-ranked
    # to it at any time. (This branch asserted the old cascade; it first
    # ran when gutenberg's parser 10 moved the collection id, and with it
    # the hash, giving both parts to the other peer.)
    others = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards if f"{s.slice}/{s.n:05d}" not in expected]
    if others:
        opened = cov.opened_at(kw["con"])
        assert all(cov.shard_id(codex, sid) in opened for sid in others), "seen open: the first-open time"
        grace = codex.update.grace_days * 86400
        for at in (time.time() + grace - 60, time.time() + grace + 3 * 86400, time.time() + 2 * grace + 3 * 86400):
            assert contribute.shards_i_lead(codex, snapshot, kw["con"], me=OUR_KEY, live=live, now=at) == [], \
                "nothing cascades: the first in the ordering stays first"


def test_leaders_agrees_with_shards_i_lead_and_writes_nothing(ready):
    root, snapshot, codex, kw = ready
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1000.0)]
    con = kw["con"]
    before = dict(cov.opened_at(con))
    lead = contribute.leaders(codex, snapshot, con, live=live)
    assert dict(cov.opened_at(con)) == before, \
        "the embed stage's read: no first-open time written"
    mine = contribute.shards_i_lead(codex, snapshot, con, me=OUR_KEY, live=live)
    assert sorted(cov.shard_id(codex, f"{s.slice}/{s.n:05d}") for s in mine) == \
        sorted(sid for sid, k in lead.items() if k == OUR_KEY)
    assert set(lead.values()) <= {OUR_KEY, PEER_KEY}


def test_what_a_peer_already_holds_it_bundles_whoever_leads_the_shard(ready):
    """04 §4.5, amended 2026-10-19 (RULE 1): the node held 91 % of gutenberg's
    vectors under `embed = "all"` and announced none; every coverage map
    showed 0 % and a new peer re-embedded what the node had. A holder
    bundles what it holds for every open shard, before any GPU is spent,
    whoever rendezvous gives the shard to."""
    from app.hold import land as landing, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    # every vector embedded here already (the node before it had neighbours)
    built = vb.build(codex, shards=list(snapshot.shards),
                     records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
                     vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    assert built.chunks_written > 0
    # a neighbour so heavy that rendezvous gives it every shard
    heavy = rendezvous.Peer(PEER_KEY, 1e12)
    live = [rendezvous.Peer(OUR_KEY, 1000.0), heavy]
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    limit = cov.period_seconds(codex.work.per)
    assert all(rendezvous.leader(rendezvous.ShardWork(cov.shard_id(codex, sid), s.tokens), live, limit).key == PEER_KEY
               for sid, s in zip(shards, snapshot.shards)), "the fixture: another peer leads everything"
    embed_calls: list[int] = []
    real_embed = kw["embed"]

    def counted(texts):
        embed_calls.append(len(texts))
        return real_embed(texts)
    said: list[str] = []
    out = contribute.once(root, "gutenberg", codex, live=live, log=said.append, **{**kw, "embed": counted})
    assert out["mine"] == [] and out["built"] == 0, "this peer leads nothing and embeds nothing"
    assert out["held"] == shards and out["probed"] == len(shards), "every open shard probed; every one held, bundled"
    assert len(out["accepted"]) == len(out["bundles"]) == len(shards) and out["refused"] == []
    assert all(b["from"] == "held" for b in out["bundles"])
    from app.hold import export as exp
    assert embed_calls and all(n <= exp.CHALLENGE_N for n in embed_calls), \
        "the GPU saw the challenge rows and never a chunk (RULE 1)"
    assert out["events"] == len(shards) and len(peers.to_forward(kw["node_con"])) == len(shards), "announced"
    shown = cov.show(codex, snapshot, kw["con"], peers=live)
    assert shown["covered"] == shown["chunks"] and all(not s["open"] for s in shown["shards"]), \
        "the first bundle is the claim: the shards close for the leader too"
    assert any("no GPU spent" in s for s in said)
    # nothing open: the next pass probes nothing, bundles nothing
    again = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert again["probed"] == 0 and again["held"] == [] and again["events"] == 0


def test_a_bundle_s_row_and_event_are_dated_when_it_is_made_not_with_the_pass_s_clock(ready):
    """The node's holder pass ran for hours; every row and `bundle` event it
    made would have carried the pass's start (Perugia found the same stamp
    on the fetch beat, 2026-10-19). 04 §4.5's grace window reads the
    leader's last bundle from `accepted_at`."""
    from app.share import messages
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    stale = time.time() - 3600
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], now=stale, **kw)
    assert out["accepted"]
    for r in cov.rows(kw["con"]):
        assert r["accepted_at"] != messages.time_text(stale)
        assert abs(messages.parse_time(r["accepted_at"]) - time.time()) < 120
    for ev in peers.to_forward(kw["node_con"]):
        assert abs(messages.parse_time(ev["envelope"]["body"]["time"]) - time.time()) < 120


def test_a_peer_that_holds_nothing_of_a_shard_led_elsewhere_leaves_it_to_the_leader(ready):
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1e12)]
    out = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert out["mine"] == [] and out["probed"] == len(snapshot.shards) and out["held"] == []
    assert out["bundles"] == [] and out["events"] == 0 and out["built"] == 0, "nothing held, nothing embedded"


def test_a_bundle_is_announced_only_where_it_is_served(ready):
    """04 §4.5, amended 2026-10-19 (§7 of the review): the Perugia peer, under
    `share = false`, announced bundles nobody could fetch from it, and the
    node waited two minutes on each. Not serving: accepted, in the map, in
    the digest's coverage — no event."""
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    said: list[str] = []
    me = [rendezvous.Peer(OUR_KEY, 1000.0)]
    out = contribute.once(root, "gutenberg", codex, live=me, log=said.append, serving=False, **kw)
    n = len(snapshot.shards)
    assert len(out["accepted"]) == n and out["events"] == 0 and out["unannounced"] == n
    assert peers.to_forward(kw["node_con"]) == [], "no event"
    rows = cov.rows(kw["con"])
    assert len(rows) == n and all(r["accepted_at"] and r["source"] == "local" for r in rows), "accepted here"
    from app.share import gossip
    assert sum(len(c["bundles"]) for c in gossip.coverage_of(root, "gutenberg")) == n, "in the digest's coverage"
    assert any("does not serve" in s for s in said) and any("NOT announced" in s for s in said)


def test_the_command_announces_where_the_file_shares_and_the_mode_seeds(ready, monkeypatch):
    from app import louise, node_state
    root, snapshot, codex, kw = ready
    held = louise._held_file(root, "gutenberg")
    text = held.read_text(encoding="utf-8") if held.is_file() else f'codex = "{codex.collection_id}"\n'
    with peers.connect() as node_con:
        monkeypatch.setattr(node_state, "worker_state", lambda con=None: "acquire-only")
        held.write_text(text.replace("share = true\n", "").replace("share = false\n", "") + "share = false\n",
                        encoding="utf-8")
        assert louise._serves_here(root, "gutenberg", node_con) is False, "share = false"
        held.write_text(text.replace("share = true\n", "").replace("share = false\n", "") + "share = true\n",
                        encoding="utf-8")
        assert louise._serves_here(root, "gutenberg", node_con) is True, "acquire-only seeds (07 §7.8)"
        monkeypatch.setattr(node_state, "worker_state", lambda con=None: "paused")
        assert louise._serves_here(root, "gutenberg", node_con) is False, "paused seeds nothing"
        monkeypatch.setattr(node_state, "worker_state", lambda con=None: "running")
        assert louise._serves_here(root, "gutenberg", node_con) is True
        held.unlink()
        assert louise._serves_here(root, "gutenberg", node_con) is False, "no file: not shared"


def test_a_holder_leaves_a_shard_whose_bundle_a_neighbour_offers_to_the_fetch(ready):
    """One version where one exists (04 §4.5, amended 2026-10-19): with many
    holders joining, each bundling what it holds would make as many versions
    of every part. A shard whose bundle a live neighbour both claims and
    offers is left to the fetch beat; a claim nobody serves is not."""
    from app.hold import land as landing, vectors_build as vb
    from app.share import messages
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    vb.build(codex, shards=list(snapshot.shards),
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    assert len(shards) >= 2
    offered_sid, claimed_only_sid = shards[0], shards[1]
    now = time.time()
    files = [[f, "sha256:" + "a" * 64, 10] for f in sorted(mem.BUNDLE_FILES)]
    from app.hold import shard as shard_mod
    bid_offered, bid_claimed = "sha256:" + "b" * 64, "sha256:" + "c" * 64
    member = {"name": f"bundle-{codex.space_segment}-{'b' * 64}", "root": shard_mod.member_root(
        tuple(shard_mod.MemberFile(p, r, b) for p, r, b in files)), "bytes": 40, "files": files}
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "g",
             "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [member],
             "coverage": [{"shard": cov.shard_id(codex, offered_sid), "bundles": [bid_offered]},
                          {"shard": cov.shard_id(codex, claimed_only_sid), "bundles": [bid_claimed]}]}
    digest = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                  benchmarks=[], collections=[entry], now=now)
    peers.record_digest(kw["node_con"], {**digest, "peer_port": 6881}, now=now)
    assert cov.fold_claims(kw["con"], codex, [digest]) == 2
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1e12)]
    said: list[str] = []
    out = contribute.once(root, "gutenberg", codex, live=live, log=said.append, **kw)
    assert out["left_to_fetch"] == [offered_sid], "claimed AND offered: the fetch beat's"
    assert out["held"] == [s for s in shards if s != offered_sid], \
        "a claim nobody serves does not stop the holder; the rest is bundled"
    assert out["probed"] == len(shards) - 1 and any("left to the fetch beat" in s for s in said)
    # once this peer holds (or refused) that bundle, the shard is its own business again
    cov.record(kw["con"], bundle_id=bid_offered, shard=cov.shard_id(codex, offered_sid), producer=PEER_KEY,
               uids=[], chunks=0, tiers={"T0": "fail"}, source=cov.FETCHED, accepted_at=None)
    out = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert out["left_to_fetch"] == [] and out["held"] == [offered_sid]


def test_a_part_completed_by_its_leader_gets_a_bundle_of_the_new_records_only(ready):
    """Operator, 2026-10-19: minimum redundancy. The node's mine phase
    re-shipped the 6,407 chunks of a holder bundle beside the 2,942 it had
    just embedded. A further export of a part leaves out the records this
    producer's accepted bundles already claim."""
    import json
    from app.hold import land as landing, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    assert len(records) >= 2, "the fixture: two records per shard"
    # held: the first record embedded here already, as under embed = "all"
    vb.build(codex, shards=[shard], records_for=lambda s: records[:1], vectors_for=kw["vectors_for"],
             embed=kw["embed"], write=kw["write"], batch=2)
    heavy = rendezvous.Peer(PEER_KEY, 1e12)
    first = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0), heavy], **kw)
    assert sid in first["held"]
    (b1,) = [b for b in first["bundles"] if b["shard"] == sid]
    claimed = contribute.claimed_uids(root, "gutenberg", codex, kw["con"], sid, own_key=OUR_KEY)
    assert claimed == {records[0]["uid"]}
    # then this peer leads the part: it embeds the rest and bundles only that
    second = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)
    assert sid in second["mine"] and second["built"] > 0
    (b2,) = [b for b in second["bundles"] if b["shard"] == sid]
    p1 = json.loads((contribute.bundle_dir(root, "gutenberg", codex, sid, b1["bundle_id"]) / "possession.json").read_bytes())
    p2 = json.loads((contribute.bundle_dir(root, "gutenberg", codex, sid, b2["bundle_id"]) / "possession.json").read_bytes())
    assert set(p1).isdisjoint(p2) and set(p1) | set(p2) == {r["uid"] for r in records}, \
        "the second bundle carries the new records only; together they cover the part once"
    view = {v["shard"]: v for v in cov.show(codex, snapshot, kw["con"])["shards"]}[cov.shard_id(codex, sid)]
    assert view["covered"] == view["chunks"] == b1["chunks"] + b2["chunks"] and not view["open"]
    # nothing new: a third pass bundles nothing for the part and says so
    said: list[str] = []
    third = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], log=said.append, **kw)
    assert sid not in third["mine"] and not any(b["shard"] == sid for b in third["bundles"])


def test_a_leader_leaves_a_led_shard_whose_bundle_a_neighbour_offers_to_the_fetch(ready):
    """Perugia, 2026-10-19: 632 of the 636 parts it led had a neighbour's
    bundle offered (the node's holder pass) and were re-embedded — the offer
    check ran only for shards led elsewhere. A led shard a live neighbour
    both claims and offers is the fetch beat's too (RULE 1); one with a claim
    nobody serves, or none, is built."""
    from app.hold import shard as shard_mod
    from app.share import messages
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    assert len(shards) >= 2
    offered_sid = shards[0]
    now = time.time()
    files = [[f, "sha256:" + "a" * 64, 10] for f in sorted(mem.BUNDLE_FILES)]
    bid = "sha256:" + "b" * 64
    member = {"name": f"bundle-{codex.space_segment}-{'b' * 64}", "root": shard_mod.member_root(
        tuple(shard_mod.MemberFile(p, r, b) for p, r, b in files)), "bytes": 40, "files": files}
    entry = {"collection_id": codex.collection_id, "lineage_id": codex.collection_id, "title": "g",
             "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "codex_hash": "sha256:" + "2" * 64,
             "license": "green", "privacy": "public", "members": [member],
             "coverage": [{"shard": cov.shard_id(codex, offered_sid), "bundles": [bid]}]}
    digest = messages.digest_body(key=PEER_KEY, address="http://10.0.0.7:30042", neighbours=[],
                                  benchmarks=[], collections=[entry], now=now)
    peers.record_digest(kw["node_con"], {**digest, "peer_port": 6881}, now=now)
    assert cov.fold_claims(kw["con"], codex, [digest]) == 1
    # this peer so heavy that it leads every shard; the neighbour is live
    live = [rendezvous.Peer(OUR_KEY, 1e12), rendezvous.Peer(PEER_KEY, 1.0)]
    embed_calls: list[int] = []
    real_embed = kw["embed"]

    def counted(texts):
        embed_calls.append(len(texts))
        return real_embed(texts)
    said: list[str] = []
    out = contribute.once(root, "gutenberg", codex, live=live, log=said.append, **{**kw, "embed": counted})
    assert out["mine"] == shards, "the fixture: this peer leads everything"
    assert out["left_to_fetch"] == [offered_sid], "led here, offered there: the fetch beat's, not the GPU's"
    assert {b["shard"] for b in out["bundles"]} == set(shards) - {offered_sid} and out["built"] > 0
    assert not any(b["shard"] == offered_sid for b in out["bundles"]) and any("left to the fetch beat" in s for s in said)
    # once the offered bundle is held (or refused) here, the shard is this peer's again
    cov.record(kw["con"], bundle_id=bid, shard=cov.shard_id(codex, offered_sid), producer=PEER_KEY,
               uids=[], chunks=0, tiers={"T0": "fail"}, source=cov.FETCHED, accepted_at=None)
    again = contribute.once(root, "gutenberg", codex, live=live, **kw)
    assert again["left_to_fetch"] == [] and again["mine"] == [offered_sid] and again["built"] > 0
    assert [b["shard"] for b in again["bundles"]] == [offered_sid]


def test_the_build_marks_the_documents_it_completed_embedded_and_the_reconcile_catches_up(ready, monkeypatch, capsys):
    """München, 2026-10-19: 372 documents on the node had every chunk in
    Lance and `embed_done = 0` — the build wrote vectors and never touched
    the manifest. After a shard's build the completed records are marked,
    after the write; `dev mark-embedded` marks what an older build left."""
    from app import identity, louise
    from app.bundles import keys as node_keys
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    con = kw["con"]
    uids = [r["uid"] for r in con.execute("SELECT uid FROM documents WHERE in_scope=1").fetchall()]
    assert uids and all(r["embed_done"] == 0 for r in con.execute("SELECT embed_done FROM documents").fetchall())
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)
    assert out["built"] > 0 and out["marked"] == len(uids)
    rows = {r["uid"]: r for r in con.execute("SELECT uid, embed_done, embed_version FROM documents").fetchall()}
    assert all(rows[u]["embed_done"] == 1 and rows[u]["embed_version"] == config.EMBED_VERSION for u in uids)
    # an older build's documents: vectors there, rows unmarked — the reconcile
    con.execute("UPDATE documents SET embed_done=0, updated_at=1")
    monkeypatch.setattr(node_keys, "load_signing_key", lambda path=None: OURS)
    monkeypatch.setattr(identity, "own", lambda: OUR_KEY)
    assert louise.main(["dev", "mark-embedded", "gutenberg", "--root", str(root), "--dry-run"]) == 0
    text = capsys.readouterr().out
    dry = json.loads(text[text.index("{"):])
    assert dry["complete_unmarked"] == dry["candidates"] == len(uids) and dry["marked"] == 0 and dry["dry_run"]
    assert dry["shards"] == len(snapshot.shards) and dry["rows_scanned"] > 0
    assert all(r["embed_done"] == 0 for r in con.execute("SELECT embed_done FROM documents").fetchall())
    assert louise.main(["dev", "mark-embedded", "gutenberg", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    got = json.loads(text[text.index("{"):])
    assert got["marked"] == got["complete_unmarked"] == len(uids) and got["dry_run"] is False
    assert all(r["embed_done"] == 1 for r in con.execute("SELECT embed_done FROM documents").fetchall())
    # nothing left: the second run marks nothing
    assert louise.main(["dev", "mark-embedded", "gutenberg", "--root", str(root)]) == 0
    text = capsys.readouterr().out
    assert json.loads(text[text.index("{"):])["candidates"] == 0, "nothing unmarked: the table is not even scanned"


def test_the_held_path_chunks_only_the_documents_it_holds_vectors_for_and_stops_between_them(ready, monkeypatch):
    """Perugia, 2026-10-19: the held path chunked every document of every
    probed part in pure Python before knowing whether any row was unclaimed
    — 9.5 minutes a pass with nothing to bundle, and a SIGTERM waited it
    out. The unclaimed documents are asked of the vector table by book id
    first; only those with a vector are chunked; a stop set between two
    documents ends the export with nothing written."""
    import threading
    from app import embeddings, layers as layers_mod
    from app.hold import land as landing, records as rec, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    local = rec.local_collection(root, "gutenberg", codex)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    held_sid, bare_sid = shards[0], shards[1]
    held_records = landing.records_for(root, "gutenberg", snapshot, held_sid, None)
    # vectors for the first record of the first part only
    vb.build(codex, shards=[snapshot.shards[0]], records_for=lambda s: held_records[:1],
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    books = embeddings.books_with_vectors([int(r["native_id"]) for s in shards
                                           for r in landing.records_for(root, "gutenberg", snapshot, s, None)],
                                          collection=local)
    assert books == {int(held_records[0]["native_id"])}, "one book has vectors; the lookup names it, no chunk derived"
    assert embeddings.books_with_vectors([], collection=local) == set()
    chunked: list[str] = []
    real = layers_mod.chunks_for_document

    def counted(codex_, row, sections):
        chunked.append(row["uid"])
        return real(codex_, row, sections)
    monkeypatch.setattr(layers_mod, "chunks_for_document", counted)
    heavy = rendezvous.Peer(PEER_KEY, 1e12)
    held_books = lambda ids: embeddings.books_with_vectors(ids, collection=local)   # noqa: E731
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0), heavy],
                          held_books=held_books, **kw)
    assert out["held"] == [held_sid] and out["probed"] == len(shards)
    assert set(chunked) <= {held_records[0]["uid"]}, "the bare part's books were never chunked; nor the other record's"
    # a stop set once the first document is chunked: the export ends between documents, nothing written
    chunked.clear()
    stop = threading.Event()
    kw["con"].execute("DELETE FROM coverage")
    for d in (root / "gutenberg" / "state" / "bundles").glob("*"):
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    vb.build(codex, shards=[snapshot.shards[0]], records_for=lambda s: held_records,
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)

    def stopping(codex_, row, sections):
        stop.set()
        return real(codex_, row, sections)
    monkeypatch.setattr(layers_mod, "chunks_for_document", stopping)
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0), heavy],
                          held_books=held_books, stop=stop, **kw)
    assert out["interrupted"] and out["held"] == [] and out["bundles"] == []
    assert not list((root / "gutenberg" / "state" / "bundles").glob(".export-*")), "the scratch is gone"
    assert cov.rows(kw["con"]) == []


def test_a_publish_only_pass_bundles_the_led_shards_it_holds_and_builds_nothing(ready):
    """The worker's publisher (07 §7.8 "all p2p", 2026-10-19): under `embed =
    "p2p"` the embed stage writes the led shards' vectors; a pass with
    `build=False` bundles what is held — led or not — and builds nothing. A
    led shard with no vector yet is awaiting the embed stage; the GPU sees
    only each bundle's challenge rows."""
    from app.hold import export as exp, land as landing, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    # the embed stage has finished the first shard and not touched the rest
    first = snapshot.shards[0]
    vb.build(codex, shards=[first],
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    embed_calls: list[int] = []
    real_embed = kw["embed"]

    def counted(texts):
        embed_calls.append(len(texts))
        return real_embed(texts)

    def never_write(rows):
        raise AssertionError("a publish-only pass writes no vector")
    alone = [rendezvous.Peer(OUR_KEY, 1000.0)]
    local = db.connect  # noqa: F841 — the fixture's connection is kw["con"]
    out = contribute.once(root, "gutenberg", codex, live=alone, build=False,
                          held_books=lambda ids: embeddings.books_with_vectors(ids, collection=rec.local_collection(root, "gutenberg", codex)),
                          **{**kw, "embed": counted, "write": never_write})
    assert out["mine"] == shards, "alone, this peer leads everything"
    assert out["built"] == 0 and out["held"] == [shards[0]], "the finished shard bundled, nothing built"
    assert out["awaiting"] == shards[1:], "the rest await the embed stage"
    assert len(out["accepted"]) == len(out["bundles"]) == 1 and out["bundles"][0]["from"] == "held"
    assert embed_calls and all(n <= exp.CHALLENGE_N for n in embed_calls), "challenge rows only"
    assert out["events"] == 1, "announced"
    # the next pass: the first shard is closed by its bundle; nothing new to bundle
    again = contribute.once(root, "gutenberg", codex, live=alone, build=False,
                            **{**kw, "embed": counted, "write": never_write})
    assert again["held"] == [] and again["bundles"] == [] and shards[0] not in again["mine"]
    assert again["awaiting"] == shards[1:]


def test_the_job_order_is_the_bounty_list_as_this_peer_takes_it(ready):
    """04 §4.5 (amended 2026-10-20): `work_order` gives this peer's rank per
    open part; `job_order` the parts it may take, in the order it takes them
    — rank 1 first, within a rank by its own score, highest first — without
    the parts another producer is visibly on. The embed stage works the
    first two of these at a time (operator: "up to 2 jobs at a time")."""
    from app.share import messages
    root, snapshot, codex, kw = ready
    con = cov.ensure_schema(kw["con"])
    now = time.time()
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1000.0)]
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    sids = [cov.shard_id(codex, s) for s in shards]
    tokens = {cov.shard_id(codex, f"{s.slice}/{s.n:05d}"): int(s.tokens) for s in snapshot.shards}
    assert len(sids) >= 2
    ranks = contribute.work_order(codex, snapshot, con, me=OUR_KEY, live=live, now=now)
    order = contribute.job_order(codex, snapshot, con, me=OUR_KEY, live=live, now=now)
    assert set(ranks) == set(sids) == set(order) and set(ranks.values()) <= {1, 2}, "every part open, two eligible peers"
    me = live[0]
    assert order == sorted(sids, key=lambda s: (ranks[s], -rendezvous.score(me, rendezvous.ShardWork(s, tokens[s])), s))
    assert [ranks[s] for s in order] == sorted(ranks[s] for s in order), "rank 1 first"
    # the neighbour's bundle for the first part, accepted just now: visibly on it, left aside
    cov.record(con, bundle_id="sha256:" + "b" * 64, shard=order[0], producer=PEER_KEY, uids=[], chunks=1,
               tiers={"T0": "pass"}, source=cov.FETCHED, accepted_at=messages.time_text(now))
    ranks2 = contribute.work_order(codex, snapshot, con, me=OUR_KEY, live=live, now=now)
    order2 = contribute.job_order(codex, snapshot, con, me=OUR_KEY, live=live, now=now)
    assert ranks2[order[0]] is None and order2 == order[1:]
    # alone, every open part is this peer's, in its own order
    alone = contribute.job_order(codex, snapshot, con, me=OUR_KEY, live=live[:1], now=now)
    assert set(alone) == set(sids) and alone == sorted(
        sids, key=lambda s: -rendezvous.score(me, rendezvous.ShardWork(s, tokens[s])))


def test_a_part_with_another_producers_bundle_accepted_but_not_here_is_left_to_the_fetch_not_re_signed(ready):
    """Perugia, 2026-10-20 (en/01192): rows accepted on the compare path had
    no files; `claimed_uids` read nothing from them, so the held path
    re-signed the node's rows as this peer's (963 chunks over a bundle of
    6,924). Another producer's accepted row without its files leaves the
    part to the fetch, which takes the bundle again; this peer's own
    fileless rows claim nothing, as before — nobody else may hold them."""
    from app.hold import land as landing, vectors_build as vb
    from app.share import messages
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    vb.build(codex, shards=[shard], records_for=lambda s: records[:1], vectors_for=kw["vectors_for"],
             embed=kw["embed"], write=kw["write"], batch=2)
    now = time.time()
    ghost = "sha256:" + "e" * 64
    cov.record(kw["con"], bundle_id=ghost, shard=cov.shard_id(codex, sid), producer=PEER_KEY, uids=[], chunks=1,
               tiers={"T0": "pass"}, source=cov.FETCHED, accepted_at=messages.time_text(now))
    own_ghost = "sha256:" + "d" * 64
    cov.record(kw["con"], bundle_id=own_ghost, shard=cov.shard_id(codex, sid), producer=OUR_KEY, uids=[], chunks=1,
               tiers={"T0": "pass"}, source=cov.LOCAL, accepted_at=messages.time_text(now))
    assert contribute.unreadable_bundles(root, "gutenberg", codex, kw["con"], sid, own_key=OUR_KEY) == [ghost]
    said: list[str] = []
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], log=said.append, **kw)
    assert sid in out["unreadable"] and not any(b["shard"] == sid for b in out["bundles"])
    assert any("left to the fetch, which takes them again" in s and sid in s for s in said)
    # the other parts are bundled as before
    assert out["bundles"] or len(snapshot.shards) == 1


def test_an_own_export_the_ladder_refuses_is_set_aside_not_served_and_not_exported_again_for_a_day(ready, monkeypatch):
    """München, 2026-10-20: the node's publisher re-exported two parts its own
    ladder refused at T0 every pass — 26 copies in ten hours — and every
    copy sat in state/bundles, which the seeder serves and the digest offers
    whole, so "not announced" was not true and Perugia fetched them all. A
    refused export is set aside under state/refused-bundles, the part is not
    exported again for REFUSED_RETRY_S, and refused copies left under
    state/bundles by earlier code are swept aside at the next pass."""
    import dataclasses
    from app.hold import admit as adm
    from app.share import members as mem
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    state = root / "gutenberg" / "state"
    shards = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards]
    assert len(shards) >= 2
    # a refused copy of an EARLIER code's making, still under state/bundles: served until now
    old_bid = "sha256:" + "c" * 64
    old_dir = contribute.bundle_dir(root, "gutenberg", codex, shards[1], old_bid)
    old_dir.mkdir(parents=True)
    for f in mem.BUNDLE_FILES:
        (old_dir / f).write_bytes(b"x")
    cov.record(kw["con"], bundle_id=old_bid, shard=cov.shard_id(codex, shards[1]), producer=OUR_KEY, uids=[],
               chunks=0, tiers={"T0": "fail"}, source=cov.LOCAL, accepted_at=None)
    assert mem.bundle_dirs(state, codex.space_segment) == [old_dir], "served and offered, as on the node"
    real = contribute._own_ladder

    def refusing(root_, name_, codex_, snapshot_, where):
        report, header = real(root_, name_, codex_, snapshot_, where)
        tiers = tuple(adm.Tier("T0", adm.FAIL, "forced by the test") if t.tier == "T0" else t for t in report.tiers)
        return dataclasses.replace(report, tiers=tiers), header
    monkeypatch.setattr(contribute, "_own_ladder", refusing)
    now = time.time()
    said: list[str] = []
    out = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], now=now,
                          log=said.append, **kw)
    assert out["set_aside"] == 1 and not old_dir.exists() \
        and contribute.refused_dir(root, "gutenberg", codex, shards[1], old_bid).is_dir()
    assert out["refused_recently"] == [shards[1]], "its refusal is fresh: not exported again"
    assert out["bundles"] and out["refused"] and not out["accepted"]
    assert mem.bundle_dirs(state, codex.space_segment) == [], "a refused export is not among the served bundles"
    assert len(list((state / contribute.REFUSED_DIRNAME).rglob("header.json"))) == len(out["bundles"]) + 1, \
        "plus the swept one"
    # the seeder owns state/share: a sync removes every directory there but the
    # torrents and the current snapshot's (seeder.sync) — the copies set aside
    # must survive it, or the day's hold has no mtime to read (the node, 678450f)
    for old in (state / seeder.SHARE_DIR).iterdir() if (state / seeder.SHARE_DIR).is_dir() else []:
        if old.is_dir() and old.name not in (seeder.TORRENTS_DIR, snapshot.snapshot_id):
            import shutil
            shutil.rmtree(old)
    assert len(list((state / contribute.REFUSED_DIRNAME).rglob("header.json"))) == len(out["bundles"]) + 1, \
        "the set-aside copies are outside the seeder's directory"
    assert not contribute.refused_dir(root, "gutenberg", codex, shards[1], old_bid).is_relative_to(
        state / seeder.SHARE_DIR) and not contribute.refused_dir(
        root, "gutenberg", codex, shards[1], old_bid).is_relative_to(state / "bundles")
    rows = [r for r in cov.rows(kw["con"]) if r["source"] == cov.LOCAL]
    assert rows and all(r["accepted_at"] is None and r["tiers"]["T0"] == "fail" for r in rows)
    assert any("set aside, not served, not announced" in s for s in said)
    # the next pass within the day: nothing exported for those parts, said once per part
    again = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], now=now + 600,
                            log=said.append, **kw)
    assert again["bundles"] == [] and set(again["refused_recently"]) == set(shards)
    assert any("not exported again for 24 h" in s for s in said)
    assert len([r for r in cov.rows(kw["con"]) if r["source"] == cov.LOCAL]) == len(rows), "no new copy"
    # a day later: tried again (and refused again here, the ladder being forced)
    later = contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)],
                            now=now + contribute.REFUSED_RETRY_S + 60, **kw)
    assert later["bundles"] and later["refused_recently"] == []


PREDECESSOR = "sha256:" + "d9" * 32


def _predecessor(root, codex, con, sid, possession, *, bundle_id=None, files=True, accepted=True,
                 producer=PEER_KEY):
    """A carry predecessor's accepted row, UNKEYED under its own collection's
    shard id (app/hold/carry.py, 2026-10-22), with its bundle's possession
    file where the row's path names it — and the marker naming it."""
    from app.share import contribute as c
    bundle_id = bundle_id or "sha256:" + format(len(possession) + 7, "064x")
    uids = sorted(possession, key=c._uid_key)
    cov.record(con, bundle_id=bundle_id, shard=f"{PREDECESSOR}/{codex.space_segment}/{sid}", producer=producer,
               uids=uids, chunks=len(uids), tiers={t: "pass" for t in cov.TIERS}, source=cov.FETCHED,
               accepted_at="2026-10-20T00:00:00Z" if accepted else None)
    if files:
        d = c.bundle_dir(root, "gutenberg", codex, sid, bundle_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "possession.json").write_text(json.dumps(possession), encoding="utf-8")
    marker = root / "gutenberg" / "state" / "carry.json"
    marker.write_text(json.dumps({"done": True, "same_space": True, "predecessors": [PREDECESSOR]}),
                      encoding="utf-8")
    return bundle_id


def test_a_predecessor_s_accepted_rows_claim_by_range_and_content_hash(ready):
    """2026-10-22, gutenberg p9 → p10: the predecessor's rows are read by
    SLICE and uid range — the part numbers moved with the re-cut, and a
    bundle may span two new parts — and a uid is claimed only where the
    bundle's possession names it with the record's own content_hash."""
    from app.hold import land as landing
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    assert len(records) >= 2
    first, second = records[0], records[1]
    # filed under ANOTHER part number of the same slice (the re-cut), and spanning past this part
    _predecessor(root, codex, kw["con"], f"{shard.slice}/{shard.n + 7:05d}",
                 {first["uid"]: first["content_hash"], second["uid"]: "0" * 32,      # changed since: not claimed
                  "gutenberg:999999999": "f" * 32})
    claimed = contribute.claimed_uids(root, "gutenberg", codex, kw["con"], sid, records=records)
    assert claimed == {first["uid"]}
    assert contribute.claimed_uids(root, "gutenberg", codex, kw["con"], sid) == frozenset(), \
        "without the records nothing is read from a predecessor"
    other = next(s for s in snapshot.shards if s.slice != shard.slice) if len({s.slice for s in snapshot.shards}) > 1 else None
    if other is not None:
        osid = f"{other.slice}/{other.n:05d}"
        orecs = landing.records_for(root, "gutenberg", snapshot, osid, None)
        assert not (contribute.claimed_uids(root, "gutenberg", codex, kw["con"], osid, records=orecs)
                    & {first["uid"]}), "another slice's part reads none of it"


def test_a_fileless_foreign_row_claims_its_unchanged_records_but_is_never_coverage(ready):
    """A predecessor's row of ANOTHER producer without files (the compare
    path) says only that this peer held vectors then, not whose: its
    records are claimed (Perugia, en/01192, 2026-10-20, re-signed the node's
    rows through such a row), yet it covers nothing — no possession here to
    count. Never unreadable: nothing stalls."""
    from app.hold import land as landing
    from app.share import possession
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    _predecessor(root, codex, kw["con"], sid, {r["uid"]: r["content_hash"] for r in records},
                 files=False, bundle_id="sha256:" + "f1" * 32)
    assert contribute.claimed_uids(root, "gutenberg", codex, kw["con"], sid, own_key=OUR_KEY,
                                   records=records) == {r["uid"] for r in records}
    assert possession.index(root, "gutenberg", codex, kw["con"]) == {}, "counted as coverage of nothing"
    assert contribute.unreadable_bundles(root, "gutenberg", codex, kw["con"], sid, own_key=OUR_KEY) == []


def test_any_producer_s_predecessor_bundle_covers_its_unchanged_records(ready):
    """03 §3.5: a bundle is admissible by what it was computed from — the
    collection and snapshot in its header gate nothing. So this peer's own
    predecessor bundle covers its records as well as another producer's:
    claimed, never bundled again (operator, 2026-10-22: "once a bundle is
    made and published, the ownership is of the community")."""
    from app.hold import land as landing
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    _predecessor(root, codex, kw["con"], sid, {r["uid"]: r["content_hash"] for r in records}, producer=OUR_KEY)
    assert contribute.claimed_uids(root, "gutenberg", codex, kw["con"], sid, own_key=OUR_KEY,
                                   records=records) == {r["uid"] for r in records}


def test_a_bundle_filed_only_under_an_admitted_segment_is_read(ready):
    """The A1f twin filed 1,743 of the node's gutenberg bundles under the
    admitted segment 5ac3c581, not the space's own; a fixed path read them as
    missing (München's first count, 2026-10-22). Possession is read through
    `bundle_dir`, every admitted segment."""
    import types
    from app.hold import land as landing
    from app.share import possession
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    bid = _predecessor(root, codex, kw["con"], sid, {r["uid"]: r["content_hash"] for r in records})
    base = root / "gutenberg" / "state" / "bundles"
    own_dir = contribute.bundle_dir(root, "gutenberg", codex, sid, bid)
    other = base / "5ac3c5810f2cd56a" / sid / own_dir.name
    other.parent.mkdir(parents=True, exist_ok=True)
    own_dir.rename(other)                             # filed only under the admitted segment
    plain = possession.index(root, "gutenberg", codex, kw["con"])
    assert plain == {}, "this Codex admits no other segment: not found"
    admitting = types.SimpleNamespace(**{k: getattr(codex, k) for k in ("collection_id", "space_segment")},
                                      admitted_segments=(codex.space_segment, "5ac3c5810f2cd56a"))
    possession._memo.clear()
    got = possession.index(root, "gutenberg", admitting, kw["con"])
    assert set(got) == {r["uid"] for r in records}


def test_the_view_counts_a_predecessor_s_coverage_by_possession(ready):
    """Re-minted in the same space, a part's coverage is counted BY
    POSSESSION: the predecessor's bundle, filed under its own shard key,
    covers this collection's part — its records' chunks, by the manifest."""
    from app.hold import land as landing
    from app.share import counting
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    _predecessor(root, codex, kw["con"], f"{shard.slice}/{shard.n + 3:05d}",
                 {r["uid"]: r["content_hash"] for r in records})
    union = counting.view_kwargs(root, "gutenberg", codex, kw["con"])["union"]
    uids = [r["uid"] for r in records]
    expect = kw["con"].execute(f"SELECT COALESCE(SUM(chunks), 0) FROM documents WHERE uid IN "
                               f"({','.join('?' * len(uids))})", uids).fetchone()[0]
    assert union[cov.shard_id(codex, sid)] == min(int(shard.chunks), int(expect))
    assert all(v == 0 for k, v in union.items() if k != cov.shard_id(codex, sid)), "nothing else covered"


def test_a_possession_count_that_fails_is_no_figure_never_the_rows_undercount(ready, monkeypatch):
    """2026-10-25: a cache-file race made `possession.covered` raise, and the
    view fell back to the rows: 114,154 of 10,471,327 chunks covered for
    10,164,506 — a re-minted collection's rows hold only its own bundles.
    Now the caller gets `CoverageUnknown`; the publish pass defers on it."""
    import pytest
    from app.hold import land as landing
    from app.share import counting, possession
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    _predecessor(root, codex, kw["con"], f"{shard.slice}/{shard.n + 3:05d}",
                 {r["uid"]: r["content_hash"] for r in records})

    def raced(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'x.json.tmp'")
    monkeypatch.setattr(possession, "covered", raced)
    with pytest.raises(counting.CoverageUnknown, match="could not be counted by possession.*FileNotFoundError"):
        counting.view_kwargs(root, "gutenberg", codex, kw["con"])
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    with pytest.raises(counting.CoverageUnknown):
        contribute.once(root, "gutenberg", codex, live=[rendezvous.Peer(OUR_KEY, 1000.0)], **kw)


def test_a_re_minted_collection_with_no_snapshot_yet_counts_its_rows(ready, monkeypatch):
    from app.hold import seed
    from app.hold.codex import CodexError
    from app.share import counting, possession
    root, snapshot, codex, kw = ready
    monkeypatch.setattr(contribute, "predecessors", lambda root, name: ["sha256:" + "aa" * 32])

    def none_yet(root, name, snapshot_id=None):
        raise CodexError(f"{name} holds no snapshot: there are no parts to name")
    monkeypatch.setattr(seed, "latest_snapshot", none_yet)
    monkeypatch.setattr(possession, "covered", lambda *a, **k: (_ for _ in ()).throw(AssertionError("asked")))
    view = counting.view_kwargs(root, "gutenberg", codex, kw["con"])
    assert view["union"] == counting.union_counts(root, "gutenberg", codex, cov.rows(kw["con"]))


def test_the_held_path_signs_none_of_a_predecessor_s_rows_and_never_counts_them_as_coverage(ready):
    """The node's held path, after the same-space re-mint: the rows it had
    landed from another producer's bundles in the predecessor are not
    bundled and signed as its own; a changed record is. The predecessor's
    rows are never this collection's coverage."""
    from app.hold import land as landing, vectors_build as vb
    root, snapshot, codex, kw = ready
    for check, key in (("roots", OURS), ("chunks", OURS), ("records-vs-origin", PEER)):
        attested(root, codex, snapshot, check=check, key=key)
    vb.build(codex, shards=list(snapshot.shards),
             records_for=lambda s: landing.records_for(root, "gutenberg", snapshot, f"{s.slice}/{s.n:05d}", None),
             vectors_for=kw["vectors_for"], embed=kw["embed"], write=kw["write"], batch=2)
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    changed = records[-1]
    _predecessor(root, codex, kw["con"], sid,
                 {**{r["uid"]: r["content_hash"] for r in records}, changed["uid"]: "0" * 32})
    live = [rendezvous.Peer(OUR_KEY, 1000.0), rendezvous.Peer(PEER_KEY, 1e12)]
    out = contribute.once(root, "gutenberg", codex, live=live, **kw)
    mine = [b for b in out["bundles"] if b["shard"] == sid]
    assert len(mine) == 1, out["bundles"]
    held = json.loads((contribute.bundle_dir(root, "gutenberg", codex, sid, mine[0]["bundle_id"])
                       / "possession.json").read_bytes())
    assert set(held) == {changed["uid"]}, "only the record that changed since the predecessor's bundle"
    view = {v["shard"]: v for v in cov.show(codex, snapshot, kw["con"])["shards"]}[cov.shard_id(codex, sid)]
    assert view["covered"] == mine[0]["chunks"], "the predecessor's row is not this collection's coverage"
    assert not any(str(r["shard"]).startswith(PREDECESSOR) for r in cov.rows(kw["con"], shard=cov.shard_id(codex, sid)))


def _own_bundle(root, codex, con, sid, possession, bundle_id):
    """An accepted bundle of THIS collection, this peer's, with its possession."""
    cov.record(con, bundle_id=bundle_id, shard=cov.shard_id(codex, sid), producer=OUR_KEY,
               uids=sorted(possession), chunks=len(possession), tiers={t: "pass" for t in cov.TIERS},
               source=cov.LOCAL, accepted_at="2026-10-22T12:00:00Z")
    d = contribute.bundle_dir(root, "gutenberg", codex, sid, bundle_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "possession.json").write_text(json.dumps(possession), encoding="utf-8")


def test_withdraw_covered_finds_own_copies_another_bundle_covers_and_never_pairs_them(ready):
    """2026-10-22: the node's 117 bundles of gutenberg 6f030405, every record
    already in a d913 bundle. An own bundle whose every (uid, hash) another
    bundle held here covers is found; one with a record nobody else holds is
    not; two own copies of the same records never cover each other."""
    from app.hold import land as landing
    from app.share import possession
    root, snapshot, codex, kw = ready
    shard = snapshot.shards[0]
    sid = f"{shard.slice}/{shard.n:05d}"
    records = landing.records_for(root, "gutenberg", snapshot, sid, None)
    full = {r["uid"]: r["content_hash"] for r in records}
    first = dict(list(full.items())[:1])
    _predecessor(root, codex, kw["con"], f"{shard.slice}/{shard.n + 5:05d}", first)       # covers one record
    _own_bundle(root, codex, kw["con"], sid, first, "sha256:" + "a1" * 32)                 # covered: found
    _own_bundle(root, codex, kw["con"], sid, full, "sha256:" + "a2" * 32)                  # one record nobody else holds
    _own_bundle(root, codex, kw["con"], sid, full, "sha256:" + "a3" * 32)                  # its twin: not cover
    found = possession.covered_by_others(root, "gutenberg", codex, kw["con"], own_key=OUR_KEY)
    assert [f["bundle_id"] for f in found] == ["sha256:" + "a1" * 32]
