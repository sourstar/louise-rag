"""tests/share/test_contribute.py — `share contribute --once` (07 §7.3;
04 §4.5; 09 §9.7, §9.16): the preconditions refuse first; alone, a peer
leads every open shard, embeds it, bundles it under state/bundles/, passes
its own ladder, records the coverage and announces a `bundle` event; a
second pass finds nothing open; a heavier neighbour takes the shards it
leads; the seeder serves the bundles as members."""
import json
import time

import pytest

from app import db, embeddings
from app.hold import collection as col, envelope as env, records as rec
from app.share import attest, contribute, coverage as cov, members as mem, peers, rendezvous, seeder
from tests.hold.conftest import KEY
from tests.hold.test_admit import reference_and_envelope, space_embed
from tests.hold.test_vectors_build import _statement
from tests.hold.test_vectors_import import _member
from tests.share.test_messages import PEER, PEER_KEY
from tests.share.test_share_api import OURS, OUR_KEY

pytestmark = pytest.mark.strict_encoder


def attested(root, codex, snapshot, *, check, key, time_text="2026-10-06T00:00:00Z"):
    body = {"kind": "louise-attestation", "version": 3, "collection_id": codex.collection_id,
            "snapshot_id": snapshot.snapshot_id, "snapshot_hash": snapshot.hash, "check": check,
            "seed": None, "shards": [], "result": "pass", "detail": {},
            "platform_image": "sha256:" + "1f" * 32, "encoder_engine": None, "encoder_weights": None,
            "key": env.key_text(key.verify_key), "time": time_text}
    envelope = env.sign(env.ATTESTATION, body, key)
    p = attest.attestation_path(root, "gutenberg", codex, env.body_id(body))
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
                        lambda root_, name, codex_, stop: (kw["embed"], None))
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
    # the other peer's window runs out with no bundle: its shards cascade to us
    others = [f"{s.slice}/{s.n:05d}" for s in snapshot.shards if f"{s.slice}/{s.n:05d}" not in expected]
    if others:
        opened = cov.opened_at(kw["con"])
        assert all(cov.shard_id(codex, sid) in opened for sid in others), "seen open: the window started"
        grace = codex.update.grace_days * 86400
        within = time.time() + grace - 60
        still = contribute.shards_i_lead(codex, snapshot, kw["con"], me=OUR_KEY, live=live, now=within)
        assert still == [], "within the leader's window the shard is the leader's"
        later = time.time() + grace + 3 * 86400
        taken = contribute.shards_i_lead(codex, snapshot, kw["con"], me=OUR_KEY, live=live, now=later)
        assert sorted(f"{s.slice}/{s.n:05d}" for s in taken) == sorted(others), \
            "the leader published nothing within grace_days: the next in line leads (04 §4.5)"
        much_later = time.time() + 2 * grace + 3 * 86400
        assert contribute.shards_i_lead(codex, snapshot, kw["con"], me=OUR_KEY, live=live, now=much_later) == [], \
            "every window run out: the cascade starts over at the top"


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
