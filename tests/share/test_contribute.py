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
